import os, sys, shutil
import pickle
import jinja2
import uuid
import json
import functools

import typer
from typing import Optional, Callable
from typer.testing import CliRunner
from pathlib import Path
from datetime import datetime
from rich.prompt import Prompt
from rich import print as rich_print
import unittest.mock as mocker

import garden_ai
from garden_ai.app.main import app

from globus_compute_sdk import Client
import globus_sdk

"""
If prompt_for_git_secret is true, must have a json file ./templates/git_secrets.json containing
GARDEN_API_CLIENT_ID and GARDEN_API_CLIENT_SECRET. For manual running ease of use.
"""
prompt_for_git_secret = False

sklearn_container_uuid = "b9cf409f-d5f2-4956-b198-0d90ffa133e6"
tf_container_uuid = "TBD"
torch_container_uuid = "44563f7c-5045-4b6b-8e3a-34f5c7fa781e"

key_store_path = Path(os.path.expanduser("~/.garden"))

garden_title = f"ETE-Test-Garden-{str(uuid.uuid4())}"

scaffolded_pipeline_folder_name = "ete_test_pipeline_title"
pipeline_template_name = "ete_pipeline"

sklearn_pipeline_path = os.path.join(key_store_path, "sklearn_pipeline.py")
sklearn_pipeline_name = "ETESklearnPipeline"
sklearn_model_name = "ETE-Test-Model-Sklearn"

tf_pipeline_path = os.path.join(key_store_path, "tf_pipeline.py")
tf_pipeline_name = "ETETfPipeline"
tf_model_name = "ETE-Test-Model-Tf"

torch_pipeline_path = os.path.join(key_store_path, "torch_pipeline.py")
torch_pipeline_name = "ETETorchPipeline"
torch_model_name = "ETE-Test-Model-Torch"

sklearn_model_location = os.path.abspath("./models/sklearn_model.pkl")
tf_model_location = os.path.abspath("./models/keras_model")
torch_model_location = os.path.abspath("./models/torch_model.pth")

sklearn_input_data_location = os.path.abspath("./models/sklearn_test_input.pkl")
tf_input_data_location = os.path.abspath("./models/keras_test_input.pkl")
torch_input_data_location = os.path.abspath("./models/torch_test_input.pkl")

sklearn_model_reqs_location = os.path.abspath("./models/sklearn_requirements.txt")
tf_model_reqs_location = os.path.abspath("./models/keras_requirements.txt")
torch_model_reqs_location = os.path.abspath("./models/torch_requirements.txt")

pipeline_template_location = os.path.abspath("./templates")

example_garden_data = {
    "authors": ["Test Garden Author"],
    "title": "ETE Test Garden Title",
    "contributors": ["Test Garden Contributor"],
    "year": "2023",
    "description": "ETE Test Garden Description",
}
example_pipeline_data = {
    "authors": ["Test Pipeline Author"],
    "title": "ETE Test Pipeline Title",
    "contributors": ["Test Pipeline Contributor"],
    "year": "2023",
    "description": "ETE Test Pipeline Description",
}

local_files_list = [
    sklearn_pipeline_path,
    tf_pipeline_path,
    torch_pipeline_path,
    os.path.join(key_store_path, scaffolded_pipeline_folder_name),
    os.path.join(key_store_path, "data.json"),
    os.path.join(key_store_path, "tokens.json"),
    os.path.join(key_store_path, "model.zip.zip"),
]


t_app = typer.Typer()


