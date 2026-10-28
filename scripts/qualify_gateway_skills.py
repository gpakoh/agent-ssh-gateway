"""Verify actual native skill discovery without a model, credentials or production writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def qualify(binary: Path) -> dict[str, object]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.skill_pack import snapshot
    from app.worker_skills import worker_skill_script_lines

    binary = binary.resolve(strict=True)
    pack = snapshot()
    expected = {r["name"] for r in pack["skills"] if "worker" in r["roles"]}
    with tempfile.TemporaryDirectory(prefix="gateway-skills-qualification-") as scratch:
        root = Path(scratch)
        task, work = root / "task", root / "workspace"
        task.mkdir()
        work.mkdir()
        # Do not inherit provider credentials, project configuration or user caches.
        env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}
        for kind in ("DATA", "CACHE", "CONFIG"):
            path = root / kind.lower()
            path.mkdir()
            env[f"XDG_{kind}_HOME"] = str(path)
        script = (
            'td="$1"\n'
            + "\n".join(worker_skill_script_lines())
            + '\nprintf "%s" "$OPENCODE_CONFIG_CONTENT"\n'
        )
        result = subprocess.run(
            ["sh", "-s", "--", str(task)],
            input=script,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        env["OPENCODE_CONFIG_CONTENT"] = result.stdout
        version = subprocess.run(
            [str(binary), "--version"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
        discovered = subprocess.run(
            [str(binary), "debug", "skill"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        rows = json.loads(discovered.stdout)
        if not isinstance(rows, list):
            raise ValueError("Native skill discovery did not return a list")
        selected = [r for r in rows if isinstance(r, dict) and r.get("name") in expected]
        if len(selected) != len(expected) or {r["name"] for r in selected} != expected:
            raise ValueError("Native skill discovery did not return all worker skills")
        for row in selected:
            path = (
                task / "skill-context" / pack["pack_sha256"] / "skills" / row["name"] / "SKILL.md"
            )
            if Path(row.get("location", "")).resolve() != path.resolve():
                raise ValueError("Native skill discovery resolved a different instruction source")
            if path.read_text() != pack["files"][f"{row['name']}/SKILL.md"]:
                raise ValueError("Native skill source differs from release pack")
            expected_content = path.read_text().split("\n---\n", 1)[1]
            if row.get("content") != expected_content:
                raise ValueError("Native skill content differs from release instructions")
        if any(isinstance(r, dict) and r.get("name") == "gateway-finish-task" for r in rows):
            raise ValueError("Controller-only skill unexpectedly visible to worker")
        receipt = json.loads((task / "skills-receipt.json").read_text())
        return {
            "ok": True,
            "opencode_version": version,
            "pack_sha256": pack["pack_sha256"],
            "skills": sorted(expected),
            "native_discovery_verified": True,
            "file_hashes": receipt["file_hashes"],
            "content_hashes": {
                r["name"]: hashlib.sha256(r["content"].encode()).hexdigest() for r in selected
            },
            "model_called": False,
            "production_changed": False,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opencode-bin", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = qualify(args.opencode_bin)
    except (OSError, ValueError, subprocess.SubprocessError):
        print(json.dumps({"ok": False, "error": "Native skill qualification failed"}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
