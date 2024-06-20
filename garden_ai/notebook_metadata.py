from pathlib import Path
from typing import Optional, Union
from pydantic import BaseModel, ValidationError
import typer
import yaml
import json
import os
import sys
import subprocess

import ipywidgets as widgets  # type: ignore
import IPython
from IPython.display import display

import nbformat

from nbformat.notebooknode import NotebookNode  # type: ignore


NOTEBOOK_DISPLAY_METADATA_CELL = (
    '"""\n'
    "This cell is auto-generated by Garden. Don't delete it. Do keep it as the first cell.\n"
    "You can use this widget to edit your notebooks metadata. \n"
    "That way the next time you run this notebook, Garden can start it with the same enviorment.\n"
    "Any changes made to your notebook's metadata using the widget will be saved when the notebook is saved.\n\n"
    "Notebook metadata fields:\n"
    "- Global DOI: The DOI of a Garden you want to add all entrypoints in this notebook too.\n"
    "If you want to specify a differnt Garden DOI for individual entrypoints, you can provide that entrypoint's\n"
    "'garden_entrypoint' decorator with the optional 'garden_doi' argument. Providing the decorator with a DOI\n"
    "will override the Global DOI for that specific entrypoint.\n"
    "- Base image name: The name of the garden base image you want to start this notebook with.\n"
    "To see a list of the available Garden base images, use 'garden-ai notebook list-premade-images'\n"
    "- Requirements: Any additional requirements that should be installed in this notebook's container.\n"
    "After making changes to your notebook's requirements, the widget will show a 'Install new requirements' button\n"
    "that installs the new requirements to the container, restarts the jupyter kernel and \n"
    "updates your local requirements file if one was provided.\n"
    '"""\n\n'
    "from garden_ai.notebook_metadata import display_metadata_widget\n"
    "display_metadata_widget()"
)

METADATA_CELL_TAG = "garden_display_metadata_cell"


class RequirementsData(BaseModel):
    file_format: str
    contents: Union[dict, list]


class NotebookMetadata(BaseModel):
    global_notebook_doi: Optional[str]
    notebook_image_name: Optional[str]
    notebook_image_uri: Optional[str]
    notebook_requirements: Optional[RequirementsData]


def add_notebook_metadata(
    notebook_path: Path,
):
    """
    Adds metadata editor widget cell to top of the notebook if missing
    Adds empty `garden_metadata` dict to the notebook's metadata if missing
    """

    ntbk = _read_notebook(notebook_path)

    # Find cell with 'garden_display_metadata_cell' tag
    found_cell = False
    for cell in ntbk.cells:
        cell_tags = cell.get("metadata", {}).get("tags", [])
        if METADATA_CELL_TAG in cell_tags:
            found_cell = True

    # If metadata widget cell does not exist, add to top of notebook
    if not found_cell:
        new_cell = nbformat.v4.new_code_cell(NOTEBOOK_DISPLAY_METADATA_CELL)
        new_cell["metadata"] = {
            "tags": [METADATA_CELL_TAG],
        }
        del new_cell["id"]
        ntbk.cells.insert(0, new_cell)

    # Add empty garden_metadata dict to notebooks metadata if missing
    if "garden_metadata" not in ntbk["metadata"]:
        ntbk["metadata"]["garden_metadata"] = {}
    # If some of the fields are set, dont want to write over them
    for field in list(NotebookMetadata.model_fields):
        if field not in ntbk["metadata"]["garden_metadata"]:
            ntbk["metadata"]["garden_metadata"][field] = None

    # Write updated notebook data to file
    nbformat.write(ntbk, notebook_path, version=nbformat.NO_CONVERT)


def get_notebook_metadata(notebook_path: Path) -> NotebookMetadata:
    ntbk = _read_notebook(notebook_path)

    # Return empty notebook metadata dict if was unable to find cell source
    if "garden_metadata" not in ntbk["metadata"]:
        typer.echo("Unable to find garden metadata.")
        return NotebookMetadata(
            global_notebook_doi=None,
            notebook_image_name=None,
            notebook_image_uri=None,
            notebook_requirements=None,
        )

    try:
        return NotebookMetadata.parse_obj(ntbk["metadata"]["garden_metadata"])
    except ValidationError:
        typer.echo("Unable to parse garden metadata cell.")
        return NotebookMetadata(
            global_notebook_doi=None,
            notebook_image_name=None,
            notebook_image_uri=None,
            notebook_requirements=None,
        )


def set_notebook_metadata(
    notebook_path: Path,
    notebook_global_doi: Optional[str],
    base_image_name: Optional[str],
    base_image_uri: str,
    requirements_data: Optional[RequirementsData],
):
    ntbk = _read_notebook(notebook_path)

    ntbk["metadata"]["garden_metadata"] = NotebookMetadata(
        global_notebook_doi=notebook_global_doi,
        notebook_image_name=base_image_name,
        notebook_image_uri=base_image_uri,
        notebook_requirements=requirements_data,
    ).model_dump()

    # Write updated notebook data to file
    nbformat.write(ntbk, notebook_path, version=nbformat.NO_CONVERT)


def read_requirements_data(requirements_path: Path) -> Optional[RequirementsData]:
    # For txt requirements files, contents is list of lines, format is pip
    if requirements_path.suffix in {".txt"}:
        file_format = "pip"
        with open(requirements_path, "r") as req_file:
            # read lines into list and strip any newlines
            file_contents = [line.replace("\n", "") for line in req_file.readlines()]
            req_file.close()
        return RequirementsData(file_format=file_format, contents=file_contents)
    # For yaml requirements files, contents is safe_load dict of yaml file, format is conda
    elif requirements_path.suffix in {".yml", ".yaml"}:
        file_format = "conda"
        with open(requirements_path, "r") as req_file:
            file_contents = yaml.safe_load(req_file)
            req_file.close()
        return RequirementsData(file_format=file_format, contents=file_contents)
    else:
        typer.echo("Invalid requirements file format.")
        return None


