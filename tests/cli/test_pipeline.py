import string
import random
from keyword import iskeyword

import pytest
from typer.testing import CliRunner

from garden_ai.app.main import app
from garden_ai.utils.misc import clean_identifier
from garden_ai.client import GardenClient

runner = CliRunner()


@pytest.mark.cli
def test_pipeline_add_paper(database_with_connected_pipeline, mocker):
    from garden_ai import local_data

    mocker.patch(
        "garden_ai.local_data.LOCAL_STORAGE", new=database_with_connected_pipeline
    )
    pipeline_doi = "10.23677/jx31-gx98"

    before_addition = local_data.get_local_pipeline_by_doi(pipeline_doi)
    assert len(before_addition.papers) == 0
    command = [
        "pipeline",
        "add-paper",
        "--doi",
        str(pipeline_doi),
        "--title",
        "This is not a real paper",
        "--paper-doi",
        "ab-cdef/12345",
        "--citation",
        "Citation Test",
    ]
    for name in before_addition.authors:
        command += ["--author", name]

    result = runner.invoke(app, command)
    assert result.exit_code == 0

    after_addition = local_data.get_local_pipeline_by_doi(pipeline_doi)
    first_paper = after_addition.papers[0]
    assert len(after_addition.papers) != 0
    assert first_paper.title == "This is not a real paper"
    assert first_paper.authors == before_addition.authors
    assert first_paper.doi == "ab-cdef/12345"


@pytest.mark.cli
def test_pipeline_add_repository(database_with_connected_pipeline, mocker):
    from garden_ai import local_data

    mocker.patch(
        "garden_ai.local_data.LOCAL_STORAGE", new=database_with_connected_pipeline
    )
    pipeline_doi = "10.23677/jx31-gx98"

    before_addition = local_data.get_local_pipeline_by_doi(pipeline_doi)
    assert len(before_addition.repositories) == 0
    command = [
        "pipeline",
        "add-repository",
        "--doi",
        str(pipeline_doi),
        "--url",
        "https://fakerepository-link.org",
        "--repository_name",
        "Fake repository",
        "--contributor",
        "Person1",
        "--contributor",
        "Person2",
        "--contributor",
        "Person3",
    ]
    result = runner.invoke(app, command)
    assert result.exit_code == 0

    after_addition = local_data.get_local_pipeline_by_doi(pipeline_doi)
    first_repo = after_addition.repositories[0]
    assert len(after_addition.repositories) == 1
    assert first_repo.url == "https://fakerepository-link.org"
    assert first_repo.repo_name == "Fake repository"
    assert first_repo.contributors == ["Person1", "Person2", "Person3"]


@pytest.mark.cli
def test_pipeline_list(database_with_connected_pipeline, tmp_path, mocker):
    mocker.patch(
        "garden_ai.local_data.LOCAL_STORAGE", new=database_with_connected_pipeline
    )

    pipeline_title = "Fixture pipeline"
    pipeline_doi = "10.23677/jx31-gx98"

    command = [
        "pipeline",
        "list",
    ]
    result = runner.invoke(app, command)
    assert result.exit_code == 0

    assert pipeline_title in result.stdout
    assert pipeline_doi in result.stdout


@pytest.mark.cli
def test_pipeline_show(database_with_connected_pipeline, tmp_path, mocker):
    mocker.patch(
        "garden_ai.local_data.LOCAL_STORAGE", new=database_with_connected_pipeline
    )

    pipeline_title = "Fixture pipeline"
    pipeline_doi = "10.23677/jx31-gx98"

    command = [
        "pipeline",
        "show",
        pipeline_doi,
    ]
    result = runner.invoke(app, command)
    assert result.exit_code == 0

    assert pipeline_title in result.stdout
    assert pipeline_doi in result.stdout

    command = [
        "pipeline",
        "show",
        "not_a_pipeline_id",
        pipeline_doi,
    ]
    result = runner.invoke(app, command)
    assert result.exit_code == 0

    assert pipeline_title in result.stdout
    assert pipeline_doi in result.stdout


def test_clean_identifier():
    possible_name = "".join(random.choices(string.printable, k=50))
    valid_name = clean_identifier(possible_name)
    assert valid_name.isidentifier()
    assert not iskeyword(clean_identifier("import"))
