import json
import os
import logging
from typing import Callable, Optional
import boto3
from functools import wraps

import requests

from garden_ai.constants import GardenConstants
from garden_ai.gardens import PublishedGarden

logger = logging.getLogger()


def backend_toggle(func):
    """feature flag decorator for BackendClient methods.

    Behavior is controlled by the env vars GARDEN_ENV and GARDEN_BACKEND_TOGGLES
    (see .env.shared). To disable for all methods, set
    GARDEN_BACKEND_TOGGLES="".

    If a decorated method's name (e.g. mint_doi_on_datacite) is found in the
    GARDEN_BACKEND_TOGGLES var, it will target an instance of the new backend
    according to the value of GARDEN_ENV.

    If GARDEN_ENV=dev or prod, this targets one of the lightsail deployments. If
    GARDEN_ENV=local, this targets localhost:5500 (i.e. an instance started with
    the backend's run-dev-server.sh script)
    """
    # "dev", "prod" or "local"
    garden_env = os.getenv("GARDEN_ENV", "")
    # e.g. "mint_doi_on_datacite,update_doi_on_datacite"
    toggles = os.getenv("GARDEN_BACKEND_TOGGLES", "")
    env_url_mapping = {
        "dev": "https://garden-service-dev.0dh7fu9qsbhfi.us-east-1.cs.amazonlightsail.com",
        "prod": "https://garden-service-prod.0dh7fu9qsbhfi.us-east-1.cs.amazonlightsail.com",
        "local": "http://localhost:5500",
    }
    if garden_env not in env_url_mapping or func.__name__ not in toggles:
        return func

    old_url = GardenConstants.GARDEN_ENDPOINT
    new_url = env_url_mapping[garden_env]

    old_call_method = BackendClient._call

    def _call_with_trailing_slash(self: BackendClient, http_verb, resource, payload):
        """Drop in replacement for monkey-patching `BackendClient._call`"""
        # for context: calls to the new backend on lightsail break without a
        # trailing slash, but a trailing slash everywhere would break calls to
        # the old backend.
        resource = resource + "/"
        return old_call_method(self, http_verb, resource, payload)

    @wraps(func)
    def monkey_patch_url(*args, **kwargs):
        try:
            GardenConstants.GARDEN_ENDPOINT = new_url
            BackendClient._call = _call_with_trailing_slash
            return func(*args, **kwargs)
        finally:
            GardenConstants.GARDEN_ENDPOINT = old_url
            BackendClient._call = old_call_method

    return monkey_patch_url


# Client for the Garden backend API. The name "GardenClient" was taken :)
class BackendClient:
    def __init__(self, garden_authorizer):
        self.garden_authorizer = garden_authorizer

    def _call(
        self, http_verb: Callable, resource: str, payload: Optional[dict]
    ) -> dict:
        headers = {"Authorization": self.garden_authorizer.get_authorization_header()}
        url = GardenConstants.GARDEN_ENDPOINT + resource
        resp = http_verb(url, headers=headers, json=payload)
        try:
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError:
            logger.error(
                f"Request to Garden backend failed. Status code {resp.status_code}. {resp.text}"
            )
            raise
        except requests.exceptions.JSONDecodeError:
            logger.error(f"Could not parse response as JSON. {resp.text}")
            raise

    def _post(self, resource: str, payload: dict) -> dict:
        return self._call(requests.post, resource, payload)

    def _put(self, resource: str, payload: dict) -> dict:
        return self._call(requests.put, resource, payload)

    def _delete(self, resource: str, payload: dict) -> dict:
        return self._call(requests.delete, resource, payload)

    def _get(self, resource: str) -> dict:
        return self._call(requests.get, resource, None)

    @backend_toggle
    def mint_doi_on_datacite(self, payload: dict) -> str:
        response_dict = self._post("/doi", payload)
        doi = response_dict.get("doi", None)
        if not doi:
            raise ValueError("Failed to mint DOI. Response was missing doi field.")
        return doi

    @backend_toggle
    def update_doi_on_datacite(self, payload: dict):
        self._put("/doi", payload)

    @backend_toggle
    def publish_garden_metadata(self, garden: PublishedGarden):
        payload = json.loads(garden.json())
        self._post("/garden-search-record", payload)

    @backend_toggle
    def delete_garden_metadata(self, doi: str):
        self._delete("/garden-search-record", {"doi": doi})

    @backend_toggle
    def upload_notebook(
        self, notebook_contents: dict, username: str, notebook_name: str
    ):
        payload = {
            "notebook_json": json.dumps(notebook_contents),
            "notebook_name": notebook_name,
            "folder": username,
        }
        resp = self._post("/notebook", payload)
        return resp["notebook_url"]

    @backend_toggle
    def get_docker_push_session(self) -> boto3.Session:
        resp = self._get("/docker-push-token")

        # Make sure the response has the expected fields
        for field in ["AccessKeyId", "SecretAccessKey", "SessionToken", "ECRRepo"]:
            if field not in resp or not resp[field]:
                raise ValueError(
                    f"/docker-push-token response missing field {field}. Full response: {resp}"
                )

        return boto3.Session(
            aws_access_key_id=resp["AccessKeyId"],
            aws_secret_access_key=resp["SecretAccessKey"],
            aws_session_token=resp["SessionToken"],
            region_name="us-east-1",
        )
