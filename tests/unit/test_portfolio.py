"""Contratos da hierarquia produto -> projeto -> iniciativa."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from orkmind.cli import main as cli_main
from orkmind.core.portfolio import CycleScope, Initiative, PortfolioCatalog, Product, Project
from orkmind.store.memory_adapter import MemoryAdapter


@pytest.mark.asyncio
async def test_catalog_preserves_hierarchy_and_multi_workspace() -> None:
    store = MemoryAdapter()
    await store.initialize()
    catalog = PortfolioCatalog(store)
    product = Product(id="prod-orkastery", title="Orkastery")
    project = Project(
        id="proj-maestro-workspace",
        title="Maestro Workspace",
        product_id=product.id,
        workspace_ids=["orkastery", "orkmind", "site"],
    )
    initiative = Initiative(
        id="init-portfolio-ontology",
        title="Ontologia",
        project_id=project.id,
    )

    await catalog.add_product(product)
    await catalog.add_project(project)
    await catalog.add_initiative(initiative)

    projects = await catalog.list("project", product.id)
    initiatives = await catalog.list("initiative", project.id)
    assert [item.id for item in projects] == [project.id]
    assert projects[0].metadata["workspace_ids"] == ["orkastery", "orkmind", "site"]
    assert [item.id for item in initiatives] == [initiative.id]
    assert initiatives[0].parent_id == project.id


@pytest.mark.asyncio
async def test_catalog_rejects_missing_parent_and_cross_project_dependency() -> None:
    store = MemoryAdapter()
    await store.initialize()
    catalog = PortfolioCatalog(store)
    with pytest.raises(ValueError, match="produto"):
        await catalog.add_project(Project(
            id="proj-orphan", title="Órfão", product_id="prod-missing"
        ))

    first_product = Product(id="prod-one", title="One")
    second_product = Product(id="prod-two", title="Two")
    await catalog.add_product(first_product)
    await catalog.add_product(second_product)
    first_project = Project(id="proj-one", title="One", product_id=first_product.id)
    second_project = Project(id="proj-two", title="Two", product_id=second_product.id)
    await catalog.add_project(first_project)
    await catalog.add_project(second_project)
    dependency = Initiative(id="init-dependency", title="Dependency", project_id=first_project.id)
    await catalog.add_initiative(dependency)
    with pytest.raises(ValueError, match="mesmo projeto"):
        await catalog.add_initiative(Initiative(
            id="init-invalid", title="Invalid", project_id=second_project.id,
            depends_on=[dependency.id],
        ))


def test_cycle_scope_is_project_or_nonempty_initiative_set() -> None:
    assert CycleScope(project_id="proj-one", delivery="project").initiative_ids == []
    scope = CycleScope(
        project_id="proj-one", delivery="initiatives",
        initiative_ids=["init-one", "init-one", "init-two"],
    )
    assert scope.initiative_ids == ["init-one", "init-two"]
    with pytest.raises(ValidationError):
        CycleScope(project_id="proj-one", delivery="initiatives")


def test_portfolio_cli_accepts_stable_ids_and_lists_the_whole_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryAdapter()
    monkeypatch.setattr(cli_main, "_get_layer", lambda: SimpleNamespace(store=store))
    runner = CliRunner()

    product = runner.invoke(
        cli_main.cli,
        ["portfolio", "create", "product", "prod-one", "--title", "One"],
    )
    assert product.exit_code == 0, product.output
    project = runner.invoke(
        cli_main.cli,
        [
            "portfolio",
            "create",
            "project",
            "proj-one",
            "--title",
            "One project",
            "--parent",
            "prod-one",
        ],
    )
    assert project.exit_code == 0, project.output

    listed = runner.invoke(cli_main.cli, ["portfolio", "list"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["kind"] == "all"
    assert [item["id"] for item in payload["items"]] == ["prod-one", "proj-one"]