@t_app.command()
def run_garden_end_to_end(
    ete_grant: Optional[str] = "cc",
    ete_model: Optional[str] = "sklearn",
    globus_compute_endpoint: Optional[str] = "none",
    live_print_stdout: Optional[bool] = False,
    pre_build_container: Optional[str] = "none",
):
    # Set up
    rich_print("\n[bold blue]Setup ETE Test[/bold blue]")

    run_sklearn = False
    run_tf = False
    run_torch = False
    if ete_model == "all":
        run_sklearn = True
        run_tf = True
        run_torch = True
    elif ete_model == "sklearn":
        run_sklearn = True
    elif ete_model == "tf":
        run_tf = True
    elif ete_model == "torch":
        run_torch = True

    rich_print(f"Testing with sklearn model: {run_sklearn}")
    rich_print(f"Testing with tensorflow model: {run_tf}")
    rich_print(f"Testing with pytorch model: {run_torch}")

    is_gha = os.getenv("GITHUB_ACTIONS")

    runner = None
    rich_print(f"CliRunner live print set to: {live_print_stdout}")
    if live_print_stdout:
        runner = _make_live_print_runner()
    else:
        runner = CliRunner()

    rich_print(f"Pre build container set to: {pre_build_container}")
    rich_print(f"garden_ai module location: {garden_ai.__file__}")
    rich_print("\n[bold blue]Starting ETE Test[/bold blue]")

    # Cleanup any left over files generated from the test
    _cleanup_local_files(local_files_list)

    client = None
    if ete_grant == "cc":
        # Create GardenClient with ClientCredentialsAuthorizer and patch all instances of GardenClients
        rich_print("Initializing GardenClient with CC grant.")
        if is_gha:
            GARDEN_API_CLIENT_ID = os.getenv("GARDEN_API_CLIENT_ID")
            GARDEN_API_CLIENT_SECRET = os.getenv("GARDEN_API_CLIENT_SECRET")
        else:
            if prompt_for_git_secret:
                GARDEN_API_CLIENT_ID = Prompt.ask(
                    "Please enter the GARDEN_API_CLIENT_ID here "
                ).strip()
                GARDEN_API_CLIENT_SECRET = Prompt.ask(
                    "Please enter the GARDEN_API_CLIENT_SECRET here "
                ).strip()
            else:
                with open("./templates/git_secrets.json") as json_file:
                    git_secrets = json.load(json_file)
                GARDEN_API_CLIENT_ID = git_secrets["GARDEN_API_CLIENT_ID"]
                GARDEN_API_CLIENT_SECRET = git_secrets["GARDEN_API_CLIENT_SECRET"]

        client = _make_garden_client_with_cc(
            GARDEN_API_CLIENT_ID, GARDEN_API_CLIENT_SECRET
        )

    elif ete_grant == "at":
        # Create GardenClient normally with access token grant
        rich_print("Initializing GardenClient with AT grant.")
        client = _make_garden_client_with_at()
    else:
        raise Exception(
            "Invalid grant type; must be either CC (Client credential grant) or AT (Access token grant)."
        )

    # Patch all instances of GardenClient with our new grant type one and run tests with patches.
    # If pre build container is true then also patch build_container method.
    if pre_build_container != "none":
        with mocker.patch(
            "garden_ai.app.garden.GardenClient"
        ) as mock_garden_gc, mocker.patch(
            "garden_ai.app.model.GardenClient"
        ) as mock_model_gc, mocker.patch(
            "garden_ai.app.pipeline.GardenClient"
        ) as mock_pipeline_gc, mocker.patch.object(
            client, "build_container"
        ) as mock_container_build:
            mock_garden_gc.return_value = client
            mock_model_gc.return_value = client
            mock_pipeline_gc.return_value = client

            if pre_build_container == "sklearn":
                mock_container_build.return_value = sklearn_container_uuid
            elif pre_build_container == "tf":
                mock_container_build.return_value = sklearn_container_uuid
            elif pre_build_container == "torch":
                mock_container_build.return_value = sklearn_container_uuid
            else:
                raise Exception(
                    "Invalid container type; must be either sklearn, tf or torch."
                )

            _run_test_cmds(
                client,
                runner,
                ete_grant,
                ete_model,
                globus_compute_endpoint,
                run_sklearn,
                run_tf,
                run_torch,
                GARDEN_API_CLIENT_ID,
                GARDEN_API_CLIENT_SECRET,
            )
    else:
        with mocker.patch(
            "garden_ai.app.garden.GardenClient"
        ) as mock_garden_gc, mocker.patch(
            "garden_ai.app.model.GardenClient"
        ) as mock_model_gc, mocker.patch(
            "garden_ai.app.pipeline.GardenClient"
        ) as mock_pipeline_gc:
            mock_garden_gc.return_value = client
            mock_model_gc.return_value = client
            mock_pipeline_gc.return_value = client

            _run_test_cmds(
                client,
                runner,
                ete_grant,
                ete_model,
                globus_compute_endpoint,
                run_sklearn,
                run_tf,
                run_torch,
                GARDEN_API_CLIENT_ID,
                GARDEN_API_CLIENT_SECRET,
            )

    # Cleanup local files
    _cleanup_local_files(local_files_list)


