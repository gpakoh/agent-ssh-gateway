"""Repository-owned defense against fork PRs selecting host Docker CI labels.

These are workflow topology assertions, NOT a replacement for protected
out-of-repository runner admission (see SECURITY.md / P1-07).
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
SAME_HEAD = "github.event.pull_request.head.repo.full_name == github.repository"
SAME_BASE = "github.event.pull_request.base.repo.full_name == github.repository"


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_origin_gate_precedes_all_pr_checkout_or_shell_steps() -> None:
    jobs = _jobs()
    scope = jobs["scope"]
    assert scope["runs-on"] == "ubuntu-latest"
    assert "github.event_name != 'pull_request'" in scope["if"]
    assert SAME_HEAD in scope["if"]
    assert SAME_BASE in scope["if"]
    assert " || " in scope["if"]
    assert scope["steps"][0]["name"] == "Checkout exact event commit from Gitea"

    # No privileged PR job may outlive or bypass its originating scope gate.
    for name, job in jobs.items():
        if name == "scope":
            continue
        if name == "e2e":
            assert job["needs"] == ["test"]
            assert jobs["test"]["needs"] == ["scope"]
        elif name in {"fleet-postgres", "build-and-push", "test", "test-gates", "coverage"}:
            assert "scope" in job["needs"]


def test_docker_jobs_require_exact_same_repo_pr_origin() -> None:
    jobs = _jobs()
    for name in ("fleet-postgres", "e2e", "build-and-push"):
        condition = jobs[name]["if"]
        assert SAME_HEAD in condition, name
        assert SAME_BASE in condition, name

    assert jobs["build-and-push"]["runs-on"] == "docker-e2e-ci"
    assert "github.ref_name == 'master'" in jobs["build-and-push"]["if"]
    assert "github.event_name == 'push'" in jobs["build-and-push"]["if"]

    for name in ("deploy", "host-smoke"):
        condition = jobs[name]["if"]
        assert "github.ref_name == 'master'" in condition
        assert "github.event_name == 'push'" in condition
        assert "pull_request" not in condition


def test_trust_boundary_is_explicit_not_claimed_as_isolation() -> None:
    security = (WORKFLOW.parents[2] / "SECURITY.md").read_text(encoding="utf-8")
    assert "shared host Docker daemon" in security
    assert "not a security boundary" in security
    assert "out-of-repository runner admission controls" in security
    assert "registry credential" in security