def save_requirements_data(
    requirements_dir_path: Path, requirements_data: RequirementsData
) -> Optional[Path]:
    # Save requirements_data to requirements file in either pip or conda format
    # Returns path to new requirements file or None if was unable to write.
    file_format = requirements_data.file_format
    contents = requirements_data.contents

    if file_format == "pip":
        # requirements file is txt
        requirements_path = requirements_dir_path / "requirements.txt"
        with open(requirements_path, "w") as req_file:
            # contents is list of requirements
            file_contents = ""
            for line in contents:
                file_contents += f"{line}\n"
            req_file.write(file_contents)
        return requirements_path

    elif file_format == "conda":
        # requirements file is yml
        requirements_path = requirements_dir_path / "requirements.yml"
        with open(requirements_path, "w") as req_file:
            # contents is dict of yaml requirements
            yaml.dump(contents, req_file, allow_unicode=True)
        return requirements_path
    else:
        typer.echo(
            f"Invalid format for requirements data, must be either pip or conda, got {file_format}. Ignoring requirements."
        )
        return None


def display_metadata_widget():
    """
    Displays the metadata editor widget
    When one of the widgets fields is changed, pickles and saves the updated NotebookMetadata.
    When the notebook is saved, the post_save_hook in custom_jupyter_config will
    go and look for the pickled NotebookMetadata and save it to the notebooks metadata.
    """
    from garden_ai.app.console import console

    # NOTEBOOK_PATH env var set in start_container_with_notebook
    notebook_path = Path(os.environ["NOTEBOOK_PATH"])
    nb_meta = get_notebook_metadata(notebook_path)

    output = widgets.Output()

    # Global DOI widget
    doi_widget = widgets.Textarea(
        value=nb_meta.global_notebook_doi,
        placeholder="Global DOI",
        continuous_update=False,
        disabled=False,
    )

    # Base image name widget
    base_image_widget = widgets.Textarea(
        value=nb_meta.notebook_image_name,
        placeholder="Base image name",
        continuous_update=False,
        disabled=False,
    )

    # Requirements widget
    if nb_meta.notebook_requirements.file_format == "pip":
        reqs_string = "\n".join([req for req in nb_meta.notebook_requirements.contents])
    else:
        # ignoring conda requirements, since we are planning to remove support for them anyways
        reqs_string = ""

    reqs_widget = widgets.Textarea(
        value=reqs_string,
        placeholder="Requirements",
        layout=widgets.Layout(width="100%", height="80px"),
        continuous_update=False,
        disabled=False,
    )

    update_reqs_widget = widgets.Button(
        description="Install new requirements",
        style=widgets.ButtonStyle(button_color="lightgreen", font_weight="bold"),
        layout=widgets.Layout(width="100%", height="50px", border="1px solid black"),
    )

    accordion_widget = widgets.Accordion(
        children=[doi_widget, base_image_widget, reqs_widget],
        titles=("Global DOI", "Base Image", "Requirements"),
    )

    metadata_widget = widgets.VBox(
        children=[accordion_widget],
    )

    def doi_observer(change):
        with output:
            nonlocal nb_meta
            nb_meta.global_notebook_doi = change.new.strip()
            if nb_meta.global_notebook_doi == "":
                nb_meta.global_notebook_doi = None
            _save_metadata_as_json(nb_meta)

    doi_widget.observe(doi_observer, "value")

    def base_image_observer(change):
        with output:
            nonlocal nb_meta
            nb_meta.notebook_image_name = change.new.strip()
            if nb_meta.notebook_image_name == "":
                nb_meta.notebook_image_name = None
            _save_metadata_as_json(nb_meta)

    base_image_widget.observe(base_image_observer, "value")

    def reqs_observer(change):
        with output:
            nonlocal nb_meta
            nb_meta.notebook_requirements.contents = change.new.split("\n")

            if "" in nb_meta.notebook_requirements.contents:
                nb_meta.notebook_requirements.contents.remove("")
            _save_metadata_as_json(nb_meta)
            if update_reqs_widget not in metadata_widget.children:
                metadata_widget.children = list(metadata_widget.children) + [
                    update_reqs_widget
                ]

    reqs_widget.observe(reqs_observer, "value")

    def update_reqs_observer(button):
        with output:
            nonlocal nb_meta
            # save changes to requirements file
            # REQUIREMENTS_PATH env var set in start_container_with_notebook
            reqs_path = Path(os.environ["REQUIREMENTS_PATH"])
            save_requirements_data(reqs_path, nb_meta.notebook_requirements)

            # pip install new requirements file
            with console.status("[bold green] Installing new libraries..."):
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", "-r", reqs_path]
                )

            # restart jupyter kernel
            IPython.Application.instance().kernel.do_shutdown(True)

            # remove update button from metadata_widget
            new_children = list(metadata_widget.children)
            new_children.remove(update_reqs_widget)
            metadata_widget.children = new_children

    update_reqs_widget.on_click(update_reqs_observer)

    display(metadata_widget, output)


def _save_metadata_as_json(nb_meta: NotebookMetadata):
    with open("./notebook_metadata.json", "w") as file:
        json.dump(nb_meta.model_dump(), file)


def _read_notebook(notebook_path: Path) -> NotebookNode:
    # Read notebook contents with nbformat
    try:
        ntbk = nbformat.read(notebook_path, as_version=4)
        return ntbk
    except ValueError:
        typer.echo(f"Unable to parse notebook: {notebook_path}")
        raise typer.Exit(1)
