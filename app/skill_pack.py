"""Repo-owned instructions; discovery never executes a skill or grants authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

import yaml

PACK_ROOT = Path(__file__).resolve().parents[1] / "skills"
MAX_FILE_BYTES = 32_768
MAX_PACK_BYTES = 196_608
SKILLS: dict[str, dict[str, Any]] = {
    "gateway-finish-task": {
        "title": "Доведение задачи",
        "group": "agents",
        "roles": ["controller"],
        "summary": "CI, разрешённый merge/release, отчёт в Issue и очистка своей работы.",
        "resources": ["SKILL.md", "references/closeout.md"],
    },
    "gateway-code-quality": {
        "title": "Качество кода",
        "group": "workspace",
        "roles": ["controller", "worker"],
        "summary": "Причина дефекта, небольшой scope и проверка поведения.",
        "resources": ["SKILL.md", "references/review.md"],
    },
    "site-design": {
        "title": "Дизайн сайтов",
        "group": "sites",
        "roles": ["controller", "worker"],
        "summary": "Структура, бренд, адаптивность и доступность сайтов, включая Xloud.",
        "resources": ["SKILL.md", "references/review.md", "references/asset-transfer.md"],
    },
    "site-scroll-video": {
        "title": "Видео по прокрутке",
        "group": "sites",
        "roles": ["controller", "worker"],
        "summary": "Кадры из видео, прокрутка секции, бюджет памяти и статичный fallback.",
        "resources": ["SKILL.md", "references/implementation.md"],
    },
}


class SkillPackError(ValueError):
    """Release-owned instructions are missing or malformed."""


def validate_role(role: str) -> None:
    if role not in ("controller", "worker"):
        raise ValueError("role must be controller or worker")


def _read(root: Path, name: str, resource: str) -> str:
    path = root
    for part in (name, *resource.split("/")):
        path = path / part
        if path.is_symlink():
            raise SkillPackError("Skill pack contains a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise SkillPackError("Skill resource must be a regular file")
            data = source.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise SkillPackError("Skill resource exceeds size limit")
        return data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise SkillPackError("Bundled skill resource unavailable") from exc


def snapshot(root: Path = PACK_ROOT) -> dict[str, Any]:
    """Validate the complete fixed allowlist before exposing any instructions."""
    files: dict[str, str] = {}
    rows = []
    for name, metadata in SKILLS.items():
        for resource in metadata["resources"]:
            files[f"{name}/{resource}"] = _read(root, name, resource)
        text = files[f"{name}/SKILL.md"]
        match = re.match(r"\A---\n(.*?)\n---(?:\n|$)", text, re.S)
        if match is None:
            raise SkillPackError("Skill frontmatter is missing")
        try:
            front = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise SkillPackError("Invalid skill frontmatter") from exc
        if not isinstance(front, dict) or front.get("name") != name:
            raise SkillPackError("Skill name does not match its directory")
        description = front.get("description")
        if not isinstance(description, str) or not 1 <= len(description) <= 1024:
            raise SkillPackError("Skill description must contain 1-1024 characters")
        rows.append({"name": name, **metadata})
    if sum(len(text.encode()) for text in files.values()) > MAX_PACK_BYTES:
        raise SkillPackError("Skill pack exceeds size limit")
    digest = hashlib.sha256(
        json.dumps(
            {"version": 1, "skills": rows, "files": files},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {"pack_sha256": digest, "skills": rows, "files": files}


def skill_metadata(pack: dict[str, Any], role: str = "controller") -> list[dict[str, Any]]:
    validate_role(role)
    return [
        {**row, "load": {"tool": "gateway_skill", "arguments": {"name": row["name"], "role": role}}}
        for row in pack["skills"]
        if role in row["roles"]
    ]


def read_skill(
    name: str,
    resource: str = "SKILL.md",
    *,
    role: str = "controller",
    expected_pack_sha256: str | None = None,
    root: Path = PACK_ROOT,
) -> dict[str, Any]:
    validate_role(role)
    if name not in SKILLS or role not in SKILLS[name]["roles"]:
        raise ValueError("Unknown skill for selected role")
    if resource not in SKILLS[name]["resources"]:
        raise ValueError("Unknown skill resource; use the advertised resource names")
    pack = snapshot(root)
    if expected_pack_sha256 is not None and expected_pack_sha256 != pack["pack_sha256"]:
        raise ValueError("Skill pack changed; refresh gateway_catalog")
    content = pack["files"][f"{name}/{resource}"]
    return {
        "name": name,
        "role": role,
        "resource": resource,
        "content": content,
        "pack_sha256": pack["pack_sha256"],
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "resources": SKILLS[name]["resources"],
        "source": "gateway-release",
        "authority": "Instructions only; tool scopes and task launch constraints still apply.",
    }
