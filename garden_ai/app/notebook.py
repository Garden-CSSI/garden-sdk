import logging
import shutil
import webbrowser
from pathlib import Path
from typing import Optional, cast
import json
import yaml
import time

import docker  # type: ignore
import typer
import nbformat
import types
from tempfile import TemporaryDirectory

from garden_ai import GardenClient, GardenConstants
from garden_ai.app.console import print_err
from garden_ai.containers import (
    build_image_with_dependencies,
    build_notebook_session_image,
    push_image_to_public_repo,
    start_container_with_notebook,
    get_docker_client,
    extract_metadata_from_image,
    DockerStartFailure,
    DockerBuildFailure,
    DockerPreBuildFailure,
)

from garden_ai.utils.notebooks import (
    clear_cells,
    is_over_size_limit,
    generate_botanical_filename,
)

logger = logging.getLogger()

notebook_app = typer.Typer(name="notebook")

BASE_IMAGE_NAMES = ", ".join(
    ["'" + image_name + "'" for image_name in GardenConstants.PREMADE_IMAGES.keys()]
)

NOTEBOOK_METADATA_CELL_TEMPLATE = (
    "# This cell is auto-generated by Garden. Don't delete it. Do keep it as the first cell.\n"
    "# It records the base image and requirements you passed to `garden-ai notebook start`.\n"
    "# That way the next time you run this notebook Garden can start it with the same libraries.\n\n"
)


class DockerClientSession:
    def __init__(self, verbose=False) -> None:
        self.verbose = verbose

    def __enter__(self) -> docker.DockerClient:
        try:
            return get_docker_client()
        except DockerStartFailure as e:
            # We're most likely to see this error raised from get_docker_client.
            self.handle_docker_start_failure(e)
        except docker.errors.BuildError as e:
            self.handle_docker_build_failure(e)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is DockerStartFailure:
            # If the user's Docker daemon shuts down partway through the session
            # and another docker command is issued, we'll catch that here.
            self.handle_docker_start_failure(exc_val)
        # Use isinstance to catch subclasses of docker.errors.BuildError
        elif isinstance(exc_val, docker.errors.BuildError):
            self.handle_docker_build_failure(exc_val)

    def handle_docker_build_failure(self, e: docker.errors.BuildError):
        # If the user is in verbose mode, the build log has already been printed.
        if not self.verbose:
            for line in e.build_log:
                typer.echo(line)

        print_err(f"Fatal Docker build error: {e}\n" "Above is the full build log.\n")

        if isinstance(e, DockerPreBuildFailure):
            print_err(
                "Garden could not set up your base Docker image. "
                "If you supplied a requirements file, check that it's formatted correctly.\n"
            )
        elif isinstance(e, DockerBuildFailure):
            last_line = e.build_log[-1] if len(e.build_log) > 0 else ""
            if "Traceback" in last_line:
                print_err(
                    "Garden could not build a Docker image from your notebook. "
                    "This is likely because of a bug in your notebook code.\n"
                    "This is where the error occurred: "
                )
                typer.echo(last_line)
            else:
                print_err(
                    "Garden could not build a Docker image from your notebook. "
                    "It looks like it is not an error in your notebook code.\n"
                )

        raise typer.Exit(1)

    def handle_docker_start_failure(self, e: DockerStartFailure):
        print_err("Garden can't access Docker on your computer.")
        if e.helpful_explanation:
            print_err(e.helpful_explanation)
            print_err(
                "If that doesn't work, use `garden-ai docker check` to troubleshoot."
            )
        else:
            print_err(
                "This doesn't look like one of the typical error cases. Printing error from Docker:"
            )
            typer.echo(e.original_exception)
        raise typer.Exit(1)


@notebook_app.callback(no_args_is_help=True)
def notebook():
    """sub-commands for editing and publishing from sandboxed notebooks."""
    pass


@notebook_app.command()
def list_premade_images():
    """List all Garden base docker images"""
    print(f"Garden premade images:\n{BASE_IMAGE_NAMES}")


