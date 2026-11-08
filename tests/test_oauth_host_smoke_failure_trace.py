"""The CI OAuth failure path emits only fixed-shape DCR phase evidence.

Raw docker logs can contain tokens, cookies, authorization and filesystem
paths; they must never be copied directly into a failed host-smoke job log.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
DCR_STEPS = (
    "OAuth black-box smoke (must be exactly 1 PASS, never SKIP)",
    "Make host-smoke (full suite)",
)


def _dcr_filter() -> str:
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["host-smoke"]["steps"]
    patterns: list[str] = []
    for name in DCR_STEPS:
        step = next(step for step in steps if step.get("name") == name)
        run = step["run"]
        assert "if ! /media/1TB/Anaconda/bin/uv run" in run
        assert "docker logs --since 3m --tail 3000 mcp-oauth 2>&1" in run
        assert "| tail -n 80; then" in run
        assert "|| true" not in run
        assert "No bounded DCR phases available" in run
        assert run.index("if ! ") < run.index("docker logs") < run.index("exit 1")
        assert "::group::Recent OAuth DCR/provisioning phases (fixed-shape only)" in run
        syntax = subprocess.run(
            ["bash", "-n"], input=run, text=True, capture_output=True, timeout=5
        )
        assert syntax.returncode == 0, syntax.stderr
        pattern = re.search(r"\| grep -E '([^']+)'", run)
        assert pattern is not None
        patterns.append(pattern.group(1))
    assert patterns[0] == patterns[1], "both smoke paths must use one safe filter"
    return patterns[0]


def test_host_smoke_failure_logs_only_fixed_shape_dcr_phases() -> None:
    pattern = _dcr_filter()
    safe = (
        "DCR_TRACE id=abcdef012345 phase=received elapsed_ms=2\n"
        "DCR_TRACE id=abcdef012345 phase=upstream_send elapsed_ms=119\n"
        "DCR_STORE_TRACE id=fedcba987654 phase=begin elapsed_ms=0\n"
        "DCR_STORE_TRACE id=fedcba987654 phase=lock_acquired elapsed_ms=5\n"
        "DCR_STORE_TRACE id=fedcba987654 phase=loaded elapsed_ms=7\n"
        "DCR_STORE_TRACE id=fedcba987654 phase=persisted elapsed_ms=42\n"
    )
    unsafe = (
        "Authorization: Bearer opaque-bearer\n"
        "PASSWORD=private\n"
        "DCR_TRACE id=abcdef012345 phase=received elapsed_ms=2 token=private\n"
        "DCR_TRACE id=abcdef012345 phase=unexpected elapsed_ms=2\n"
        "DCR_TRACE id=abcdef012345 phase=received elapsed_ms=2\rSECRET=private\n"
        "DCR_TRACE id=abcdef012345 phase=upstream_headers elapsed_ms=1000000000\n"
        "DCR_TRACE id=abcdef012345 phase=received elapsed_ms=2 /private/path\n"
        "DCR_STORE_TRACE id=fedcba987654 phase=begin elapsed_ms=2 client_id=private\n"
        "DCR_STORE_TRACE id=fedcba987654 phase=unknown elapsed_ms=5\n"
        "DCR_STORE_TRACE id=fedcba987654 phase=loaded elapsed_ms=2\rTOKEN=private\n"
        "DCR_STORE_TRACE id=fedcba987654 phase=persisted elapsed_ms=100000000\n"
    )
    filtered = subprocess.run(
        ["grep", "-E", pattern],
        input=unsafe + safe,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert filtered.stdout == safe
    assert filtered.stderr == ""
    assert "private" not in filtered.stdout
    assert "opaque-bearer" not in filtered.stdout
