"""Verifier cache lifecycle regression tests."""

import fcntl
from pathlib import Path

import mcp_client_tools as tools
import pytest

from examples.mcp_server import verifier_storage as storage


@pytest.mark.parametrize("tool", ["pytest", "ruff", "mypy", "compileall"])
def test_primary_keeps_inherited_lock_until_last_descriptor_closes(tool):
    script = tools._build_primary_script(tool, "/safe/project", ["tests"])
    assert 'LOCK="$PPROJ/.sync.lock"' in script
    assert "flock -u 9" not in script


@pytest.mark.parametrize("tool", ["pytest", "ruff", "mypy"])
def test_fallback_uses_primary_lock_through_entire_check(tool):
    project = "/safe/project"
    root = tools._verifier_project_root(project)
    script = tools._build_readonly_fallback_script(tool, project, ["tests"])
    assert f"PPROJ={root}" in script
    assert 'LOCK="$PPROJ/.sync.lock"' in script
    assert 'LOCK="$TMP_ROOT/.sync.lock"' not in script
    assert "flock -u 9" not in script
    assert f"TMP_ROOT={root}/fallback-u${{_MCP_UID}}-${{_MCP_USER}}" in script


@pytest.mark.parametrize("tool", ["ruff", "compileall"])
def test_standalone_fallback_keeps_shared_lock(tool):
    script = tools._build_standalone_fallback_script(tool, "/safe/project", ["tests"])
    assert 'LOCK="$PPROJ/.sync.lock"' in script
    assert "flock -u 9" not in script
    assert script.index("VERIFIER_LOCK_ACQUIRED") < script.index("VERIFIER_CHECK_PHASE: execute")
    assert "verifier_uv uv" in script


@pytest.fixture
def cache_tree(tmp_path, monkeypatch):
    base = tmp_path / "verifier"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("MCP_VERIFIER_ROOT", str(base))
    # tmp_path is deliberately below world-writable /tmp on CI. Exercise
    # the real owner/device checks, but not production ancestor admission.
    monkeypatch.setattr(storage, "_trusted_base", lambda p: p.lstat().st_dev)
    source = tmp_path / ".mcp-candidate-clones" / "candidate-example"
    source.parent.mkdir()
    root = Path(storage.verifier_project_root(str(source)))
    root.mkdir(mode=0o700)
    lock = root / ".sync.lock"
    lock.touch(mode=0o600)
    for venv in (root / ".venv", root / "fallback-u1000-mcpuser" / ".venv"):
        venv.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (venv / "payload").write_text("generated\n")
    for name in ("pytest", "mypy", "ruff", "pycache"):
        cache = root / "cache" / name
        cache.mkdir(parents=True)
        (cache / "payload").write_text("generated\n")
    (root / "source-receipt").write_text("retain\n")
    (root / "cache" / "unknown").mkdir()
    return source, root, lock


def test_reclaim_exact_terminal_generated_trees_preserves_lock_and_metadata(cache_tree):
    source, root, lock = cache_tree
    identity = lock.stat().st_ino
    with storage.terminal_verifier_lease(source) as lease:
        assert lease.state == "locked"
        result = lease.reclaim(source)
    assert result == {"state": "reclaimed", "removed_trees": 6}
    assert lock.stat().st_ino == identity
    assert (root / "source-receipt").read_text() == "retain\n"
    assert (root / "cache" / "unknown").is_dir()
    assert not (root / ".venv").exists()
    assert not (root / "fallback-u1000-mcpuser" / ".venv").exists()
    with storage.terminal_verifier_lease(source) as lease:
        assert lease.reclaim(source) == {"state": "reclaimed", "removed_trees": 0}


def test_first_use_lock_blocks_writer_before_source_removal(tmp_path, monkeypatch):
    base = tmp_path / "verifier"
    base.mkdir(mode=0o700)
    monkeypatch.setenv("MCP_VERIFIER_ROOT", str(base))
    monkeypatch.setattr(storage, "_trusted_base", lambda path: path.lstat().st_dev)
    source = tmp_path / ".mcp-candidate-clones" / "candidate-first-use"
    source.mkdir(parents=True)
    root = Path(storage.verifier_project_root(str(source)))
    lock = root / ".sync.lock"
    assert not root.exists()
    with storage.terminal_verifier_lease(source) as unadmitted:
        assert unadmitted.state == "absent"
    assert not root.exists()

    with storage.terminal_verifier_lease(source, create_if_missing=True) as lease:
        assert lease.state == "locked"
        inode = lock.stat().st_ino
        with lock.open("rb") as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        source.rmdir()
        assert lease.reclaim(source) == {"state": "reclaimed", "removed_trees": 0}

    assert lock.stat().st_ino == inode
    with lock.open("rb") as contender:
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_active_lock_prevents_reclaim(cache_tree):
    source, root, lock = cache_tree
    with lock.open("rb") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with storage.terminal_verifier_lease(source) as lease:
            assert lease.state == "busy"
            assert lease.reclaim(source) == {"state": "busy", "removed_trees": 0}
    assert (root / ".venv" / "payload").exists()


def test_recreated_source_prevents_reclaim(cache_tree):
    source, root, _ = cache_tree
    source.mkdir()
    with storage.terminal_verifier_lease(source) as lease:
        assert lease.reclaim(source) == {"state": "deferred_unsafe", "removed_trees": 0}
    assert (root / ".venv" / "payload").exists()


def test_unknown_venv_and_symlink_rejected_before_removal(cache_tree, tmp_path):
    source, root, _ = cache_tree
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "preserve").touch()
    (root / ".venv" / "pyvenv.cfg").unlink()
    (root / ".venv" / "pyvenv.cfg").symlink_to(outside / "preserve")
    with storage.terminal_verifier_lease(source) as lease:
        assert lease.reclaim(source) == {"state": "deferred_unsafe", "removed_trees": 0}
    assert (outside / "preserve").is_file()
    assert (root / "cache" / "pytest" / "payload").exists()


def test_symlinked_lock_is_not_opened(cache_tree, tmp_path):
    source, root, lock = cache_tree
    outside = tmp_path / "outside-lock"
    outside.touch()
    lock.unlink()
    lock.symlink_to(outside)
    with storage.terminal_verifier_lease(source) as lease:
        assert lease.state == "deferred_unsafe"
    assert (root / ".venv" / "payload").exists()


def test_mount_rejection_preserves_cache(cache_tree, monkeypatch):
    source, root, _ = cache_tree

    def refuse(_):
        raise ValueError("nested mount")

    monkeypatch.setattr(storage, "_require_no_mounts", refuse)
    with storage.terminal_verifier_lease(source) as lease:
        assert lease.reclaim(source) == {"state": "deferred_unsafe", "removed_trees": 0}
    assert (root / ".venv" / "payload").exists()
