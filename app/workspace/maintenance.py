"""Cooperative inode interlock for managed candidate file maintenance.

The candidate directory itself is the lock object. There is no stale lock
pathname to create or unlink, and separate mount aliases of the same inode
contend. This is not a fence against nonparticipating writers.
"""
from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.workspace.policy import WorkspacePolicyError


class CandidateMaintenanceContended(WorkspacePolicyError):
    """A managed candidate file operation cannot acquire its admission."""


def managed_candidate_root(root: Path) -> Path | None:
    """Resolve the enclosing managed clone, including registered subroots."""
    parts = root.parts
    for i, part in enumerate(parts[:-1]):
        if part == ".mcp-candidate-clones":
            return Path(*parts[:i + 2])
    return None


@contextmanager
def candidate_directory_admission(root: Path, *, exclusive: bool) -> Iterator[None]:
    """Acquire a nonblocking shared writer or exclusive maintenance lock."""
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise CandidateMaintenanceContended("candidate maintenance directory unavailable") from exc
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            held = os.fstat(fd)
            visible = root.lstat()
            if (held.st_dev, held.st_ino) != (visible.st_dev, visible.st_ino):
                raise OSError("directory changed")
        except OSError as exc:
            raise CandidateMaintenanceContended("candidate maintenance admission unavailable") from exc
        yield
    finally:
        os.close(fd)