def _run_test_cmds(
    client,
    runner,
    ete_grant,
    ete_model,
    globus_compute_endpoint,
    run_sklearn,
    run_tf,
    run_torch,
    GARDEN_API_CLIENT_ID,
    GARDEN_API_CLIENT_SECRET,
):
    old_cwd = os.getcwd()
    os.chdir(key_store_path)

    # Garden create
    new_garden = _test_garden_create(example_garden_data, garden_title, runner)

    # Pipeline create
    _test_pipeline_create(
        example_pipeline_data,
        key_store_path,
        scaffolded_pipeline_folder_name,
        runner,
    )

    if run_sklearn:
        # Model register sklearn
        sklearn_model_full_name = _test_model_register(
            sklearn_model_location, "sklearn", sklearn_model_name, runner
        )
        # Pipeline make sklearn
        sklearn_pipeline_local = _make_pipeline_file(
            sklearn_pipeline_name,
            sklearn_model_full_name,
            sklearn_model_reqs_location,
            sklearn_pipeline_path,
            pipeline_template_name,
            pipeline_template_location,
            GARDEN_API_CLIENT_ID,
            GARDEN_API_CLIENT_SECRET,
            client,
        )
        # Pipeline register sklearn
        sklearn_pipeline = _test_pipeline_register(
            sklearn_pipeline_path,
            sklearn_pipeline_local,
            sklearn_model_full_name,
            runner,
        )
        # Add sklearn pipeline to garden
        _test_garden_add_pipeline(new_garden, sklearn_pipeline, runner)

    if run_tf:
        # Model register tensorflow
        tf_model_full_name = _test_model_register(
            tf_model_location, "tensorflow", tf_model_name, runner
        )
        # Pipeline make tensorflow
        tf_pipeline_local = _make_pipeline_file(
            tf_pipeline_name,
            tf_model_full_name,
            tf_model_reqs_location,
            tf_pipeline_path,
            pipeline_template_name,
            pipeline_template_location,
            GARDEN_API_CLIENT_ID,
            GARDEN_API_CLIENT_SECRET,
            client,
        )
        # Pipeline register tensorflow
        tf_pipeline = _test_pipeline_register(
            tf_pipeline_path, tf_pipeline_local, tf_model_full_name, runner
        )
        # Add tensorflow pipeline to garden
        _test_garden_add_pipeline(new_garden, tf_pipeline, runner)

    if run_torch:
        # Model register pytorch
        torch_model_full_name = _test_model_register(
            torch_model_location, "pytorch", torch_model_name, runner
        )
        # Pipeline make pytorch
        torch_pipeline_local = _make_pipeline_file(
            torch_pipeline_name,
            torch_model_full_name,
            torch_model_reqs_location,
            torch_pipeline_path,
            pipeline_template_name,
            pipeline_template_location,
            GARDEN_API_CLIENT_ID,
            GARDEN_API_CLIENT_SECRET,
            client,
        )
        # Pipeline register pytorch
        torch_pipeline = _test_pipeline_register(
            torch_pipeline_path, torch_pipeline_local, torch_model_full_name, runner
        )
        # Add pytorch pipeline to garden
        _test_garden_add_pipeline(new_garden, torch_pipeline, runner)

    # Publish the garden
    published_garden = _test_garden_publish(new_garden, runner)

    # Search for our garden
    _test_garden_search(published_garden, runner)

    # Test run all pipelines on globus compute endpoint
    if globus_compute_endpoint != "none":
        if run_sklearn:
            _test_run_garden_on_endpoint(
                published_garden,
                key_store_path,
                sklearn_pipeline_name,
                sklearn_input_data_location,
                globus_compute_endpoint,
                client,
            )
        if run_tf:
            _test_run_garden_on_endpoint(
                published_garden,
                key_store_path,
                tf_pipeline_name,
                tf_input_data_location,
                globus_compute_endpoint,
                client,
            )
        if run_torch:
            _test_run_garden_on_endpoint(
                published_garden,
                key_store_path,
                torch_pipeline_name,
                torch_input_data_location,
                globus_compute_endpoint,
                client,
            )
    else:
        rich_print("Skipping remote execution on endpoint; no endpoint given.")

    os.chdir(old_cwd)


