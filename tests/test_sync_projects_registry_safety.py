"""Regression tests for append-only, race-safe projects registry sync."""

from __future__ import annotations

import fcntl
import sys
from pathlib import Path

import pytest

from scripts import sync_projects_registry as sync


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "clones"
    (root / "new-service").mkdir(parents=True)
    registry_file = tmp_path / "projects.yaml"
    registry_file.write_text(
        f"registry_root: {root}\nprojects:\n"
        '  existing:\n    root: existing\n    type: service\n',
        encoding="utf-8",
    )
    for key in ("GITEA_API_BASE", "GITEA_TOKEN", "GITEA_OWNER"):
        monkeypatch.setenv(key, "test")
    monkeypatch.setattr(
        sync, "_fetch_gitea_repos", lambda *_: {"new-service": "New service"}
    )
    monkeypatch.setattr(
        sync, "_find_local_clones", lambda *_: {"new-service": root / "new-service"}
    )
    return registry_file, root


def test_sync_dry_run_then_append_preserves_existing_and_is_idempotent(
    registry: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path, _root = registry
    original = yaml_path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["sync", "--yaml", str(yaml_path)])
    assert sync.main() == 0
    assert yaml_path.read_bytes() == original

    monkeypatch.setattr(sys, "argv", ["sync", "--apply", "--yaml", str(yaml_path)])
    assert sync.main() == 0
    first = yaml_path.read_bytes()
    assert first.startswith(original)
    assert first.count(b"  new-service:") == 1
    assert sync.main() == 0
    assert yaml_path.read_bytes() == first


def test_apply_refuses_stale_snapshot_without_overwriting_manual_change(
    registry: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path, root = registry
    manual = "  manual-entry:\n    root: preserved\n"

    def concurrent_edit(*_args: object) -> dict[str, Path]:
        with yaml_path.open("a", encoding="utf-8") as stream:
            stream.write(manual)
        return {"new-service": root / "new-service"}

    monkeypatch.setattr(sync, "_find_local_clones", concurrent_edit)
    monkeypatch.setattr(sys, "argv", ["sync", "--apply", "--yaml", str(yaml_path)])
    assert sync.main() == 2
    current = yaml_path.read_text(encoding="utf-8")
    assert current.endswith(manual)
    assert "  new-service:" not in current


def test_apply_refuses_symlink_without_modifying_target(
    registry: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path, _root = registry
    link = yaml_path.with_name("linked-projects.yaml")
    link.symlink_to(yaml_path)
    original = yaml_path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["sync", "--apply", "--yaml", str(link)])
    assert sync.main() == 2
    assert yaml_path.read_bytes() == original


def test_apply_refuses_concurrent_sync_lock_without_writing(
    registry: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path, _root = registry
    original = yaml_path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["sync", "--apply", "--yaml", str(yaml_path)])
    with yaml_path.open("rb") as reader:
        fcntl.flock(reader.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert sync.main() == 2
    assert yaml_path.read_bytes() == original