@notebook_app.command(no_args_is_help=True)
def start(
    path: Optional[Path] = typer.Argument(
        default=None,
        file_okay=True,
        dir_okay=False,
        writable=True,
        readable=True,
        help=("Path to a .ipynb notebook to open in a fresh, isolated container."),
    ),
    base_image_name: Optional[str] = typer.Option(
        None,
        "--base-image",
        help=(
            "A Garden base image to boot the notebook in. "
            "For example, to boot your notebook with the default Garden python 3.8 image, use --base-image 3.8-base. "
            "To see all the available Garden base images, use 'garden-ai notebook list-premade-images'"
        ),
    ),
    requirements_path: Optional[Path] = typer.Option(
        None,
        "--requirements",
        help=(
            "Path to a requirements.txt or a conda environment.yml containing "
            "additional dependencies to install in the base image."
        ),
    ),
    custom_image_uri: Optional[str] = typer.Option(
        None,
        "--custom-image",
        help=(
            "Power users only! Provide a uri of a publicly available docker image to boot the notebook in."
        ),
        hidden=True,
    ),
    tutorial: Optional[bool] = typer.Option(
        False,
        "--tutorial",
        help=(
            "First time using Garden? Open this notebook that walks you through publishing your first model."
        ),
        hidden=True,
    ),
):
    """Open a notebook file in a sandboxed environment. Optionally, specify a different base docker image.

    Changes to the notebook file will persist after the container shuts down.
    Quit the process with Ctrl-C or by shutting down jupyter from the browser.
    If a different base image is chosen, that image will be reused as the default for this notebook in the future.
    """
    # First figure out the name of the notebook and whether we need to create it
    need_to_create_notebook = False

    if path is None:
        need_to_create_notebook = True
        new_notebook_name = generate_botanical_filename()
        notebook_path = Path.cwd() / new_notebook_name
    else:
        notebook_path = path.resolve()
        if notebook_path.suffix != ".ipynb":
            typer.echo("File must be a jupyter notebook (.ipynb)")
            raise typer.Exit(1)

        if not notebook_path.exists():
            need_to_create_notebook = True

    # Adds notebook metadata cell if needed
    if not need_to_create_notebook:
        _add_notebook_metadata_cell(notebook_path)

    # Figure out what base image uri we should start the notebook in
    base_image_uri = _get_base_image_uri(
        base_image_name,
        custom_image_uri,
        None if need_to_create_notebook else notebook_path,
    )

    # Now we have all we need to prompt the user to proceed
    if need_to_create_notebook:
        message = f"This will create a new notebook {notebook_path.name} and open it in Docker image {base_image_uri}.\n"
    else:
        message = f"This will open existing notebook {notebook_path.name} in Docker image {base_image_uri}.\n"

    # Make sure requirements file is valid format
    if requirements_path:
        message += f"Additional dependencies specified in {requirements_path.name} will also be installed in {base_image_uri}.\n"
        message += "Any dependencies previously associated with this notebook will be overwritten by the new requirements.\n"
        _validate_requirements_path(requirements_path)

    # Get requirements data from either notebook or provided requirements path.
    # Could be None if requirements have not been set by the user.
    requirements_data = _read_requirements_data(requirements_path, notebook_path)

    typer.confirm(message + "Do you want to proceed?", abort=True)

    if need_to_create_notebook:
        if tutorial:
            template_file_name = "tutorial.ipynb"
        elif base_image_name:
            template_file_name = GardenConstants.IMAGES_TO_FLAVOR.get(
                base_image_name, "empty.ipynb"
            )
        else:
            template_file_name = "empty.ipynb"

        top_level_dir = Path(__file__).parent.parent
        source_path = top_level_dir / "notebook_templates" / template_file_name
        shutil.copy(source_path, notebook_path)

    # Update garden metadata in notebook
    _set_notebook_metadata(notebook_path, base_image_uri, requirements_data)

    print(
        f"Starting notebook inside base image with full name {base_image_uri}. "
        f"If you start this notebook again from the same folder, it will use this image by default."
    )

    with DockerClientSession(verbose=True) as docker_client:
        # Need to temporarily save requirments file for build_image_with_dependencies,
        # since requirements data could be coming from the notebook instead of a file.
        with TemporaryDirectory() as temp_dir:
            if requirements_data:
                temp_dir_path = Path(temp_dir)
                tmp_requirements_path = _save_requirements_data(
                    temp_dir_path, requirements_data
                )
            else:
                tmp_requirements_path = None

            # pre-bake local image with garden-ai and additional user requirements
            local_base_image_id = build_image_with_dependencies(
                docker_client, base_image_uri, tmp_requirements_path
            )
        # start container and listen for Ctrl-C
        container = start_container_with_notebook(
            docker_client, notebook_path, local_base_image_id, pull=False
        )
        _register_container_sigint_handler(container)

    typer.echo(
        f"Notebook started! Opening http://127.0.0.1:8888/notebooks/{notebook_path.name} "
        "in your default browser (you may need to refresh the page)"
    )

    # Give the notebook server a few seconds to start up so that the user doesn't have to refresh manually
    time.sleep(3)
    webbrowser.open_new_tab(f"http://127.0.0.1:8888/notebooks/{notebook_path.name}")

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


