"""Seam test: every "list/read files under an untrusted pattern, scoped to a
root" and "resolve an untrusted path segment into a URL" entry point in this
codebase, run against the SAME attack battery.

Why this file exists: five separate audit rounds this session (T83-T86)
each found the identical bug — a symlink (or ".."-laden segment) escaping
the intended root — in a DIFFERENT implementation of the same conceptual
operation:
  - app/workspace/search.py, app/workspace/scan_project.py,
    app/services/project_search.py, mcp_client_tools.py's list_files all
    used Path.rglob(pattern)/Path.glob(pattern) with a caller-controlled
    pattern, and checked containment on the *unresolved* result — which
    doesn't catch a pattern that explicitly names a symlinked segment
    (glob() DOES follow symlinks when the pattern names them directly,
    even though it doesn't descend into them for bare "*"/"**").
  - gitea_client.py/github_client.py built a URL path via
    "/repos/{owner}/{repo}".format(...) with no validation at all; httpx
    normalizes ".." in the combined path against the *whole* URL
    (base_url's path included), so an unvalidated owner escaped past
    ALLOWED_ENDPOINTS entirely.
  - WorkspacePolicy.validate_write() only resolved+checked an existing
    write target; a symlinked ancestor with a not-yet-existing child was
    never checked (caught only by every caller separately pairing a
    second _symlink_safe_preflight() call).

Each was found, fixed, and given its own regression test independently —
this file exists so the *next* new "list files matching an untrusted
pattern" or "build a URL from an untrusted path segment" function gets
tested against the same battery automatically, by being added to
SYMLINK_GLOB_PROBES / URL_PATH_PROBES below, instead of needing its own
audit round to discover the same bug for the sixth time.

Each probe returns True if the escape succeeded (a bug) and False if
correctly blocked. The parametrized test asserts False for every real,
protected entry point. A separate baseline test proves the attack vector
itself is real (using unprotected raw Path.rglob) — so a probe returning
False can't be mistaken for "the harness doesn't actually try the attack".
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol
from unittest.mock import MagicMock

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = REPO_ROOT / "examples" / "mcp_server"
MCP_CLIENT_REMOTE_DIR = REPO_ROOT / "examples" / "mcp_client_remote"

SECRET_MARKER = "SEAM_TEST_SECRET_MARKER_7f3a"


def _make_symlink_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """project/escape_link -> outside, outside/secret.txt has SECRET_MARKER.

    Returns (project_root, outside_dir).
    """
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(f"{SECRET_MARKER}\n")
    (project / "escape_link").symlink_to(outside)
    return project, outside


class Probe(Protocol):
    def __call__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bool: ...


# ── Baseline: proves the attack vector is real, not a harness bug ──────────


def probe_baseline_unprotected_rglob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
    project, _outside = _make_symlink_fixture(tmp_path)
    for p in project.rglob("escape_link/*"):
        if p.is_file() and SECRET_MARKER in p.read_text():
            return True
    return False


# ── app/workspace/search.py ─────────────────────────────────────────────


def probe_workspace_search_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
    from app.workspace.models import ProjectInfo
    from app.workspace.registry import WorkspaceRegistry
    from app.workspace.search import project_search_text

    project, _outside = _make_symlink_fixture(tmp_path)
    registry = WorkspaceRegistry(
        projects={
            "proj": ProjectInfo(
                project_id="proj", root=project, type="t", description="", tags=[]
            )
        },
        allowed_roots=[tmp_path],
        granted_scopes={"project:read", "workspace:read"},
    )
    result = project_search_text(
        "proj", SECRET_MARKER, file_glob="escape_link/*", registry=registry
    )
    return len(result["matches"]) > 0


# ── app/workspace/scan_project.py ───────────────────────────────────────


def probe_workspace_scan_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
    from app.workspace.scan_project import scan_project

    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.sh").write_text("rm -rf /\n")
    (project / "escape_link").symlink_to(outside)
    result = scan_project(
        "proj", pattern="escape_link/*", max_files=10, _root_override=project
    )
    assert isinstance(result, dict)
    return result["total_findings"] > 0


# ── app/services/project_search.py ──────────────────────────────────────


def probe_services_project_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
    from app.services.project_search import search_text

    project, _outside = _make_symlink_fixture(tmp_path)
    result = search_text(project, SECRET_MARKER, glob="escape_link/*")
    return result["count"] > 0


# ── examples/mcp_server/mcp_client_tools.py ─────────────────────────────


def probe_mcp_client_tools_list_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> bool:
    sys.path.insert(0, str(MCP_SERVER_DIR))
    from mcp_client_tools import list_files

    project, _outside = _make_symlink_fixture(tmp_path)
    monkeypatch.setattr("mcp_client_tools._resolve_project", lambda name: project)
    monkeypatch.setattr("mcp_client_tools._validate_project", lambda name: name)
    result = list_files(MagicMock(), "project", "escape_link/*")
    return any("secret.txt" in f for f in result["files"])


def probe_mcp_client_tools_safe_glob_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> bool:
    """_safe_glob (used by find_files) already resolves before checking
    containment — included as a known-good control among the real targets.
    """
    sys.path.insert(0, str(MCP_SERVER_DIR))
    from mcp_client_tools import _safe_glob

    project, _outside = _make_symlink_fixture(tmp_path)
    result = _safe_glob(project, "escape_link/*")
    return any("secret.txt" in f for f in result["files"])


# ── app/workspace/policy.py — write-side symlink-ancestor escape ───────


def probe_workspace_policy_validate_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> bool:
    from app.workspace.policy import SymlinkEscapeError, WorkspacePolicy

    project, _outside = _make_symlink_fixture(tmp_path)
    policy = WorkspacePolicy(
        project_roots={"proj": project},
        allowed_roots=[tmp_path],
        granted_scopes={"project:write"},
    )
    try:
        # escape_link/newdir doesn't exist yet — only escape_link itself does.
        policy.validate_write("proj", "escape_link/newdir/pwned.txt")
        return True
    except SymlinkEscapeError:
        return False


SYMLINK_GLOB_PROBES: list[tuple[str, Probe]] = [
    ("workspace.search.project_search_text", probe_workspace_search_text),
    ("workspace.scan_project.scan_project", probe_workspace_scan_project),
    ("services.project_search.search_text", probe_services_project_search),
    ("mcp_client_tools.list_files", probe_mcp_client_tools_list_files),
    ("mcp_client_tools._safe_glob (control)", probe_mcp_client_tools_safe_glob_control),
    ("workspace.policy.validate_write", probe_workspace_policy_validate_write),
]


def test_baseline_confirms_attack_vector_is_real(tmp_path, monkeypatch):
    assert probe_baseline_unprotected_rglob(tmp_path, monkeypatch) is True, (
        "the baseline itself must demonstrate the leak with no protection — "
        "if this ever fails, the probe methodology is broken, not the fix"
    )


@pytest.mark.parametrize("name,probe", SYMLINK_GLOB_PROBES, ids=[n for n, _ in SYMLINK_GLOB_PROBES])
def test_no_symlink_escape_in_any_seam(name, probe, tmp_path, monkeypatch):
    escaped = probe(tmp_path, monkeypatch)
    assert not escaped, f"{name} let a symlink/glob escape the project root"


# ── URL-path containment: gitea_client.py / github_client.py ───────────


def _url_probe(client_cls_path: str, base_url: str, method: str, args: tuple) -> str | None:
    """Call a fleet client method against a recording MockTransport.

    Returns the resolved request URL if a request was sent, or None if the
    client rejected the call before ever making a request (the safe case).
    """
    import asyncio

    sys.path.insert(0, str(MCP_CLIENT_REMOTE_DIR))
    module_name, class_name = client_cls_path.rsplit(".", 1)
    import importlib

    mod = importlib.import_module(module_name)
    client_cls = getattr(mod, class_name)

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={}, request=request)

    async def run() -> str | None:
        client = client_cls("fake-token")
        client._client = httpx.AsyncClient(
            base_url=base_url, transport=httpx.MockTransport(handler)
        )
        try:
            fn = getattr(client, method)
            await fn(*args)
        except ValueError:
            pass
        finally:
            await client.aclose()
        return seen.get("url")

    return asyncio.run(run())


URL_PATH_PROBES: list[tuple[str, str, str, str, tuple]] = [
    (
        "gitea_client.GiteaClient.get_repo(owner escape)",
        "fleet.gitea_client.GiteaClient",
        "https://gitea.example/api/v1",
        "get_repo",
        ("foo/../../admin", "x"),
    ),
    (
        "gitea_client.GiteaClient.get_file(path escape)",
        "fleet.gitea_client.GiteaClient",
        "https://gitea.example/api/v1",
        "get_file",
        ("owner", "repo", "../../../etc/passwd"),
    ),
    (
        "github_client.GitHubClient.get_repo(owner escape)",
        "fleet.github_client.GitHubClient",
        "https://api.github.com",
        "get_repo",
        ("foo/../../user", "x"),
    ),
]


@pytest.mark.parametrize(
    "name,client_cls_path,base_url,method,args",
    URL_PATH_PROBES,
    ids=[p[0] for p in URL_PATH_PROBES],
)
def test_no_url_path_escape_in_any_fleet_client(name, client_cls_path, base_url, method, args):
    resolved_url = _url_probe(client_cls_path, base_url, method, args)
    assert resolved_url is None, (
        f"{name} sent a request instead of rejecting the malicious segment: {resolved_url!r}"
    )
