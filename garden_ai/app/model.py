from pathlib import Path
from typing import Optional, List

from garden_ai.client import GardenClient
from garden_ai.mlmodel import DatasetConnection, LocalModel, ModelFlavor
from garden_ai import local_data

import typer
import rich
import logging

model_app = typer.Typer(name="model", no_args_is_help=True)

logger = logging.getLogger()


@model_app.callback()
def model():
    """
    sub-commands for managing machine learning models
    """
    pass


@model_app.command(no_args_is_help=True)
def register(
    name: str = typer.Argument(
        ...,
        help=("The name of your model"),
    ),
    model_path: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=True,
        file_okay=True,
        writable=True,
        readable=True,
        resolve_path=True,
        help=("The path to your model on your filesystem"),
    ),
    flavor: str = typer.Argument(
        "sklearn",
        help=(
            "What ML library did you make the model with? "
            "Currently we support the following flavors 'sklearn', 'tensorflow', and 'pytorch'."
        ),
    ),
    extra_pip_requirements: Optional[List[str]] = typer.Option(
        None,
        "--extra-pip-requirements",
        "-r",
        help=(
            "Additonal package requirmeents. Add multiple like "
            '--extra-pip-requirements "torch=1.3.1" --extra-pip-requirements "pandas<=1.5.0"'
        ),
    ),
    dataset_url: Optional[str] = typer.Option(
        None,
        "--dataset-url",
        help=(
            "If you trained this model on a Foundry dataset, include a link to the dataset with this option"
        ),
    ),
    dataset_doi: Optional[str] = typer.Option(
        None,
        "--dataset-doi",
        help=(
            "If you trained this model on a Foundry dataset, include the doi of the dataset"
        ),
    ),
):
    """Register a model in Garden. Outputs a full model identifier that you can reference in a Pipeline."""
    if flavor not in [f.value for f in ModelFlavor]:
        raise typer.BadParameter(
            f"Sorry, we only support 'sklearn', 'tensorflow', and 'pytorch'. The {flavor} flavor is not yet supported."
        )

    only_one_dataset_option_provided = (dataset_url and not dataset_doi) or (
        dataset_doi and not dataset_url
    )
    if only_one_dataset_option_provided:
        raise typer.BadParameter(
            "If you are linking a Foundry dataset, please include both --dataset-url and --dataset-doi"
        )

    client = GardenClient()
    local_model = LocalModel(
        local_path=str(model_path),
        model_name=name,
        flavor=flavor,
        extra_pip_requirements=extra_pip_requirements,
        user_email=client.get_email(),
    )
    if dataset_doi and dataset_url:
        dataset_metadata = DatasetConnection(doi=dataset_doi, url=dataset_url)
        local_model.connections.append(dataset_metadata)

    registered_model = client.register_model(local_model)
    model_uri = registered_model.model_uri
    rich.print(
        f"Successfully uploaded your model! The full name to include in your pipeline is '{model_uri}'"
    )


@model_app.command(no_args_is_help=False)
def list():
    """Lists all local models."""

    console = rich.console.Console()
    console.print("\n")
    table = rich.table.Table(title="Local Models")
    data, fields = local_data.get_local_model_data(fields=["model_name", "flavor"])
    for f in fields:
        table.add_column(f)
    for d in data:
        table.add_row(*(d))
    console.print(table)


@model_app.command(no_args_is_help=True)
def show(
    model_id: str = typer.Option(
        ...,
        "-m",
        "--model",
        prompt="Please enter the URI of a model",
        help="The Model URI of the model you want to show",
        rich_help_panel="Required",
    ),
):
    """Shows all info for one model"""

    model_json = local_data.get_local_model_json(model_id)
    if not model_json:
        logger.fatal(f"Could not find model with URI {model_id}")
        raise typer.Exit(code=1)
    rich.print_json(data=model_json)
