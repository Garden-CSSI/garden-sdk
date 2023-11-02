import datetime
import inspect
import json
import logging
import os
import shutil
import subprocess
import textwrap
import time
import webbrowser
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any, Dict, List, Optional
from uuid import UUID

import docker  # type: ignore
import typer

from garden_ai import GardenClient, GardenConstants, RegisteredPipeline, local_data
from garden_ai.app.console import console
from garden_ai.container.containerize import (  # type: ignore
    IMAGE_NAME,
    build_container,
    start_container,
)
from garden_ai.containers import (
    JUPYTER_TOKEN,
    build_notebook_session_image,
    extract_metadata_from_image,
    push_image_to_public_repo,
    start_container_with_notebook,
)
from garden_ai.local_data import _get_notebook_base_image, _put_notebook_base_image
from garden_ai.pipelines import PipelineMetadata
from garden_ai.utils._meta import redef_in_main

logger = logging.getLogger()

notebook_app = typer.Typer(name="notebook")


@notebook_app.callback(no_args_is_help=True)
def notebook():
    """sub-commands for editing and publishing from sandboxed notebooks."""
    pass


@notebook_app.command()
def list_premade_images():
    """List all Garden base docker images"""
    premade_images = ", ".join(
        [
            "'" + image_name + "'"
            for image_name in list(GardenConstants.PREMADE_IMAGES.keys())
        ]
    )
    print(f"Garden premade images:\n{premade_images}")


@notebook_app.command(no_args_is_help=True)
def start(
    path: Path = typer.Argument(
        ...,
        file_okay=True,
        dir_okay=False,
        writable=True,
        readable=True,
        help=("Path to a .ipynb notebook to open in a fresh, isolated container. "),
    ),
    base_image: Optional[str] = typer.Option(
        default=None,
        help=(
            "A Garden base image to boot the notebook in. "
            "For example, to boot your notebook with the default Garden python 3.8 image, use --base-image 3.8-base. "
            "To see all the available Garden base images, use 'garden-ai notebook list-premade-images'"
        ),
    ),
):
    """Open a notebook file in a sandboxed environment. Optionally, specify a different base docker image.

    Changes to the notebook file will persist after the container shuts down.
    Quit the process with Ctrl-C or by shutting down jupyter from the browser.
    If a different base image is chosen, that image will be reused as the default for this notebook in the future.
    """
    notebook_path = path.resolve()
    if notebook_path.suffix != ".ipynb":
        raise ValueError("File must be a jupyter notebook (.ipynb)")

    if not notebook_path.exists():
        top_level_dir = Path(__file__).parent.parent
        source_path = top_level_dir / "notebook_templates" / "sklearn.ipynb"
        shutil.copy(source_path, notebook_path)

    # check/update local data for base image choice
    if base_image in list(GardenConstants.PREMADE_IMAGES.keys()):
        base_image = GardenConstants.PREMADE_IMAGES[base_image]
    else:
        premade_images = ", ".join(
            [
                "'" + image_name + "'"
                for image_name in list(GardenConstants.PREMADE_IMAGES.keys())
            ]
        )
        raise Exception(
            f"The image '{base_image}' is not one of the Garen base images. The current Garden base images are: \n{premade_images}"
        )

    base_image = (
        base_image or _get_notebook_base_image(notebook_path) or "gardenai/test:latest"
    )
    _put_notebook_base_image(notebook_path, base_image)

    # start container and listen for Ctrl-C
    docker_client = docker.from_env()
    container = start_container_with_notebook(docker_client, notebook_path, base_image)
    _register_container_sigint_handler(container)

    typer.echo(
        f"Notebook started! Opening http://127.0.0.1:8888/tree?token={JUPYTER_TOKEN} in your default browser (you may need to refresh the page)"
    )
    webbrowser.open_new_tab(f"http://127.0.0.1:8888/tree?token={JUPYTER_TOKEN}")

    # stream logs from the container
    for line in container.logs(stream=True):
        print(line.decode("utf-8"), end="")

    # block until the container finishes
    try:
        container.reload()
        container.wait()
    except KeyboardInterrupt:
        # handle windows Ctrl-C
        typer.echo("Stopping notebook ...")
        container.stop()
    except docker.errors.NotFound:
        # container already killed, no need to wait
        pass

    typer.echo("Notebook has stopped.")
    return


