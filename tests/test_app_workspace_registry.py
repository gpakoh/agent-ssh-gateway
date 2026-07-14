"""Tests for app.workspace_registry — registry loader, path validation, file tree."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.workspace_policy import HiddenPathError, WorkspacePolicyError
from app.workspace_registry import (
    VENDOR_CACHE_PATTERNS,
    WorkspaceRegistry,
    load_registry,
    project_info,
    project_tree,
    reset_registry,
    workspace_list_projects,
)

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def registry_root(tmp_path):
    """Create a temporary filesystem with multiple projects."""
    root = tmp_path / "python"
    root.mkdir()

    proj = root / "web-ssh-gateway"
    proj.mkdir()
    (proj / "app").mkdir()
    (proj / "app" / "__init__.py").write_text("")
    (proj / "app" / "main.py").write_text("print('hello')")
    (proj / "tests").mkdir()
    (proj / "tests" / "test_app.py").write_text("")
    (proj / ".env").write_text("SECRET=abc")
    (proj / ".venv").mkdir()
    (proj / ".venv" / "lib").mkdir()
    (proj / "__pycache__").mkdir()
    (proj / "cache.pyc").write_text("")
    (proj / "README.md").write_text("# gateway")

    proj2 = root / "nod-gateway"
    proj2.mkdir()
    (proj2 / "master_server").mkdir()
    (proj2 / "gateway_client").mkdir()
    (proj2 / "README.md").write_text("# nod")

    proj3 = root / "quart-core"
    proj3.mkdir()
    (proj3 / "quart").mkdir()

    # Symlink escape setup
    escape = tmp_path / "escape"
    escape.mkdir()
    link = proj / "escape_link"
    link.symlink_to(escape, target_is_directory=True)

    return root


@pytest.fixture
def yaml_path(registry_root):
    """Create a projects.yaml pointing at the registry_root."""
    path = registry_root.parent / "projects.yaml"
    data = {
        "version": 1,
        "registry_root": str(registry_root),
        "projects": {
            "web-ssh-gateway": {
                "root": "web-ssh-gateway",
                "type": "fastapi",
                "description": "SSH gateway",
                "tags": ["gateway", "ssh"],
            },
            "nod-gateway": {
                "root": "nod-gateway",
                "type": "monorepo",
                "description": "NOD platform",
                "tags": ["nod"],
            },
            "quart-core": {
                "root": "quart-core",
                "type": "quart",
                "description": "Core framework",
                "tags": ["quart"],
            },
        },
    }
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


@pytest.fixture
def registry(registry_root, yaml_path):
    """Build a WorkspaceRegistry from the test projects.yaml."""
    reset_registry()
    return WorkspaceRegistry.load(
        str(yaml_path),
        allowed_roots=[registry_root],
    )


# ── YAML loader tests ─────────────────────────────────────────────


class TestLoadRegistry:
    def test_load_valid_yaml(self, registry_root, yaml_path):
        projects, root = load_registry(str(yaml_path))
        assert "web-ssh-gateway" in projects
        assert "nod-gateway" in projects
        assert len(projects) == 3
        assert root == registry_root

    def test_load_includes_project_info(self, registry_root, yaml_path):
        projects, root = load_registry(str(yaml_path))
        info = projects["web-ssh-gateway"]
        assert info.type == "fastapi"
        assert info.description == "SSH gateway"
        assert info.tags == ["gateway", "ssh"]
        assert info.root.exists()
        assert root == registry_root

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(WorkspacePolicyError, match="not found"):
            load_registry(str(tmp_path / "nonexistent.yaml"))

    def test_load_invalid_yaml_raises(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("not: valid: yaml: [", encoding="utf-8")
        with pytest.raises(yaml.YAMLError):
            load_registry(str(p))

    def test_load_not_a_directory(self, registry_root, yaml_path):
        p = registry_root / "README.md"
        p.write_text("")
        projects, root = load_registry(str(yaml_path))
        assert len(projects) == 3  # skipped README because not a mapping in projects
        assert root == registry_root

    def test_load_skips_project_without_root(self, registry_root, tmp_path):
        p = tmp_path / "bad.yaml"
        data = {
            "registry_root": str(registry_root),
            "projects": {"no-root": {"type": "unknown"}},
        }
        p.write_text(yaml.dump(data), encoding="utf-8")
        projects, root = load_registry(str(p))
        assert "no-root" not in projects
        assert root == registry_root

    def test_load_skips_nonexistent_root(self, registry_root, tmp_path):
        p = tmp_path / "bad.yaml"
        data = {
            "registry_root": str(registry_root),
            "projects": {"ghost": {"root": "does-not-exist"}},
        }
        p.write_text(yaml.dump(data), encoding="utf-8")
        projects, root = load_registry(str(p))
        assert "ghost" not in projects
        assert root == registry_root


# ── list_projects tests ───────────────────────────────────────────


class TestListProjects:
    def test_returns_all_projects(self, registry):
        projects = registry.list_projects()
        assert isinstance(projects, list)
        assert len(projects) == 3
        ids = {p["project_id"] for p in projects}
        assert ids == {"web-ssh-gateway", "nod-gateway", "quart-core"}

    def test_summary_fields(self, registry):
        projects = registry.list_projects()
        web = next(p for p in projects if p["project_id"] == "web-ssh-gateway")
        assert web["type"] == "fastapi"
        assert web["description"] == "SSH gateway"
        assert web["tags"] == ["gateway", "ssh"]

    def test_sorted_order(self, registry):
        projects = registry.list_projects()
        ids = [p["project_id"] for p in projects]
        assert ids == sorted(ids)

    def test_empty_registry(self, registry_root):
        r = WorkspaceRegistry({}, [registry_root])
        assert r.list_projects() == []

    def test_empty_via_tool_function(self, registry):
        """Test that the module-level tool function works."""
        projects = workspace_list_projects(registry)
        assert len(projects) == 3


# ── project_info tests ────────────────────────────────────────────


class TestProjectInfo:
    def test_known_project(self, registry):
        info = registry.project_info("web-ssh-gateway")
        assert info["project_id"] == "web-ssh-gateway"
        assert info["type"] == "fastapi"
        assert "root" in info

    def test_root_is_string_path(self, registry):
        info = registry.project_info("web-ssh-gateway")
        root = Path(info["root"])
        assert root.exists()
        assert (root / "app").exists()

    def test_unknown_project_raises(self, registry):
        with pytest.raises(WorkspacePolicyError, match="Unknown project"):
            registry.project_info("nonexistent")

    def test_unknown_via_tool_function(self, registry):
        with pytest.raises(WorkspacePolicyError, match="Unknown project"):
            project_info("nonexistent", registry)


# ── project_tree tests ────────────────────────────────────────────


class TestProjectTree:
    def test_root_tree(self, registry, registry_root):
        tree = registry.project_tree("web-ssh-gateway")
        assert tree["type"] == "directory"
        assert tree["name"] == "web-ssh-gateway"
        assert tree["path"] == ""
        assert "children" in tree

    def test_children_visible(self, registry):
        tree = registry.project_tree("web-ssh-gateway")
        names = [c["name"] for c in tree["children"]]
        assert "app" in names
        assert "tests" in names
        assert "README.md" in names

    def test_subtree(self, registry):
        tree = registry.project_tree("web-ssh-gateway", "app")
        assert tree["type"] == "directory"
        assert tree["name"] == "app"
        assert tree["path"] == "app"
        children = [c["name"] for c in tree["children"]]
        assert "__init__.py" in children
        assert "main.py" in children

    def test_file_type(self, registry):
        tree = registry.project_tree("web-ssh-gateway", "app")
        main = next(c for c in tree["children"] if c["name"] == "main.py")
        assert main["type"] == "file"

    def test_empty_path_is_root(self, registry, registry_root):
        tree = registry.project_tree("web-ssh-gateway", "")
        assert tree["name"] == "web-ssh-gateway"

    def test_nonexistent_path_raises(self, registry):
        with pytest.raises(WorkspacePolicyError, match="does not exist"):
            registry.project_tree("web-ssh-gateway", "nonexistent_dir")

    def test_file_path_raises(self, registry):
        with pytest.raises(WorkspacePolicyError, match="not a directory"):
            registry.project_tree("web-ssh-gateway", "app/__init__.py")

    def test_unknown_project_raises(self, registry):
        with pytest.raises(WorkspacePolicyError, match="Unknown project"):
            registry.project_tree("nonexistent")

    def test_via_tool_function(self, registry):
        tree = project_tree("web-ssh-gateway", registry=registry)
        assert tree["name"] == "web-ssh-gateway"

    def test_default_depth_is_3(self, registry):
        tree = registry.project_tree("web-ssh-gateway")
        assert tree["type"] == "directory"
        assert tree["name"] == "web-ssh-gateway"

    def test_truncated_flag_default_false(self, registry):
        tree = registry.project_tree("web-ssh-gateway")
        assert "truncated" in tree
        assert tree["truncated"] is False

    def test_depth_zero_no_children(self, registry):
        tree = registry.project_tree("web-ssh-gateway", depth=0)
        assert "children" not in tree
        assert tree["truncated"] is False


# ── Secret / vendor filtering tests ───────────────────────────────


class TestSecretFiltering:
    def test_hides_env_file(self, registry):
        tree = registry.project_tree("web-ssh-gateway")
        names = [c["name"] for c in tree["children"]]
        assert ".env" not in names

    def test_hides_venv(self, registry):
        tree = registry.project_tree("web-ssh-gateway")
        names = [c["name"] for c in tree["children"]]
        assert ".venv" not in names

    def test_hides_pycache(self, registry):
        tree = registry.project_tree("web-ssh-gateway")
        names = [c["name"] for c in tree["children"]]
        assert "__pycache__" not in names

    def test_hides_pyc_file(self, registry):
        tree = registry.project_tree("web-ssh-gateway")
        names = [c["name"] for c in tree["children"]]
        assert "cache.pyc" not in names

    def test_normal_files_visible(self, registry):
        tree = registry.project_tree("web-ssh-gateway")
        names = [c["name"] for c in tree["children"]]
        assert "README.md" in names
        assert "app" in names
        assert "tests" in names

    def test_secret_path_read_still_blocked_by_policy(self, registry, registry_root):
        # The policy blocks read of .env even if tree hides it
        with pytest.raises(HiddenPathError):
            registry._policy.validate_read("web-ssh-gateway", ".env")


# ── Path validation tests ─────────────────────────────────────────


class TestPathValidation:
    def test_traversal_dotdot_raises(self, registry):
        with pytest.raises(WorkspacePolicyError):
            registry.project_tree("web-ssh-gateway", "../nod-gateway")

    def test_traversal_encoded_raises(self, registry):
        with pytest.raises(WorkspacePolicyError):
            registry.project_tree("web-ssh-gateway", "app/../../nod-gateway")

    def test_unknown_project_raises_on_info(self, registry):
        with pytest.raises(WorkspacePolicyError, match="Unknown project"):
            registry.project_info("does-not-exist")

    def test_unknown_project_raises_on_tree(self, registry):
        with pytest.raises(WorkspacePolicyError, match="Unknown project"):
            registry.project_tree("does-not-exist")


# ── Edge cases ────────────────────────────────────────────────────


class TestEdgeCases:
    def test_project_not_in_registry(self, registry):
        with pytest.raises(WorkspacePolicyError, match="Unknown project"):
            registry.project_info("")

    def test_contains_operator(self, registry):
        assert "web-ssh-gateway" in registry
        assert "nonexistent" not in registry

    def test_len(self, registry):
        assert len(registry) == 3

    def test_empty_allowed_roots(self, registry_root):
        r = WorkspaceRegistry({}, [registry_root])
        assert len(r) == 0

    def test_symlink_escape_in_tree(self, registry):
        with pytest.raises(WorkspacePolicyError):
            registry.project_tree("web-ssh-gateway", "escape_link")


# ── Regression: load() without allowed_roots ──────────────────────


class TestLoadWithoutAllowedRoots:
    """Regression: WorkspaceRegistry.load() must use YAML registry_root,
    not the parent folder of projects.yaml, as the default allowed root."""

    def test_outside_yaml_parent_accessible(self, tmp_path):
        """Projects outside the yaml parent directory but inside
        registry_root must be accessible without explicit allowed_roots."""
        # Layout:
        #   config/projects.yaml  (registry_root = /tmp/xxx/python)
        #   python/web-ssh-gateway/
        #   python/nod-gateway/
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        python_dir = tmp_path / "python"
        python_dir.mkdir()

        for name in ("web-ssh-gateway", "nod-gateway"):
            (python_dir / name).mkdir()
            (python_dir / name / "README.md").write_text(f"# {name}")

        yaml_path = config_dir / "projects.yaml"
        data = {
            "version": 1,
            "registry_root": str(python_dir),
            "projects": {
                "web-ssh-gateway": {
                    "root": "web-ssh-gateway",
                    "type": "fastapi",
                    "description": "",
                    "tags": [],
                },
                "nod-gateway": {
                    "root": "nod-gateway",
                    "type": "monorepo",
                    "description": "",
                    "tags": [],
                },
            },
        }
        yaml_path.write_text(yaml.dump(data), encoding="utf-8")

        reset_registry()
        registry = WorkspaceRegistry.load(str(yaml_path))
        # Both must be accessible even though yaml is in config/ subdir
        projects = registry.list_projects()
        assert len(projects) == 2

        for pid in ("web-ssh-gateway", "nod-gateway"):
            info = registry.project_info(pid)
            assert Path(info["root"]).exists()
            tree = registry.project_tree(pid)
            assert tree["type"] == "directory"


# ── Static patterns ───────────────────────────────────────────────


class TestVendorCachePatterns:
    def test_known_patterns_exist(self):
        assert ".git" in VENDOR_CACHE_PATTERNS
        assert "__pycache__" in VENDOR_CACHE_PATTERNS
        assert ".venv" in VENDOR_CACHE_PATTERNS
        assert "node_modules" in VENDOR_CACHE_PATTERNS

    def test_patterns_cover_cache_dirs(self):
        cache_dirs = {".mypy_cache", ".pytest_cache", ".ruff_cache", ".benchmarks"}
        for d in cache_dirs:
            assert d in VENDOR_CACHE_PATTERNS, f"{d} missing from patterns"


# ── Parent-child relationship tests ──────────────────────────────


def test_parent_child_relationship(tmp_path):
    """quart-core and kojo-bot-service have parent=quart-platform."""
    root = tmp_path / "python"
    root.mkdir()

    (root / "quart-platform").mkdir()
    (root / "quart-platform" / "quart-core").mkdir()
    (root / "quart-platform" / "kojo-bot-service").mkdir()

    yaml_content = {
        "version": 1,
        "registry_root": str(root),
        "projects": {
            "quart-platform": {
                "root": "quart-platform",
                "type": "platform",
                "description": "Umbrella",
                "tags": ["quart"],
            },
            "quart-core": {
                "root": "quart-platform/quart-core",
                "parent": "quart-platform",
                "type": "service",
                "description": "Core",
                "tags": ["quart"],
            },
            "kojo-bot-service": {
                "root": "quart-platform/kojo-bot-service",
                "parent": "quart-platform",
                "type": "service",
                "description": "Bot",
                "tags": ["bot"],
            },
        },
    }
    yaml_path = tmp_path / "projects.yaml"
    yaml_path.write_text(yaml.dump(yaml_content))

    projects, reg_root = load_registry(yaml_path)

    assert projects["quart-platform"].parent is None
    assert projects["quart-core"].parent == "quart-platform"
    assert projects["kojo-bot-service"].parent == "quart-platform"
