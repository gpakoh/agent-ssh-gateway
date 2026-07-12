from pathlib import Path

import pytest

from examples.mcp_server.project_registry import ProjectRegistry


@pytest.fixture
def registry():
    return ProjectRegistry(
        projects={
            "web-ssh-gateway": "/media/1TB/Python/web_ssh/web-ssh-gateway",
        },
        allowed_roots=["/media/1TB/Python/"],
    )


def test_resolve_known_project(registry):
    path = registry.resolve("web-ssh-gateway")
    assert path == Path("/media/1TB/Python/web_ssh/web-ssh-gateway")


def test_resolve_unknown_project(registry):
    with pytest.raises(ValueError, match="PROJECT_NOT_FOUND"):
        registry.resolve("nonexistent")


def test_resolve_symlink_escape(tmp_path, registry):
    root = tmp_path / "allowed"
    root.mkdir()
    escape = tmp_path / "escape"
    escape.mkdir()
    link = root / "link"
    link.symlink_to(escape, target_is_directory=True)

    bad_registry = ProjectRegistry({"evil": str(link)}, allowed_roots=[str(root)])
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        bad_registry.resolve("evil")


def test_resolve_project_outside_allowed_root(tmp_path):
    root = tmp_path / "python"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()

    reg = ProjectRegistry({"bad": str(outside)}, allowed_roots=[str(root)])
    with pytest.raises(ValueError, match="POLICY_DENIED"):
        reg.resolve("bad")