def _make_garden_client_with_cc(CLIENT_ID, CLIENT_SECRET):
    rich_print("Starting auth setup for CC grant.")
    confidential_client = globus_sdk.ConfidentialAppAuthClient(CLIENT_ID, CLIENT_SECRET)
    client = garden_ai.GardenClient(auth_client=confidential_client)
    rich_print("Finished auth setup for CC grant.")
    return client


def _make_garden_client_with_at():
    rich_print("Starting auth setup for AT grant.")
    client = garden_ai.GardenClient()
    rich_print("Finished auth setup for AT grant.")
    return client


def _test_garden_create(example_garden_data, unique_title, runner):
    rich_print("\nStarting test: [italic red]garden create[/italic red]")

    gardens_before = garden_ai.local_data.get_all_local_gardens()
    assert gardens_before is None

    command = [
        "garden",
        "create",
        "--title",
        unique_title,
        "--description",
        example_garden_data["description"],
        "--year",
        example_garden_data["year"],
    ]
    for name in example_garden_data["authors"]:
        command += ["--author", name]
    for name in example_garden_data["contributors"]:
        command += ["--contributor", name]

    result = runner.invoke(app, command)

    assert result.exit_code == 0

    gardens_after = garden_ai.local_data.get_all_local_gardens()
    assert len(gardens_after) == 1

    new_garden = gardens_after[0]
    assert new_garden.title == unique_title
    assert new_garden.description == example_garden_data["description"]

    rich_print("Finished test: [italic red]garden create[/italic red] with no errors.")
    return new_garden


def _test_garden_add_pipeline(original_garden, pipeline, runner):
    rich_print("\nStarting test: [italic red]garden add-pipeline[/italic red]")

    command = [
        "garden",
        "add-pipeline",
        "--garden",
        original_garden.doi,
        "--pipeline",
        pipeline.doi,
    ]

    result = runner.invoke(app, command)
    assert result.exit_code == 0

    garden_after_addition = garden_ai.local_data.get_local_garden_by_doi(
        original_garden.doi
    )
    local_pipelines = garden_ai.local_data.get_all_local_pipelines()
    local_pipeline_ids = []
    for pl in local_pipelines:
        local_pipeline_ids.append(pl.doi)

    for pl_id in garden_after_addition.pipeline_ids:
        assert pl_id not in original_garden.pipeline_ids
        assert pl_id in garden_after_addition.pipeline_ids
        assert pl_id in local_pipeline_ids

    rich_print(
        "Finished test: [italic red]garden add-pipeline[/italic red] with no errors"
    )


def _test_garden_publish(garden, runner):
    rich_print("\nStarting test: [italic red]garden publish[/italic red]")

    command = [
        "garden",
        "publish",
        "-g",
        garden.doi,
    ]

    result = runner.invoke(app, command)
    assert result.exit_code == 0

    rich_print("Finished test: [italic red]garden publish[/italic red] with no errors")

    return garden_ai.local_data.get_local_garden_by_doi(garden.doi)


def _test_garden_search(garden, runner):
    rich_print("\nStarting test: [italic red]garden search[/italic red]")

    command = [
        "garden",
        "search",
        "-t",
        garden.title,
    ]

    result = runner.invoke(app, command)
    assert result.exit_code == 0

    assert garden.title in result.stdout
    assert str(garden.doi) in result.stdout

    rich_print("Finished test: [italic red]garden search[/italic red] with no errors")


def _test_model_register(model_location, flavor, short_name, runner):
    rich_print(
        f"\nStarting test: [italic red]model register[/italic red] with model flavor: {flavor}"
    )

    command = [
        "model",
        "register",
        short_name,
        str(model_location),
        flavor,
    ]

    result = runner.invoke(app, command)
    assert result.exit_code == 0

    local_models = garden_ai.local_data.get_all_local_models()

    local_model = None
    for model in local_models:
        if model.model_name == short_name:
            local_model = model
            break

    assert local_model is not None
    assert local_model.full_name is not None
    assert local_model.model_name == short_name
    assert local_model.flavor == flavor

    rich_print(
        f"Finished test: [italic red]model register[/italic red] with model flavor: {flavor} with no errors"
    )

    return local_model.full_name


