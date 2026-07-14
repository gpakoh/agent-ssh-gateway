# Workspace Control Plane Phase A — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add parent-child project relationships to the workspace registry, update projects.yaml to 7 projects, and verify real smoke across all projects.

**Architecture:** Thin delta on existing `app/workspace/` package. Add `parent` field to `ProjectInfo`, `truncated` to `TreeNode`, update `projects.yaml` with `quart-platform` parent, adjust registry loader to pass `parent` through.

**Tech Stack:** Python 3.12, PyYAML, pytest, dataclasses

## Global Constraints

- `projects.yaml` is source of truth; project IDs are stable (rename requires migration)
- `WorkspacePolicy` is security boundary; tree-builder handles UI ignores only
- Phase A is read-only: list, info, tree. No file content, no search, no git, no docker
- Tools are sync `def`, output is `dict[str, Any]`
- Secrets patterns are built-in to `WorkspacePolicy` only (no YAML override)
- All paths validated: no `..`, no absolute, no `~`, no symlink escape

---

## File Structure

| File | Action | Purpose |
|------|--------|---------|
| `app/workspace/models.py` | Modify | Add `parent` to ProjectInfo, `truncated` to TreeNode |
| `app/workspace/registry.py` | Modify | Pass `parent` from YAML to ProjectInfo |
| `projects.yaml` | Modify | Add quart-platform parent, add parent to children |
| `tests/test_workspace_registry.py` | Modify | Add parent-child test |
| `tests/test_workspace_tools.py` | Create | Smoke tests for all 7 projects |

---

### Task 1: Add `parent` field to ProjectInfo model

**Files:**
- Modify: `app/workspace/models.py:10-18`
- Test: `tests/test_workspace_registry.py`

**Interfaces:**
- Consumes: existing `ProjectInfo` dataclass
- Produces: `ProjectInfo` with optional `parent: str | None = None`

- [ ] **Step 1: Add `parent` field to ProjectInfo**

```python
@dataclass
class ProjectInfo:
    """Metadata for a registered project."""

    project_id: str
    root: Path
    type: str
    description: str
    tags: list[str]
    parent: str | None = None
```

- [ ] **Step 2: Add `truncated` field to TreeNode**

```python
@dataclass
class TreeNode:
    """Single node in a project file tree."""

    name: str
    path: str
    type: str  # "file", "directory", or "symlink"
    size: int = 0
    children: list[TreeNode] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "type": self.type,
            "size": self.size,
        }
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        if self.truncated:
            d["truncated"] = True
        return d
```

- [ ] **Step 3: Run existing tests to verify no breakage**