def _add_notebook_metadata_cell(
    notebook_path: Path,
):
    # Read notebook contents with nbformat
    try:
        ntbk = nbformat.read(notebook_path, as_version=4)
    except ValueError:
        typer.echo(f"Unable to parse notebook: {notebook_path}")
        raise typer.Exit(1)

    # Find cell with 'garden_metadata' tag
    for cell in ntbk.cells:
        cell_tags = cell.get("metadata", {}).get("tags", [])
        if "garden_metadata" in cell_tags:
            # if cell exists, exit here, don't need to add again.
            return

    # Was unable to find cell with garden_metadata tag, add new one to top of notebook
    # notebook_image_uri can be None here since notebook start will require the user provided a base image.
    metadata_template = NOTEBOOK_METADATA_CELL_TEMPLATE
    metadata_template += "notebook_image_uri = None\n"
    metadata_template += "notebook_requirements = None\n"

    # Add new cell with garden_metadata tag to top of notebook
    new_cell = nbformat.v4.new_code_cell(metadata_template)
    new_cell["metadata"] = {"tags": ["garden_metadata"]}
    del new_cell["id"]
    ntbk.cells.insert(0, new_cell)

    # Write updated notebook data to file
    nbformat.write(ntbk, notebook_path, version=nbformat.NO_CONVERT)


def _set_notebook_metadata(
    notebook_path: Path, base_image_name: str, requirements_data: Optional[dict]
):
    # Read notebook contents with nbformat
    try:
        ntbk = nbformat.read(notebook_path, as_version=nbformat.NO_CONVERT)
    except ValueError:
        typer.echo(f"Unable to parse notebook: {notebook_path}")
        raise typer.Exit(1)

    # Find cell with 'garden_metadata' tag
    for cell in ntbk.cells:
        cell_tags = cell.get("metadata", {}).get("tags", [])
        if "garden_metadata" in cell_tags:
            # Execute cell source to get metadata values
            notebook_metadata_cell_source = cell.get("source", "")
            metedata_namespace = types.SimpleNamespace()
            exec(notebook_metadata_cell_source, metedata_namespace.__dict__)

            # Replace base_image_name in cell source with provided
            metadata_template = NOTEBOOK_METADATA_CELL_TEMPLATE
            metadata_template += f'notebook_image_uri = "{base_image_name}"\n'

            # If requirements_data is not None, also replace previous requirements_data with new.
            if requirements_data:
                metadata_template += f"notebook_requirements = {json.dumps(requirements_data, indent=2)}\n"
            else:
                # Use metedata_namespace.notebook_requirements if is not None, otherwise just set to None
                if metedata_namespace.notebook_requirements:
                    metadata_template += f"notebook_requirements = {json.dumps(metedata_namespace.notebook_requirements, indent=2)}\n"
                else:
                    metadata_template += "notebook_requirements = None\n"

            # Replace old cell source with new metadata
            cell["source"] = metadata_template

    # Write updated notebook data to file
    nbformat.write(ntbk, notebook_path, version=nbformat.NO_CONVERT)


def _get_notebook_metadata(notebook_path: Path) -> dict:
    notebook_metadata: dict = {}
    notebook_metadata["notebook_image_uri"] = None
    notebook_metadata["notebook_requirements"] = None
    notebook_metadata_cell_source = None

    # Read notebook contents with nbformat
    try:
        ntbk = nbformat.read(notebook_path, as_version=nbformat.NO_CONVERT)
    except ValueError:
        typer.echo(f"Unable to parse notebook: {notebook_path}")
        raise typer.Exit(1)

    # Find cell with 'garden_metadata' tag and get source
    for cell in ntbk.cells:
        cell_tags = cell.get("metadata", {}).get("tags", [])
        if "garden_metadata" in cell_tags:
            notebook_metadata_cell_source = cell.get("source", None)
            break

    if not notebook_metadata_cell_source:
        typer.echo("Unable to find garden metadata cell.")
        return notebook_metadata

    # Run metadata cell source and get values
    metedata_namespace = types.SimpleNamespace()
    exec(notebook_metadata_cell_source, metedata_namespace.__dict__)

    if hasattr(metedata_namespace, "notebook_image_uri"):
        notebook_metadata["notebook_image_uri"] = metedata_namespace.notebook_image_uri
    if hasattr(metedata_namespace, "notebook_requirements"):
        notebook_metadata["notebook_requirements"] = (
            metedata_namespace.notebook_requirements
        )

    # Either notebook_image_uri or notebook_requirements could be None at this point.
    # _get_base_image_uri will exit if unable to find a uri from avalible data.
    # notebook_requirements is fine as None if no requirments were ever set.
    return notebook_metadata