def _test_pipeline_create(
    example_pipeline_data, location, scaffolded_pipeline_folder_name, runner
):
    rich_print("\nStarting test: [italic red]pipeline create[/italic red]")

    command = [
        "pipeline",
        "create",
        "--directory",
        str(location),
        "--title",
        example_pipeline_data["title"],
        "--description",
        example_pipeline_data["description"],
        "--year",
        example_pipeline_data["year"],
    ]
    for name in example_pipeline_data["authors"]:
        command += ["--author", name]
    for name in example_pipeline_data["contributors"]:
        command += ["--contributor", name]

    result = runner.invoke(app, command)
    assert result.exit_code == 0

    assert os.path.exists(os.path.join(location, scaffolded_pipeline_folder_name))
    assert os.path.isfile(
        os.path.join(location, scaffolded_pipeline_folder_name, "pipeline.py")
    )
    assert os.path.isfile(
        os.path.join(location, scaffolded_pipeline_folder_name, "requirements.txt")
    )

    rich_print("Finished test: [italic red]pipeline create[/italic red] with no errors")


def _test_pipeline_register(pipeline_path, pipeline, model_full_name, runner):
    rich_print("\nStarting test: [italic red]pipeline register[/italic red]")

    command = [
        "pipeline",
        "register",
        pipeline_path,
    ]

    result = runner.invoke(app, command)

    assert result.exit_code == 0

    local_pipelines = garden_ai.local_data.get_all_local_pipelines()

    registered_pipeline = None
    for local_pipeline in local_pipelines:
        if str(pipeline.doi) == str(local_pipeline.doi):
            registered_pipeline = local_pipeline
            break

    assert registered_pipeline is not None
    assert pipeline.title == registered_pipeline.title
    assert registered_pipeline.doi is not None

    assert len(registered_pipeline.steps) == 1
    assert model_full_name in registered_pipeline.steps[0]["model_full_names"]

    rich_print(
        "Finished test: [italic red]pipeline register[/italic red] with no errors"
    )

    return registered_pipeline


def _test_run_garden_on_endpoint(
    garden, key_store_path, pipeline_name, input_file, globus_compute_endpoint, client
):
    rich_print("\nStarting test: [bold blue]garden remote execution[/bold blue]")

    with open(input_file, "rb") as f:
        Xtest = pickle.load(f)
    test_garden = client.get_published_garden(garden.doi)
    run_pipeline = getattr(test_garden, pipeline_name)

    result = run_pipeline(Xtest, endpoint=globus_compute_endpoint)

    rich_print(
        "Finished test: [bold blue]garden remote execution[/bold blue] with no errors"
    )
    assert result is not None


def _make_pipeline_file(
    short_name,
    model_full_name,
    req_file_path,
    save_path,
    template_name,
    pipeline_template_location,
    GARDEN_API_CLIENT_ID,
    GARDEN_API_CLIENT_SECRET,
    client,
):
    rich_print(f"\nMaking pipeline file: {short_name}.")

    @garden_ai.step
    def run_inference(arg: object) -> object:
        """placeholder"""
        return arg

    pipeline = client.create_pipeline(
        title=short_name,
        authors=["ETE Test Author"],
        contributors=[],
        steps=[run_inference],  # type: ignore
        tags=[],
        description="ETE Test Pipeline",
        year=str(datetime.now().year),
    )

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(pipeline_template_location))
    template = env.get_template(template_name)
    contents = template.render(
        short_name=short_name,
        pipeline=pipeline,
        model_full_name=model_full_name,
        req_file_path=req_file_path,
        GARDEN_API_CLIENT_ID=GARDEN_API_CLIENT_ID,
        GARDEN_API_CLIENT_SECRET=GARDEN_API_CLIENT_SECRET,
    )

    with open(save_path, "w") as f:
        f.write(contents)

    rich_print(f"Finished pipeline file: {short_name}.")
    return pipeline


def _cleanup_local_files(local_file_list):
    rich_print("\nDeleting leftover up local files.")
    for path in local_file_list:
        if os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
        else:
            rich_print(f"Could not find path: {path}, skipping")


def _make_live_print_runner():
    """Yield a typer.testing.CliRunner to invoke the CLI."""
    """This enables pytest to show the output in its logs on test failures."""
    """https://github.com/pallets/click/issues/737"""

    cls = CliRunner

    def invoke_wrapper(f):
        """Augment CliRunner.invoke to emit its output to stdout."""

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            result = f(*args, **kwargs)
            sys.stdout.write(result.output)
            return result

        return wrapper

    cls.invoke = invoke_wrapper(cls.invoke)
    cli_runner = cls()

    return cli_runner


if __name__ == "__main__":
    t_app()
