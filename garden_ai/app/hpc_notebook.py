import logging

import typer

import os

import subprocess
import tempfile


logger = logging.getLogger()

hpc_notebook_app = typer.Typer(name="hpc-notebook")


@hpc_notebook_app.callback(no_args_is_help=True)
def hpc_notebook():
    """sub-commands for editing and publishing from sandboxed notebooks in HPC."""
    pass


@hpc_notebook_app.command()
def rerun(
    notebooks_dir: str = typer.Option(..., help="Directory to bind for notebooks."),
):
    root = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(root)
    root = os.path.dirname(root)
    container_image = os.path.join(root, "hpc-notebook.sif")
    if not os.path.exists(container_image):
        logger.error("Not found.")
        typer.echo("Not found")
    else:
        working_directory = tempfile.mkdtemp()

        tmp_dir = os.path.join(working_directory, "tmp")
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        os.makedirs(tmp_dir, exist_ok=True)

        # Step 2: Set environment variables
        os.environ["SINGULARITY_TMPDIR"] = tmp_dir
        os.environ["APPTAINER_TMPDIR"] = tmp_dir
        os.environ["JUPYTER_RUNTIME_DIR"] = tmp_dir
        os.environ["JUPYTER_DATA_DIR"] = tmp_dir
        os.environ["JUPYTER_CONFIG_DIR"] = tmp_dir
        run_command = [
            "apptainer",
            "run",
            "--bind",
            f"{notebooks_dir}:/notebooks",
            container_image,
            "jupyter",
            "notebook",
            "--no-browser",
            "--ip=0.0.0.0",
        ]
        subprocess.run(run_command, check=True)
        logger.info("Jupyter Notebook started successfully in the Apptainer container.")


@hpc_notebook_app.command()
def start(
    notebooks_dir: str = typer.Option(..., help="Directory to bind for notebooks."),
):
    """Open a notebook file in HPC."""

    script_dir = os.path.dirname(os.path.abspath(__file__))
    script_dir = os.path.dirname(script_dir)
    script_dir = os.path.dirname(script_dir)
    container_image = os.path.join(script_dir, "hpc-notebook.sif")
    definition_file = os.path.join(script_dir, "scripts", "Singularity.def")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(script_dir, ".."))
    definition_file = os.path.abspath(os.path.join(project_root, definition_file))

    if not os.path.exists(notebooks_dir):
        logger.info(f"Notebooks directory {notebooks_dir} does not exist. Creating it.")
        os.makedirs(notebooks_dir)

    # Step 1: Create temporary directory if it doesn't exist
    working_directory = tempfile.mkdtemp()

    tmp_dir = os.path.join(working_directory, "tmp")
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)

    # Step 2: Set environment variables
    os.environ["SINGULARITY_TMPDIR"] = tmp_dir
    os.environ["APPTAINER_TMPDIR"] = tmp_dir
    os.environ["JUPYTER_RUNTIME_DIR"] = tmp_dir
    os.environ["JUPYTER_DATA_DIR"] = tmp_dir
    os.environ["JUPYTER_CONFIG_DIR"] = tmp_dir

    try:
        # Ensure the definition file exists
        if not os.path.isfile(definition_file):
            logger.error(f"Definition file {definition_file} not found.")
            typer.echo(f"🚧🌱🚧 Definition file {definition_file} not found 🚧🌱🚧")
            raise typer.Exit(code=1)

        # Step 3: Build the Apptainer container
        build_command = ["apptainer", "build", container_image, definition_file]
        subprocess.run(build_command, check=True)
        logger.info("Apptainer container built successfully.")

        # Step 4: Run the Apptainer container and start Jupyter Notebook
        run_command = [
            "apptainer",
            "run",
            "--bind",
            f"{notebooks_dir}:/notebooks",
            container_image,
            "jupyter",
            "notebook",
            "--no-browser",
            "--ip=0.0.0.0",
        ]
        subprocess.run(run_command, check=True)
        logger.info("Jupyter Notebook started successfully in the Apptainer container.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to start Jupyter Notebook: {e}")
        typer.echo("🚧🌱🚧 Failed to start Jupyter Notebook 🚧🌱🚧")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        typer.echo("🚧🌱🚧 An unexpected error occurred 🚧🌱🚧")


@hpc_notebook_app.command()
def publish():
    """Publish your hpc-notebook."""
    print("🚧🌱🚧 Under Construction 🚧🌱🚧")
