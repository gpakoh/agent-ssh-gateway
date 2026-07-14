"""Workspace data structures — ProjectInfo, TreeNode."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProjectInfo:
    """Metadata for a registered project."""

    project_id: str
    root: Path
    type: str
    description: str
    tags: list[str]
    parent: str | None = None


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