Run: `pytest tests/test_workspace_policy.py tests/test_app_workspace_registry.py -q`
Expected: PASS (existing tests don't use `parent` yet)

- [ ] **Step 4: Commit**

```bash
git add app/workspace/models.py
git commit -m "feat(workspace): add parent field to ProjectInfo, truncated to TreeNode"
```

---

### Task 2: Update registry loader to pass `parent` from YAML

**Files:**
- Modify: `app/workspace/registry.py:106-126`
- Test: `tests/test_workspace_registry.py`

**Interfaces:**
- Consumes: `ProjectInfo` with `parent` field (Task 1)
- Produces: `load_registry()` populates `parent` from YAML config

- [ ] **Step 1: Update load_registry to read `parent`**

In `app/workspace/registry.py`, inside the `for pid, cfg in projects_raw.items()` loop, change:

```python
        projects[pid] = ProjectInfo(
            project_id=pid,
            root=root,
            type=str(cfg.get("type", "unknown")),
            description=str(cfg.get("description", "")),
            tags=list(cfg.get("tags", [])),
            parent=cfg.get("parent"),
        )
```

- [ ] **Step 2: Update project_info to include parent**

In `app/workspace/registry.py`, in `WorkspaceRegistry.project_info()`, add parent to output:

```python
    def project_info(self, project_id: str) -> dict[str, Any]:
        """Return detailed metadata for a single project."""
        info = self._projects.get(project_id)
        if info is None:
            raise WorkspacePolicyError(f"Unknown project: {project_id}")
        result = {
            "project_id": info.project_id,
            "root": str(info.root),
            "type": info.type,
            "description": info.description,
            "tags": info.tags,
        }
        if info.parent:
            result["parent"] = info.parent
        return result
```

- [ ] **Step 3: Run existing tests**

Run: `pytest tests/test_app_workspace_registry.py -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app/workspace/registry.py
git commit -m "feat(workspace): pass parent field from YAML to ProjectInfo"
```

---

### Task 3: Update projects.yaml with quart-platform parent

**Files:**
- Modify: `projects.yaml`

**Interfaces:**
- Consumes: existing 6 projects
- Produces: 7 projects (1 parent + 6 leaf)

- [ ] **Step 1: Add quart-platform and parent fields**

Replace the `projects:` section of `projects.yaml` with:

```yaml
projects:
  web-ssh-gateway:
    root: web_ssh/web-ssh-gateway
    type: fastapi
    description: "API-first SSH gateway for agents, CI/CD, and infra teams"
    tags: [gateway, ssh, mcp]

  nod-gateway:
    root: NOD_gateway
    type: monorepo
    description: "NOD platform — master server, gateway client, tg bot, web UI, payment"
    tags: [nod, platform]

  quart-platform:
    root: quart-platform
    type: platform
    description: "Quart platform umbrella"
    tags: [quart, platform]

  quart-core:
    root: quart-platform/quart-core
    parent: quart-platform
    type: service
    description: "Quart-core async microframework"
    tags: [quart, async, web]

  kojo-bot-service:
    root: quart-platform/kojo-bot-service
    parent: quart-platform
    type: service
    description: "Kojo Telegram bot service"
    tags: [bot, telegram, aiogram]

  pricetuner-scraper:
    root: scraper
    type: selenium
    description: "PriceTuner price scraping with Selenium"
    tags: [scraper, selenium]

  tg-audio-bot:
    root: tg_audio_bot
    type: aiogram
    description: "Telegram audio processing bot"
    tags: [bot, telegram, audio]
```

- [ ] **Step 2: Verify YAML loads correctly**

Run: `python3 -c "from app.workspace.registry import load_registry; p, r = load_registry('projects.yaml'); print(f'{len(p)} projects loaded'); [print(f'  {k}: parent={v.parent}') for k,v in p.items()]"`

Expected:
```
7 projects loaded
  web-ssh-gateway: parent=None
  nod-gateway: parent=None
  quart-platform: parent=None
  quart-core: parent=quart-platform
  kojo-bot-service: parent=quart-platform
  pricetuner-scraper: parent=None
  tg-audio-bot: parent=None
```

- [ ] **Step 3: Commit**

```bash
git add projects.yaml
git commit -m "feat(workspace): add quart-platform parent, 7 projects total"
```

---

### Task 4: Add parent-child test to registry tests

**Files:**
- Modify: `tests/test_app_workspace_registry.py`

**Interfaces:**
- Consumes: updated `load_registry()` with parent support (Task 2)
- Produces: test verifying parent-child relationship

- [ ] **Step 1: Add test for parent-child relationship**

Add to `tests/test_app_workspace_registry.py`:

```python
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
```

- [ ] **Step 2: Run the new test**

Run: `pytest tests/test_app_workspace_registry.py::test_parent_child_relationship -v`
Expected: PASS

- [ ] **Step 3: Run all registry tests**

Run: `pytest tests/test_app_workspace_registry.py -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_app_workspace_registry.py
git commit -m "test(workspace): add parent-child relationship test"
```

---

### Task 5: Real smoke test across all 7 projects

**Files:**
- Create: `tests/test_workspace_tools.py`

**Interfaces:**
- Consumes: real `projects.yaml`, updated registry with parent support
- Produces: smoke test verifying tools work for all 7 projects

- [ ] **Step 1: Create smoke test file**

```python
"""Smoke tests for workspace tools — real projects.yaml, all 7 projects."""

from __future__ import annotations

import pytest

from app.workspace.tools import project_info, project_tree, workspace_list_projects
from app.workspace.registry import WorkspaceRegistry, reset_registry


@pytest.fixture(autouse=True)
def _reset():
    """Reset registry singleton before each test."""
    reset_registry()
    yield
    reset_registry()


class TestWorkspaceListProjects:
    def test_returns_all_projects(self):
        projects = workspace_list_projects()
        ids = [p["project_id"] for p in projects]
        assert "web-ssh-gateway" in ids
        assert "quart-platform" in ids
        assert "quart-core" in ids
        assert "kojo-bot-service" in ids
        assert "pricetuner-scraper" in ids
        assert "tg-audio-bot" in ids
        assert "nod-gateway" in ids
        assert len(projects) == 7

    def test_project_has_expected_fields(self):
        projects = workspace_list_projects()
        for p in projects:
            assert "project_id" in p
            assert "type" in p
            assert "description" in p
            assert "tags" in p


class TestProjectInfo:
    def test_info_returns_metadata(self):
        info = project_info("web-ssh-gateway")
        assert info["project_id"] == "web-ssh-gateway"
        assert "root" in info
        assert "type" in info

    def test_info_includes_parent(self):
        info = project_info("quart-core")
        assert info.get("parent") == "quart-platform"

    def test_info_no_parent_for_root(self):
        info = project_info("quart-platform")
        assert "parent" not in info

    def test_info_unknown_project(self):
        with pytest.raises(Exception):
            project_info("nonexistent-project")


class TestProjectTree:
    def test_tree_root(self):
        tree = project_tree("web-ssh-gateway")
        assert tree["type"] == "directory"
        assert "children" in tree

    def test_tree_depth_limit(self):
        tree = project_tree("web-ssh-gateway", depth=1)
        for child in tree.get("children", []):
            if child["type"] == "directory":
                assert "children" not in child or child.get("children") is None

    def test_tree_all_projects(self):
        """Smoke: project_tree works for all 7 projects."""
        projects = workspace_list_projects()
        for p in projects:
            tree = project_tree(p["project_id"])
            assert tree["type"] == "directory"
```

- [ ] **Step 2: Run smoke tests**

Run: `pytest tests/test_workspace_tools.py -v`
Expected: PASS

- [ ] **Step 3: Run full test suite**

Run: `pytest -q`
Expected: PASS

- [ ] **Step 4: Run lint and type checks**

Run: `ruff check . && python3 -m mypy .`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_workspace_tools.py
git commit -m "test(workspace): smoke tests for all 7 projects"
```

---

### Task 6: Final verification

- [ ] **Step 1: Run full test suite**

Run: `pytest -q`
Expected: all tests pass

- [ ] **Step 2: Run lint**

Run: `ruff check .`
Expected: no issues

- [ ] **Step 3: Run type check**

Run: `python3 -m mypy .`
Expected: no issues

- [ ] **Step 4: Verify old imports work**

Run: `python3 -c "import app.workspace_policy; import app.workspace_registry; print('Old imports OK')"`
Expected: `Old imports OK`

- [ ] **Step 5: Verify Docker import**

Run: `python3 -c "import app.workspace.registry; import app.workspace.tools; print('Package imports OK')"`
Expected: `Package imports OK`
