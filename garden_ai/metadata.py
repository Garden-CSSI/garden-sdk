from string import Template
from pathlib import Path
from typing import Optional
import re
import typer
import yaml
import json
import nbformat

from nbformat.notebooknode import NotebookNode  # type: ignore


NOTEBOOK_METADATA_CELL_TEMPLATE = Template(
    "# This cell is auto-generated by Garden. Don't delete it. Do keep it as the first cell.\n"
    "# It records the base image and requirements you passed to `garden-ai notebook start`.\n"
    "# That way the next time you run this notebook Garden can start it with the same libraries.\n\n"
    '"""\n'
    "NOTEBOOK_METADATA = $metadata\n"
    '"""'
)


def add_notebook_metadata_cell(
    notebook_path: Path,
):
    ntbk = _read_notebook(notebook_path)

    # Find cell with 'garden_metadata_cell' tag
    for cell in ntbk.cells:
        cell_tags = cell.get("metadata", {}).get("tags", [])
        if "garden_metadata_cell" in cell_tags:
            # if cell exists, exit here, don't need to add again.
            return

    # Was unable to find cell with garden_metadata_cell tag, add new one to top of notebook
    # notebook_image_uri can be None here since notebook start will require the user provided a base image.
    metadata_string = json.dumps(
        {"NOTEBOOK_BASE_IMAGE_NAME": None, "NOTEBOOK_REQUIREMENTS": None}, indent=2
    )
    notebook_metadata = NOTEBOOK_METADATA_CELL_TEMPLATE.substitute(
        metadata=metadata_string
    )

    # Add new cell with garden_metadata_cell tag to top of notebook
    new_cell = nbformat.v4.new_code_cell(notebook_metadata)
    new_cell["metadata"] = {
        "tags": ["garden_metadata_cell"],
        "garden_metadata": {"NOTEBOOK_BASE_IMAGE_URI": None},
    }
    del new_cell["id"]
    ntbk.cells.insert(0, new_cell)

    # Write updated notebook data to file
    nbformat.write(ntbk, notebook_path, version=nbformat.NO_CONVERT)


def set_notebook_metadata(
    notebook_path: Path,
    base_image_name: Optional[str],
    base_image_uri: str,
    requirements_data: Optional[dict],
):
    ntbk = _read_notebook(notebook_path)

    # Find cell with 'garden_metadata_cell' tag
    for cell in ntbk.cells:
        cell_tags = cell.get("metadata", {}).get("tags", [])
        if "garden_metadata_cell" in cell_tags:
            # Replace old cell source with new metadata
            metadata_string = json.dumps(
                {
                    "NOTEBOOK_BASE_IMAGE_NAME": base_image_name,
                    "NOTEBOOK_REQUIREMENTS": requirements_data,
                },
                indent=2,
            )

            cell["source"] = NOTEBOOK_METADATA_CELL_TEMPLATE.substitute(
                metadata=metadata_string
            )

            # Add hidden metadata
            cell["metadata"]["garden_metadata"][
                "NOTEBOOK_BASE_IMAGE_URI"
            ] = base_image_uri

    # Write updated notebook data to file
    nbformat.write(ntbk, notebook_path, version=nbformat.NO_CONVERT)


def get_notebook_metadata(notebook_path: Path) -> dict:
    notebook_metadata: dict = {}
    notebook_metadata["notebook_image_name"] = None
    notebook_metadata["notebook_requirements"] = None
    notebook_metadata_cell_source = None
    notebook_metadata_string = None

    ntbk = _read_notebook(notebook_path)

    # Find cell with 'garden_metadata_cell' tag and get source
    for cell in ntbk.cells:
        cell_tags = cell.get("metadata", {}).get("tags", [])
        if "garden_metadata_cell" in cell_tags:
            # Grab cell source and hidden metadata
            notebook_metadata_cell_source = cell.get("source", None)
            notebook_hidden_metadata = cell.get("metadata", {}).get(
                "garden_metadata", {}
            )
            break

    # Return empty notebook metadata dict if was unable to find cell source
    if not notebook_metadata_cell_source:
        typer.echo("Unable to find garden metadata cell.")
        return notebook_metadata

    # Grab the part of cell source with the metadata dict in it
    clean_source = notebook_metadata_cell_source.replace("\n", "")
    regex_get_metadata = r"^(.*?)\"\"\"(.*?)NOTEBOOK_METADATA(.*?)=(.*?)\"\"\"(.*?)$"

    metadata_match = re.match(regex_get_metadata, clean_source)
    if metadata_match and len(metadata_match.groups()) == 5:
        notebook_metadata_string = metadata_match[4]

    if notebook_metadata_string:
        notebook_metadata_dict = json.loads(notebook_metadata_string.strip())
        notebook_metadata["notebook_image_name"] = notebook_metadata_dict.get(
            "NOTEBOOK_BASE_IMAGE_NAME", None
        )
        notebook_metadata["notebook_requirements"] = notebook_metadata_dict.get(
            "NOTEBOOK_REQUIREMENTS", None
        )
        notebook_metadata["notebook_image_uri"] = notebook_hidden_metadata.get(
            "NOTEBOOK_BASE_IMAGE_URI", None
        )

    return notebook_metadata


def read_requirements_data(
    requirements_path: Optional[Path],
    notebook_path: Path,
) -> Optional[dict]:
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

    # Notebook still needs to be created and no requirements file was given, return None
    if not notebook_path.is_file():
        return None

    # No requirements file was given, but notebook exists
    # So try to get requirements from saved notebook metadata
    return get_notebook_metadata(notebook_path).get("notebook_requirements", None)


def save_requirements_data(
    requirements_dir_path: Path, requirements_data: dict
) -> Optional[Path]:
    # Save requirements_data to requirements file in either pip or conda format
    # Returns path to new requirements file or None if was unable to write.
    file_format = requirements_data.get("format", None)
    contents = requirements_data.get("contents", None)

    # check that requirements_data has data for file_format and contents
    if contents and file_format:
        if file_format == "pip":
            # requirements file is txt
            requirements_path = requirements_dir_path / "requirements.txt"
            with open(requirements_path, "w") as req_file:
                # contents is list of requirements
                file_contents = ""
                for line in contents:
                    file_contents += f"{line}\n"
                req_file.write(file_contents)
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


def _read_notebook(notebook_path: Path) -> NotebookNode:
    # Read notebook contents with nbformat
    try:
        ntbk = nbformat.read(notebook_path, as_version=4)
        return ntbk
    except ValueError:
        typer.echo(f"Unable to parse notebook: {notebook_path}")
        raise typer.Exit(1)
