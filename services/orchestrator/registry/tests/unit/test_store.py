"""Unit tests for the projects.yaml load/save round trip."""

from services.orchestrator.registry.schemas import ProjectRecord
from services.orchestrator.registry.store import load_registry, save_registry


def test_load_registry_returns_empty_list_when_file_does_not_exist(tmp_path):
    assert load_registry(tmp_path / "projects.yaml") == []


def test_save_then_load_round_trips_a_record(tmp_path):
    path = tmp_path / "projects.yaml"
    record = ProjectRecord(
        id="foundry-itself",
        name="Foundry",
        mode="greenfield",
        path="/data/projects/foundry-itself",
        stack="python",
        coverage_threshold=80,
        pr_mode="configurable",
        cost_ceiling_usd_weekly=None,
    )

    save_registry(path, [record])
    loaded = load_registry(path)

    assert loaded == [record]


def test_registry_file_is_a_top_level_yaml_list(tmp_path):
    path = tmp_path / "projects.yaml"
    record = ProjectRecord(id="proj-1", name="Proj 1", mode="greenfield", path="/x", stack="python")

    save_registry(path, [record])

    assert path.read_text().lstrip().startswith("- ")
