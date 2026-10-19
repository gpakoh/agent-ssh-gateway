from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from examples.mcp_server.mcp_infra.adapters import gateway
from examples.mcp_server.tool_results import validate_pagination

CANDIDATE_STORE = {
    "status": "ok",
    "consistent": True,
    "physical_candidate_count": 0,
    "registered_candidate_count": 0,
    "unregistered_candidate_ids": [],
    "missing_candidate_ids": [],
    "unsafe_candidate_entries": [],
    "invalid_candidate_entry_count": 0,
}


def _row(
    project_id: str,
    *,
    type_: str = "repo",
    description: str = "",
    tags: list[str] | None = None,
    parent: str | None = None,
    root: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "project_id": project_id,
        "root": root if root is not None else project_id,
        "type": type_,
        "description": description,
        "tags": tags or [],
    }
    if parent is not None:
        row["parent"] = parent
    return row


def _install_registry(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Install a registry whose list_projects returns rows (unsorted on purpose)."""

    class Registry:
        def list_projects(self) -> list[dict[str, Any]]:
            return list(rows)

    monkeypatch.setattr(gateway, "_server_workspace_registry", lambda: Registry())
    monkeypatch.setattr(gateway, "_candidate_store_status", lambda: dict(CANDIDATE_STORE))
    return rows


def _install_forbidden_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> Any:
        raise AssertionError("inventory must not run before argument validation")

    monkeypatch.setattr(gateway, "_server_workspace_registry", _boom)
    monkeypatch.setattr(gateway, "_candidate_store_status", _boom)


def test_no_argument_call_preserves_previous_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row("zeta", description="late"),
        _row("alpha", description="early"),
    ]
    _install_registry(monkeypatch, rows)

    response = gateway.gateway_project_list()

    assert response["ok"] is True
    assert response["result"] == {
        "count": 2,
        "projects": sorted(rows, key=lambda r: str(r["project_id"])),
        "candidate_store": CANDIDATE_STORE,
    }
    assert set(response["result"]) == {"count", "projects", "candidate_store"}


def test_exact_project_id_returns_single_canonical_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row("candidate-0001", type_="candidate-clone"),
        _row("candidate-0002", type_="candidate-clone"),
        _row("canonical-web", type_="repo"),
        _row("candidate-0003", type_="candidate-clone"),
    ]
    _install_registry(monkeypatch, rows)

    response = gateway.gateway_project_list(project_id="canonical-web")

    result = response["result"]
    assert response["ok"] is True
    assert result["count"] == 1
    assert result["total"] == 1
    assert result["returned"] == 1
    assert [row["project_id"] for row in result["projects"]] == ["canonical-web"]
    assert result["candidate_store"] == CANDIDATE_STORE


def test_exact_project_id_unknown_returns_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_registry(monkeypatch, [_row("only-project")])

    response = gateway.gateway_project_list(project_id="does-not-exist")

    result = response["result"]
    assert response["ok"] is True
    assert result["count"] == 0
    assert result["total"] == 0
    assert result["returned"] == 0
    assert result["projects"] == []


def test_query_matches_id_prefix_and_description_text(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row("web-ssh-gateway", description="public edge gateway"),
        _row("worker-pool", description="webhook consumers"),
        _row("docs-site", description="handbook"),
    ]
    _install_registry(monkeypatch, rows)

    prefix = gateway.gateway_project_list(query="web-")
    text = gateway.gateway_project_list(query="Gateway")

    assert [row["project_id"] for row in prefix["result"]["projects"]] == ["web-ssh-gateway"]
    assert prefix["result"]["total"] == 1
    assert [row["project_id"] for row in text["result"]["projects"]] == ["web-ssh-gateway"]
    assert text["result"]["total"] == 1


def test_project_type_filter_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row("canonical", type_="repo"),
        _row("candidate-a", type_="candidate-clone"),
        _row("candidate-b", type_="candidate-clone"),
    ]
    _install_registry(monkeypatch, rows)

    response = gateway.gateway_project_list(project_type="candidate-clone")

    result = response["result"]
    assert result["count"] == 2
    assert result["total"] == 2
    assert [row["project_id"] for row in result["projects"]] == ["candidate-a", "candidate-b"]

    exact = gateway.gateway_project_list(project_type="repo")
    assert [row["project_id"] for row in exact["result"]["projects"]] == ["canonical"]


def test_tags_filter_requires_all_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row("both", tags=["infra", "edge"]),
        _row("one", tags=["infra"]),
        _row("none", tags=[]),
    ]
    _install_registry(monkeypatch, rows)

    response = gateway.gateway_project_list(tags=["infra", "edge"])

    result = response["result"]
    assert result["count"] == 1
    assert [row["project_id"] for row in result["projects"]] == ["both"]

    partial = gateway.gateway_project_list(tags=["infra"])
    assert [row["project_id"] for row in partial["result"]["projects"]] == ["both", "one"]


def test_parent_filter_matches_declared_parent_only(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row("child-a", parent="suite-x"),
        _row("child-b", parent="suite-y"),
        _row("root-project"),
    ]
    _install_registry(monkeypatch, rows)

    response = gateway.gateway_project_list(parent="suite-x")

    result = response["result"]
    assert result["count"] == 1
    assert [row["project_id"] for row in result["projects"]] == ["child-a"]


def test_limit_offset_page_deterministically_in_project_id_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_row(f"proj-{n:02d}") for n in (4, 1, 3, 0, 2)]
    _install_registry(monkeypatch, rows)

    page0 = gateway.gateway_project_list(limit=2, offset=0)
    page1 = gateway.gateway_project_list(limit=2, offset=2)
    page2 = gateway.gateway_project_list(limit=2, offset=4)
    beyond = gateway.gateway_project_list(limit=2, offset=99)

    expected = [f"proj-{n:02d}" for n in range(5)]
    got = (
        [row["project_id"] for row in page0["result"]["projects"]]
        + [row["project_id"] for row in page1["result"]["projects"]]
        + [row["project_id"] for row in page2["result"]["projects"]]
    )
    assert got == expected
    for page in (page0, page1, page2, beyond):
        assert page["result"]["total"] == 5
        assert page["result"]["count"] == 5
        assert page["result"]["offset"] in {0, 2, 4, 99}
        assert page["result"]["limit"] == 2
    assert page0["result"]["returned"] == 2
    assert page2["result"]["returned"] == 1
    assert beyond["result"]["returned"] == 0
    assert beyond["result"]["projects"] == []


def test_offset_only_returns_tail_without_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_row(f"proj-{n}") for n in range(4)]
    _install_registry(monkeypatch, rows)

    response = gateway.gateway_project_list(offset=2)

    result = response["result"]
    assert [row["project_id"] for row in result["projects"]] == ["proj-2", "proj-3"]
    assert result["limit"] is None
    assert result["offset"] == 2
    assert result["total"] == 4
    assert result["returned"] == 2


def test_filters_and_pagination_combine_with_total_accuracy(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row("candidate-01", type_="candidate-clone"),
        _row("candidate-02", type_="candidate-clone"),
        _row("candidate-03", type_="candidate-clone"),
        _row("canonical-web", type_="repo"),
        _row("candidate-04", type_="candidate-clone"),
    ]
    _install_registry(monkeypatch, rows)

    response = gateway.gateway_project_list(project_type="candidate-clone", limit=2, offset=2)

    result = response["result"]
    assert result["total"] == 4
    assert result["count"] == 4
    assert result["returned"] == 2
    assert [row["project_id"] for row in result["projects"]] == ["candidate-03", "candidate-04"]


def test_compact_projection_drops_metadata_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row(
            "web-ssh-gateway",
            type_="repo",
            description="edge gateway",
            tags=["infra", "edge"],
            parent=None,
        ),
        _row("child", type_="repo", description="kid", tags=["t"], parent="suite"),
    ]
    _install_registry(monkeypatch, rows)

    response = gateway.gateway_project_list(query="web", compact=True)

    result = response["result"]
    assert result["projects"] == [
        {
            "project_id": "web-ssh-gateway",
            "root": "web-ssh-gateway",
            "type": "repo",
        }
    ]
    assert result["total"] == 1
    assert result["returned"] == 1


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": -1}, "limit"),
        ({"limit": 501}, "limit"),
        ({"limit": "10"}, "limit"),
        ({"limit": True}, "limit"),
        ({"offset": -1}, "offset"),
        ({"offset": 10_001}, "offset"),
        ({"offset": "2"}, "offset"),
        ({"offset": False}, "offset"),
        ({"project_id": 42}, "project_id"),
        ({"project_id": ""}, "project_id"),
        ({"query": b"web"}, "query"),
        ({"query": "   "}, "query"),
        ({"project_type": ["repo"]}, "project_type"),
        ({"parent": 7}, "parent"),
        ({"tags": "infra"}, "tags"),
        ({"tags": ["infra", 3]}, "tags"),
        ({"tags": [""]}, "tags"),
    ],
)
def test_invalid_args_fail_before_registry_or_candidate_inventory(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, Any],
    needle: str,
) -> None:
    _install_forbidden_inventory(monkeypatch)

    response = gateway.gateway_project_list(**kwargs)

    assert response["ok"] is False
    assert response["error"]["code"] == "INVALID_INPUT"
    assert response["error"]["retryable"] is False
    assert needle in response["error"]["message"]


def test_pagination_bounds_match_shared_validator() -> None:
    with pytest.raises(ValueError, match="limit"):
        validate_pagination(0, "limit")
    with pytest.raises(ValueError, match="limit"):
        validate_pagination(501, "limit")
    validate_pagination(500, "limit")
    with pytest.raises(ValueError, match="offset"):
        validate_pagination(-1, "offset", min_value=0, max_value=10_000)
    validate_pagination(0, "offset", min_value=0, max_value=10_000)


def test_filtered_view_keeps_registry_relative_roots_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "registry-root"
    (workspace / "canonical-web").mkdir(parents=True)
    (workspace / "candidate-1").mkdir(parents=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "projects.yaml").write_text(
        "version: 1\n"
        f"registry_root: {workspace}\n\n"
        "projects:\n"
        "  candidate-1:\n"
        "    root: candidate-1\n"
        "    type: candidate-clone\n"
        "  canonical-web:\n"
        "    root: canonical-web\n"
        "    type: repo\n"
        "    description: canonical project\n"
        "    tags: [edge]\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MCP_RUNTIME_PROJECTS_PATH", raising=False)
    monkeypatch.delenv("MCP_SUPERVISOR_JOURNAL_ROOT", raising=False)

    from app.workspace.registry import WorkspaceRegistry

    registry = WorkspaceRegistry.load(str(config_dir / "projects.yaml"), allowed_roots=[workspace])
    monkeypatch.setattr(gateway, "_server_workspace_registry", lambda: registry)
    monkeypatch.setattr(gateway, "_candidate_store_status", lambda: dict(CANDIDATE_STORE))

    response = gateway.gateway_project_list(tags=["edge"])

    assert response["ok"] is True
    projects = response["result"]["projects"]
    assert [row["project_id"] for row in projects] == ["canonical-web"]
    for row in projects:
        assert not str(row["root"]).startswith(str(tmp_path))
