"""Materialize release-owned worker instructions in task state, never source/HOME."""

from __future__ import annotations

import base64
import json
import zlib

from app.skill_pack import SkillPackError, snapshot

INSTALLER = r"""
import base64, hashlib, json, os, stat, sys, tempfile, zlib
from pathlib import Path

payload = json.loads(zlib.decompress(base64.b64decode(__PAYLOAD__)))
config = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}")
if not isinstance(config, dict):
    raise ValueError("Inline OpenCode configuration must be an object")
existing = config.get("skills", {})
if isinstance(existing, list):
    if not all(isinstance(p, str) for p in existing):
        raise ValueError("Existing skills configuration is invalid")
    paths = list(existing)
elif isinstance(existing, dict):
    paths = existing.get("paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError("Existing skills paths configuration is invalid")
else:
    raise ValueError("Existing skills configuration is invalid")

td = Path(sys.argv[1]).resolve(strict=True)
base = td / "skill-context" / payload["pack_sha256"]
def directory(path):
    if path == td:
        return
    directory(path.parent)
    if path.is_symlink():
        raise ValueError("Refusing symlink skill directory")
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError("Skill directory is not a directory")

def write_exact(path, data):
    directory(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".skill-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or f.read(len(data)+1) != data:
                    raise ValueError("Existing skill context differs from release pack")
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.unlink(tmp)

for relative, text in payload["files"].items():
    write_exact(base / "skills" / relative, text.encode("utf-8"))
path = str(base / "skills")
if path not in paths:
    paths = [*paths, path]
if isinstance(existing, list):
    config["skills"] = paths
else:
    config["skills"] = {**existing, "paths": paths}
receipt = {
    "pack_sha256": payload["pack_sha256"], "role": "worker",
    "skills": payload["skills"], "files_available": True,
    "native_loader_verified": False, "loading": "native-skill-on-demand",
    "file_hashes": {p: hashlib.sha256(t.encode()).hexdigest() for p,t in payload["files"].items()},
}
write_exact(td / "skills-receipt.json", json.dumps(receipt, sort_keys=True).encode())
print(json.dumps(config, ensure_ascii=True))
"""


def worker_skill_script_lines() -> list[str]:
    pack = snapshot()
    names = [row["name"] for row in pack["skills"] if "worker" in row["roles"]]
    payload = json.dumps(
        {
            "pack_sha256": pack["pack_sha256"],
            "skills": names,
            "files": {p: text for p, text in pack["files"].items() if p.split("/")[0] in names},
        },
        ensure_ascii=False,
    )
    payload = base64.b64encode(zlib.compress(payload.encode(), 9)).decode("ascii")
    installer = INSTALLER.replace("__PAYLOAD__", repr(payload)).strip()
    if len(installer.encode()) > 60_000:
        raise SkillPackError("Worker skill transport exceeds launch budget")
    return [
        "OPENCODE_CONFIG_CONTENT=$(python3 - \"$td\" <<'GATEWAY_SKILLS_EOF'",
        installer,
        "GATEWAY_SKILLS_EOF",
        ') || { echo "Worker skill context preparation failed" >> "$td/agent-status.md"; exit 73; }',
        "export OPENCODE_CONFIG_CONTENT",
    ]
