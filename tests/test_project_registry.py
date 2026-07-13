import pytest

from examples.mcp_server.project_registry import ProjectRegistry


@pytest.fixture
def project_root(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "web-ssh-gateway").mkdir()
    return root


@pytest.fixture
def registry(project_root):
    return ProjectRegistry(
        projects={
            "web-ssh-gateway": str(project_root / "web-ssh-gateway"),
        },
        allowed_roots=[str(project_root)],
    )


def test_resolve_known_project(registry, project_root):
    path = registry.resolve("web-ssh-gateway")
    assert path == project_root / "web-ssh-gateway"


def test_resolve_unknown_project(registry):
    with pytest.raises(ValueError, match="PROJECT_NOT_FOUND"):
        registry.resolve("nonexistent")


def test_resolve_symlink_escape(tmp_path):
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