def _read_requirements_data(
    requirements_path: Optional[Path],
    notebook_path: Path,
) -> Optional[dict]:
    # Notebook still needs to be created, return None
    if not notebook_path.is_file():
        return None

    requirements_data: dict = {}

    # Always use requirements from user provided requirements_path over any previously saved notebook requirements.
    if requirements_path:
        # For txt requirements files, contents is list of lines, format is pip
        if requirements_path.suffix in {".txt"}:
            requirements_data["format"] = "pip"
            with open(requirements_path, "r") as req_file:
                # read lines into list and strip any newlines
                file_contents = [
                    line.replace("\n", "") for line in req_file.readlines()
                ]
                req_file.close()
                requirements_data["contents"] = file_contents
            return requirements_data
        # For yaml requirements files, contents is safe_load dict of yaml file, format is conda
        elif requirements_path.suffix in {".yml", ".yaml"}:
            requirements_data["format"] = "conda"
            with open(requirements_path, "r") as req_file:
                file_contents = yaml.safe_load(req_file)
                req_file.close()
                requirements_data["contents"] = file_contents
            return requirements_data

    # If no path provided for requirements by user at start, use saved requirements instead.
    # Could be None if notebook_requirements was never set in notebooks metadata.
    return _get_notebook_metadata(notebook_path).get("notebook_requirements", None)


def _save_requirements_data(
    requirements_dir_path: Path, requirements_data: dict
) -> Optional[Path]:
    # Save requirements_data to requirements file
    # Inteanded to be run prior to build_image_with_dependencies with a tmpdir for requirements_dir_path
    # Returns path to new requirements file or None if was unable to write.
    file_format = requirements_data.get("format", None)
    contents = requirements_data.get("contents", None)

    # check that requirements_data at least has data for file_format and contents
    if contents and file_format:
        if file_format == "pip":
            # requirements file is txt
            requirements_path = requirements_dir_path / "requirements.txt"
            with open(requirements_path, "w") as req_file:
                # contents is list of requirements
                for line in contents:
                    req_file.write(f"{line}\n")
                req_file.close()
            return requirements_path

        elif file_format == "conda":
            # requirements file is yml
            requirements_path = requirements_dir_path / "requirements.yml"
            with open(requirements_path, "w") as req_file:
                # contents is dict of yaml requirements
                yaml.dump(contents, req_file, allow_unicode=True)
                req_file.close()
            return requirements_path
        else:
            typer.echo(
                f"Invalid format for requirements data, must be either pip or conda, got {file_format}. Ignoring requirements."
            )
            req_file.close()
            return None
    else:
        typer.echo("Invalid requirements data, ignoring requirements.")
        return None


def _get_base_image_uri(
    base_image_name: Optional[str],
    custom_image_uri: Optional[str],
    notebook_path: Optional[Path],
) -> str:
    # First make sure that we have enough information to get a base image uri
    if base_image_name and custom_image_uri:
        typer.echo(
            "You specified both a base image and a custom image. Please specify only one."
        )
        raise typer.Exit(1)

    if notebook_path:
        # last_used_image_uri could also be None here if not set in the notebooks metadata
        last_used_image_uri = _get_notebook_metadata(notebook_path).get(
            "notebook_image_uri", None
        )
    else:
        last_used_image_uri = None

    if not any([base_image_name, custom_image_uri, last_used_image_uri]):
        typer.echo(
            f"Please specify a base image. The current Garden base images are: \n{BASE_IMAGE_NAMES}"
        )
        raise typer.Exit(1)

    # Now use precedence rules to get the base image uri
    # 1: --custom-image wins if specified
    if custom_image_uri:
        return custom_image_uri

    # 2: then go off of --base-image
    if base_image_name:
        if base_image_name in GardenConstants.PREMADE_IMAGES:
            return GardenConstants.PREMADE_IMAGES[base_image_name]
        else:
            typer.echo(
                f"The image you specified ({base_image_name}) is not one of the Garden base images. "
                f"The current Garden base images are: \n{BASE_IMAGE_NAMES}"
            )
            raise typer.Exit(1)

    # last_used_image_uri is definitely non-None at this point
    last_used_image_uri = cast(str, last_used_image_uri)

    # 3: If the user didn't specify an image explicitly, use the last image they used for this notebook.
    return last_used_image_uri


