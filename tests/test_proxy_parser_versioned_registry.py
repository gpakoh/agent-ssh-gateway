"""Ensure versioned proxy-parser registry matches the live checkout (#875)."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.workspace_registry import WorkspaceRegistry


def _checked_in_config() -> dict:
    return yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "projects.yaml").read_text(
            encoding="utf-8"
        )
    )


def test_proxy_parser_versioned_root_and_parent_are_consistent() -> None:
    source = (Path(__file__).resolve().parents[1] / "projects.yaml").read_text(
        encoding="utf-8"
    )
    # A duplicate YAML mapping can silently shadow the legacy declaration.
    assert source.count("\n  proxy-parser:\n") == 1
    projects = _checked_in_config()["projects"]
    config = projects["proxy-parser"]

    assert projects["quart-platform"]["root"] == "quart-platform"
    assert config["root"] == "quart-platform/proxy-parser"
    assert config["parent"] == "quart-platform"
    assert config["type"] == "python-service"
    assert "proxy" in config["tags"]


def test_proxy_parser_versioned_entry_loads_with_parent(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("WORKSPACE_REGISTRY_OVERLAY", raising=False)
    (tmp_path / "quart-platform" / "proxy-parser").mkdir(parents=True)
    config = _checked_in_config()["projects"]
    registry_file = tmp_path / "projects.yaml"
    registry_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "registry_root": str(tmp_path),
                "projects": {
                    "quart-platform": config["quart-platform"],
                    "proxy-parser": config["proxy-parser"],
                },
            }
        ),
        encoding="utf-8",
    )

    rows = {
        item["project_id"]: item
        for item in WorkspaceRegistry.load(
            registry_file, allowed_roots=[tmp_path]
        ).list_projects()
    }
    assert rows["quart-platform"]["root"] == "quart-platform"
    assert rows["proxy-parser"]["root"] == "quart-platform/proxy-parser"
    assert rows["proxy-parser"]["parent"] == "quart-platform"
    assert rows["proxy-parser"]["type"] == "python-service"