def _register_container_sigint_handler(container: docker.models.containers.Container):
    """helper: ensure SIGINT/ Ctrl-C to our CLI stops a given container"""
    import signal

    def handler(signal, frame):
        typer.echo("Stopping notebook...")
        container.stop()
        return

    signal.signal(signal.SIGINT, handler)
    return


@notebook_app.command()
def plant(
    path: Path = typer.Argument(
        ...,
        file_okay=True,
        dir_okay=False,
        writable=True,
        readable=True,
    ),
    base_image: Optional[str] = typer.Option(None),
    image_repo: Optional[str] = typer.Option(
        None,
        "--repo",
        help=(
            "Name of a public Dockerhub repository to publish garden-generated "
            "images, e.g. `user/garden-images`. The repository must already "
            "exist and you must have push access to the repository. "
        ),
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
):
    client = GardenClient()
    notebook_path = path.resolve()
    if notebook_path.suffix != ".ipynb":
        raise ValueError("File must be a jupyter notebook (.ipynb)")
    if not notebook_path.exists():
        raise ValueError(f"Could not find file at {notebook_path}")

    # check for preferred base image
    base_image = (
        base_image or _get_notebook_base_image(notebook_path) or "gardenai/test:latest"
    )
    _put_notebook_base_image(notebook_path, base_image)

    # check for preferred image repository
    image_repo = image_repo or local_data._get_user_image_repo()

    if image_repo is None:
        raise ValueError("No image repository specified.")
    else:
        # remember for next time
        local_data._store_user_image_repo(image_repo)

    # Build the image
    docker_client = docker.from_env()
    image = build_notebook_session_image(
        docker_client, notebook_path, base_image, image_repo, print_logs=verbose
    )
    if image is None:
        typer.echo("Failed to build image.")
        raise typer.Exit(1)
    typer.echo(f"Built image: {image}")

    # generate tag and and push image to dockerhub
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    image_tag = f"{notebook_path.stem}-{timestamp}"
    typer.echo(f"Pushing image to repository: {image_repo}")
    image_location = push_image_to_public_repo(
        docker_client, image, image_repo, image_tag
    )

    # register image with globus compute
    container_uuid = UUID(
        client.compute_client.register_container(image_location, "docker")
    )

    # register pipelines and add to gardens according to metadata
    typer.echo("Extracting metadata ...\n")
    metadata = extract_metadata_from_image(docker_client, image)
    print(metadata)

    dirty_gardens = set()  # good for gardens to get dirty, actually
    for key, record in metadata.items():
        if "." in key:
            continue
        # register function with globus compute
        # and populate required RegisteredPipeline fields
        to_register = _make_function_to_register(key)
        record["container_uuid"] = container_uuid
        record["func_uuid"] = client.compute_client.register_function(
            to_register, container_uuid=str(container_uuid), public=True
        )
        record["doi"] = record.get("doi") or client._mint_draft_doi()
        record["short_name"] = record.get("short_name") or key
        registered = RegisteredPipeline(**record)
        client._update_datacite(registered)
        local_data.put_local_pipeline(registered)
        print(
            f"Successfully registered pipeline: {registered.short_name}: {registered.doi}"
        )

        # fetch garden we're adding this to, if one is specified
        garden_doi = metadata.get(f"{key}.garden_doi")
        if garden_doi:
            garden = local_data.get_local_garden_by_doi(garden_doi)
            if garden is None:
                msg = (
                    f"Could not add pipeline {key} to garden "
                    f"{garden_doi}: could not find local garden with that DOI"
                )
                raise ValueError(msg)
            garden.add_pipeline(registered.doi)
            local_data.put_local_garden(garden)
            dirty_gardens |= {garden.doi}
            print(f"Added pipeline {registered.short_name} to garden {garden.doi}!")

    for doi in dirty_gardens:
        garden = local_data.get_local_garden_by_doi(doi)
        if garden:
            print(f"(Re-)publishing garden {garden.doi} with updated pipeline(s)")
            client.publish_garden_metadata(garden)


def _make_function_to_register(func_name: str):
    def call_pipeline(*args, **kwargs):
        import dill  # type: ignore

        dill.load_session("session.pkl")
        func = globals()[func_name]
        return func(*args, **kwargs)

    return call_pipeline