def _register_container_sigint_handler(container: docker.models.containers.Container):
    """helper: ensure SIGINT/ Ctrl-C to our CLI stops a given container"""
    import signal

    def handler(signal, frame):
        typer.echo("Stopping notebook...")
        container.stop()
        return

    signal.signal(signal.SIGINT, handler)
    return


@notebook_app.command(no_args_is_help=True)
def debug(
    path: Path = typer.Argument(
        ...,
        file_okay=True,
        dir_okay=False,
        writable=True,
        readable=True,
        exists=True,
        help=(
            "Path to a .ipynb notebook whose remote environment will be approximated for debugging."
        ),
    ),
    requirements_path: Optional[Path] = typer.Option(
        None,
        "--requirements",
        help=(
            "Path to a requirements.txt or a conda environment.yml containing "
            "additional dependencies to install in the base image."
        ),
    ),
):
    """Open the debugging notebook in a pre-prepared container.

    Changes to the notebook file will NOT persist after the container shuts down.
    Quit the process with Ctrl-C or by shutting down jupyter from the browser.
    """

    with DockerClientSession(verbose=True) as docker_client:
        base_image_uri = (
            _get_base_image_uri(
                base_image_name=None, custom_image_uri=None, notebook_path=path
            )
            or "gardenai/base:python-3.10-jupyter"
        )

        # Make sure requirements file is valid format
        if requirements_path:
            _validate_requirements_path(requirements_path)

        # Get requirements data from either notebook or provided requirements path.
        # Could be None if requirements have not been set by the user.
        requirements_data = _read_requirements_data(requirements_path, path)

        with TemporaryDirectory() as temp_dir:
            # Need to temporarily save requirments file for build_image_with_dependencies,
            # since requirements data could be coming from the notebook instead of a file.
            if requirements_data:
                temp_dir_path = Path(temp_dir)
                tmp_requirements_path = _save_requirements_data(
                    temp_dir_path, requirements_data
                )
            else:
                tmp_requirements_path = None

            # pre-bake local image with garden-ai and additional user requirements
            local_base_image_id = build_image_with_dependencies(
                docker_client, base_image_uri, tmp_requirements_path
            )

            image = build_notebook_session_image(
                docker_client,
                path,
                local_base_image_id,
            )

            if image is None:
                typer.echo("Failed to build image.")
                raise typer.Exit(1)
            image_name = str(image.id)  # str used to guarantee type-check

            top_level_dir = Path(__file__).parent.parent
            debug_path = top_level_dir / "notebook_templates" / "debug.ipynb"

            # Make tmp copy of debug notebook to add original notebook's metadata too
            tmp_debug_path = temp_dir_path / "debug.ipynb"
            shutil.copy(debug_path, tmp_debug_path)
            _set_notebook_metadata(tmp_debug_path, base_image_uri, requirements_data)

            container = start_container_with_notebook(
                docker_client, tmp_debug_path, image_name, mount=False, pull=False
            )
            _register_container_sigint_handler(container)

    typer.echo(
        f"Notebook started! Opening http://127.0.0.1:8888/notebooks/{debug_path.name} "
        "in your default browser (you may need to refresh the page)"
    )
    webbrowser.open_new_tab(f"http://127.0.0.1:8888/notebooks/{debug_path.name}")

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


