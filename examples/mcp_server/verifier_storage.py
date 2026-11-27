"""Shared verifier identity and terminal-candidate generated-cache lifecycle.

Only the existing guarded candidate cleanup owns deletion admission. A lease
covers the exact same persistent lock inode as all verifier writers. Never
unlink that inode, even after candidate removal: a queued writer may have
opened it before the terminal transaction. Unknown/legacy trees stay intact.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

_ROOT_ENV = "MCP_VERIFIER_ROOT"
_ROOT_DEFAULT = "/var/lib/mcp-agent/verifier"
_CACHE_NAMES = ("pytest", "mypy", "ruff", "pycache")
_FALLBACK_NAME = re.compile(r"fallback-u[0-9]+-[A-Za-z0-9_.-]+\Z")
_MAX_ENTRIES = 200_000


def verifier_root_config() -> str:
    value = os.environ.get(_ROOT_ENV, "").strip() or _ROOT_DEFAULT
    if "\x00" in value:
        raise ValueError(f"{_ROOT_ENV} contains a NUL byte")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{_ROOT_ENV} must be an absolute executor path")
    if str(path) == "/":
        raise ValueError(f"{_ROOT_ENV} must not be filesystem root")
    return str(path).rstrip("/")


def verifier_project_root(project_dir: str) -> str:
    proj = str(Path(project_dir).resolve())
    ident = hashlib.sha1(proj.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(proj).name) or "project"
    return f"{verifier_root_config()}/{slug}-{ident}"


def _stat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _real_owned_dir(path: Path, device: int) -> os.stat_result:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_dev != device
        or info.st_uid != os.geteuid()
    ):
        raise ValueError("unsafe verifier directory")
    return info


def _trusted_base(base: Path) -> int:
    for ancestor in (base, *base.parents):
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError("unsafe verifier ancestry")
    info = base.lstat()
    _real_owned_dir(base, info.st_dev)
    # Descendant modes may reflect uv's package modes or a legacy umask.
    # Only the executor UID may traverse this volume subtree at all.
    if info.st_mode & 0o077:
        raise ValueError("verifier root must be private to its owner")
    return info.st_dev


def _lock_identity(info: os.stat_result, device: int) -> tuple[int, int]:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_dev != device
        or info.st_nlink != 1
    ):
        raise ValueError("unsafe verifier lock")
    return info.st_dev, info.st_ino


def _require_no_mounts(root: Path) -> None:
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        for line in stream:
            mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[4]))
            if mount == root or root in mount.parents:
                raise ValueError("unexpected verifier mount")


def _generated_venv(path: Path, device: int) -> bool:
    if _stat(path) is None:
        return False
    _real_owned_dir(path, device)
    cfg = (path / "pyvenv.cfg").lstat()
    if not stat.S_ISREG(cfg.st_mode) or cfg.st_uid != os.geteuid() or cfg.st_dev != device:
        raise ValueError("unrecognized verifier environment")
    return True


def _generated_trees(root: Path, device: int) -> list[Path]:
    targets = []
    if _generated_venv(root / ".venv", device):
        targets.append(root / ".venv")
    cache = root / "cache"
    if _stat(cache) is not None:
        _real_owned_dir(cache, device)
        for name in _CACHE_NAMES:
            child = cache / name
            if _stat(child) is not None:
                _real_owned_dir(child, device)
                targets.append(child)
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= 256:
                raise ValueError("verifier root inventory exceeded")
            if _FALLBACK_NAME.fullmatch(entry.name):
                child = root / entry.name
                _real_owned_dir(child, device)
                if _generated_venv(child / ".venv", device):
                    targets.append(child / ".venv")
    return targets


def _require_owned_trees(targets: list[Path], device: int) -> None:
    pending = list(targets)
    count = 0
    while pending:
        directory = pending.pop()
        _real_owned_dir(directory, device)
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_ENTRIES:
                    raise ValueError("verifier tree inventory exceeded")
                info = entry.stat(follow_symlinks=False)
                if info.st_dev != device or info.st_uid != os.geteuid():
                    raise ValueError("foreign verifier entry")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                    raise ValueError("special verifier entry")


@dataclass(frozen=True)
class VerifierCacheLease:
    state: str
    root: Path | None = None
    fd: int | None = None
    device: int | None = None

    def reclaim(self, candidate_root: Path) -> dict[str, int | str]:
        """Run only after the guarded candidate transaction proved absence."""
        removed = 0
        if self.state != "locked":
            return {"state": self.state, "removed_trees": removed}
        assert self.root is not None and self.fd is not None and self.device is not None
        try:
            if _stat(candidate_root) is not None:
                raise ValueError("candidate still present")
            if self.root != Path(verifier_project_root(str(candidate_root))):
                raise ValueError("candidate verifier identity changed")
            if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
                raise ValueError("safe removal unavailable")
            _real_owned_dir(self.root, self.device)
            identity = _lock_identity(os.fstat(self.fd), self.device)
            if identity != _lock_identity((self.root / ".sync.lock").lstat(), self.device):
                raise ValueError("verifier lock changed")
            _require_no_mounts(self.root)
            targets = _generated_trees(self.root, self.device)
            _require_owned_trees(targets, self.device)
            for path in targets:
                if _stat(candidate_root) is not None:
                    raise ValueError("candidate recreated")
                shutil.rmtree(path)
                removed += 1
            return {"state": "reclaimed", "removed_trees": removed}
        except (OSError, ValueError, IndexError):
            # Source delivery is already complete. Do not overwrite its
            # receipt or claim a partial/uncertain cache removal succeeded.
            return {"state": "deferred_unsafe", "removed_trees": removed}


def _acquire(candidate_root: Path) -> VerifierCacheLease:
    fd = None
    try:
        if (
            candidate_root.parent.name != ".mcp-candidate-clones"
            or not candidate_root.name.startswith("candidate-")
            or candidate_root.is_symlink()
        ):
            return VerifierCacheLease("deferred_unsafe")
        base = Path(verifier_root_config())
        if _stat(base) is None:
            return VerifierCacheLease("absent")
        device = _trusted_base(base)
        root = Path(verifier_project_root(str(candidate_root)))
        if root.parent != base:
            raise ValueError("verifier identity escaped root")
        if _stat(root) is None:
            return VerifierCacheLease("absent")
        _real_owned_dir(root, device)
        lock = root / ".sync.lock"
        expected = _lock_identity(lock.lstat(), device)
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        if expected != _lock_identity(os.fstat(fd), device):
            raise ValueError("verifier lock changed")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return VerifierCacheLease("busy")
        if expected != _lock_identity(lock.lstat(), device):
            raise ValueError("verifier lock changed")
        return VerifierCacheLease("locked", root, fd, device)
    except (OSError, ValueError):
        if fd is not None:
            os.close(fd)
        return VerifierCacheLease("deferred_unsafe")


@contextlib.contextmanager
def terminal_verifier_lease(candidate_root: Path) -> Iterator[VerifierCacheLease]:
    lease = _acquire(candidate_root)
    try:
        yield lease
    finally:
        if lease.fd is not None:
            os.close(lease.fd)