@notebook_app.command(no_args_is_help=True)
def publish(
    path: Path = typer.Argument(
        ...,
        file_okay=True,
        dir_okay=False,
        writable=True,
        readable=True,
    ),
    requirements_path: Optional[Path] = typer.Option(
        None,
        "--requirements",
        help=(
            "Path to a requirements.txt or a conda environment.yml containing "
            "additional dependencies to install in the base image."
        ),
    ),
    base_image_name: Optional[str] = typer.Option(
        None,
        "--base-image",
        help=(
            "A Garden base image to run your notebook inside of. This will be the foundation for the image that runs your entrypoints."
            "For example, to run on top of the default Garden python 3.8 image, use --base-image 3.8-base. "
            "To see all the available Garden base images, use 'garden-ai notebook list-premade-images'"
        ),
    ),
    custom_image_uri: Optional[str] = typer.Option(
        None,
        "--custom-image",
        help=(
            "Power users only! Provide a uri of a publicly available docker image to boot the notebook in."
        ),
        hidden=True,
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
    keep_outputs: bool = typer.Option(
        False,
        "--keep-outputs",
        help="By default, Garden will clear all cell outputs before publishing. "
        "If you would like to have your cell outputs visible on the UI, use this flag.",
    ),
):
    client = GardenClient()
    notebook_path = path.resolve()
    if notebook_path.suffix != ".ipynb":
        raise ValueError("File must be a jupyter notebook (.ipynb)")
    if not notebook_path.exists():
        raise ValueError(f"Could not find file at {notebook_path}")

    # Publish should not change the metadata of users local notebook if user provided different requirements / base image.
    # So we use a tmpdir with a copy of the original notebook for publish.
    with TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        tmp_notebook_path = temp_dir_path / notebook_path.name
        shutil.copy(notebook_path, tmp_notebook_path)

        base_image_uri = _get_base_image_uri(
            base_image_name, custom_image_uri, tmp_notebook_path
        )
        print(f"Using base image: {base_image_uri}")

        # Make sure requirements file is valid format
        if requirements_path:
            _validate_requirements_path(requirements_path)

        # Get requirements data from either notebook or provided requirements path.
        # Could be None if requirements have not been set by the user.
        requirements_data = _read_requirements_data(
            requirements_path, tmp_notebook_path
        )

        # Update garden metadata in tmp notebook
        _set_notebook_metadata(tmp_notebook_path, base_image_uri, requirements_data)

        # Pre-process the notebook and make sure it's not too big
        raw_notebook_contents = tmp_notebook_path.read_text()
        try:
            notebook_contents = json.loads(raw_notebook_contents)
        except json.JSONDecodeError:
            typer.echo("Could not parse notebook JSON.")
            raise typer.Exit(1)

        if not keep_outputs:
            notebook_contents = clear_cells(notebook_contents)

        if is_over_size_limit(notebook_contents):
            typer.echo("Garden can't publish notebooks bigger than 5MB.")
            raise typer.Exit(1)

        # Push the notebook to the Garden API
        notebook_url = client.upload_notebook(notebook_contents, tmp_notebook_path.name)

        with DockerClientSession(verbose=verbose) as docker_client:
            # Need to temporarily save requirments file for build_image_with_dependencies,
            # since requirements data could be coming from the notebook instead of a file.
            if requirements_data:
                tmp_requirements_path = _save_requirements_data(
                    temp_dir_path, requirements_data
                )
            else:
                tmp_requirements_path = None

            # Build the image
            local_base_image_id = build_image_with_dependencies(
                docker_client,
                base_image_uri,
                tmp_requirements_path,
                print_logs=verbose,
                pull=True,
            )

            image = build_notebook_session_image(
                docker_client,
                tmp_notebook_path,
                local_base_image_id,
                print_logs=verbose,
            )

            if image is None:
                typer.echo("Failed to build image.")
                raise typer.Exit(1)
            typer.echo(f"Built image: {image}")

        # push image to ECR
        auth_config = client._get_auth_config_for_ecr_push()

        typer.echo(f"Pushing image to repository: {GardenConstants.GARDEN_ECR_REPO}")
        full_image_uri = push_image_to_public_repo(
            docker_client, image, auth_config, print_logs=verbose
        )
        typer.echo(f"Successfully pushed image to: {full_image_uri}")

        metadata = extract_metadata_from_image(docker_client, image)
        client._register_and_publish_from_user_image(
            base_image_uri, full_image_uri, notebook_url, metadata
        )


def _validate_requirements_path(requirements_path: Path):
    requirements_path.resolve()
    if not requirements_path.exists():
        typer.echo(f"Could not find file: {requirements_path}")
        raise typer.Exit(1)
    if requirements_path.suffix not in {".txt", ".yml", ".yaml"}:
        typer.echo(
            "Requirements file in unexpected format. "
            f"Expected one of: .txt, .yml, .yaml; got {requirements_path.name}. "
        )
        raise typer.Exit(1)
    return
