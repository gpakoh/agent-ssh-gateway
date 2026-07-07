#!/usr/bin/env python3
"""OpenCode runner wrapper — execute a task plan through OpenCode CLI.

Usage:
    python3 scripts/opencode_runner_wrapper.py \\
        --task-id 2026-06-25-fix-auth-opencode \\
        --project my-project \\
        [--command "opencode run ..."] \\
        [--opencode-bin /path/to/opencode] \\
        [--timeout 300] \\
        [--dry-run]

Contract:
    Input:
        task_id         — validated .ai-bridge task ID
        project         — project name under MCP_GATEWAY_PROJECT_ROOT
        command         — command to run (default: read current-plan.md and run opencode)
        opencode_bin    — path to opencode binary (default: $OPENCODE_BIN or /root/.opencode/bin/opencode)
        timeout_sec     — max runtime in seconds (default 300)
        workdir         — working directory override
        dry_run         — if true, log intent but do not execute

    Output (dict):
        status          — "completed" | "failed" | "timeout" | "dry-run"
        exit_code       — int or null
        stdout          — tail (last 200 lines)
        stderr          — tail (last 200 lines)
        log_file        — path to full log in .ai-bridge/tasks/<task_id>/opencode-run.log
        result_file     — path to result summary
        started_at      — ISO timestamp
        finished_at     — ISO timestamp | null
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

TASKS_REL_DIR = ".ai-bridge/tasks"
ARCHIVE_REL_DIR = ".ai-bridge/archive"
DEFAULT_OPENCODE_BIN = "/root/.opencode/bin/opencode"
DEFAULT_TIMEOUT_SEC = 300
OUTPUT_TAIL_LINES = 200
TASK_ID_RE_PATTERN = r"^[a-z0-9][a-z0-9-]{10,120}$"

TASK_ID_RE = re.compile(TASK_ID_RE_PATTERN)


def validate_task_id(task_id: str) -> None:
    if not TASK_ID_RE.match(task_id):
        raise ValueError(f"Invalid task_id: {task_id!r}. Must match {TASK_ID_RE_PATTERN}")


def _task_dir(task_id: str) -> str:
    return f"{TASKS_REL_DIR}/{task_id}"


def _archive_dir(task_id: str) -> str:
    return f"{ARCHIVE_REL_DIR}/{task_id}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _tail(text: str, n: int = OUTPUT_TAIL_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[-n:])


def find_opencode_bin(opencode_bin: str | None) -> str:
    if opencode_bin and os.path.isfile(opencode_bin):
        return opencode_bin
    env_val = os.environ.get("OPENCODE_BIN")
    if env_val and os.path.isfile(env_val):
        return env_val
    if os.path.isfile(DEFAULT_OPENCODE_BIN):
        return DEFAULT_OPENCODE_BIN
    which = shutil.which("opencode")
    if which:
        return which
    raise FileNotFoundError(
        f"OpenCode binary not found. Tried: {opencode_bin!r}, "
        f"$OPENCODE_BIN={env_val!r}, {DEFAULT_OPENCODE_BIN!r}, PATH"
    )


def resolve_project_root(project: str) -> str:
    base = os.environ.get("MCP_GATEWAY_PROJECT_ROOT", "")
    if not base:
        base = os.environ.get("HOME", "/root")
    if project:
        return os.path.join(base, project)
    return os.getcwd()


def read_task_file(project_root: str, task_id: str, filename: str) -> str | None:
    path = os.path.join(project_root, TASKS_REL_DIR, task_id, filename)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return f.read()


def write_task_file(project_root: str, task_id: str, filename: str, content: str) -> str:
    task_dir = os.path.join(project_root, _task_dir(task_id))
    os.makedirs(task_dir, exist_ok=True)
    path = os.path.join(task_dir, filename)
    with open(path, "w") as f:
        f.write(content)
    return path


def run_opencode(
    opencode_bin: str,
    workdir: str,
    args: list[str],
    timeout_sec: int,
) -> dict[str, Any]:
    cmd = [opencode_bin] + args
    started_at = _now_iso()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    exit_code: int | None = None
    timed_out = False

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout_sec)
            stdout_parts.append(stdout_bytes)
            stderr_parts.append(stderr_bytes)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_partial, stderr_partial = proc.communicate(timeout=10)
            stdout_parts.append(stdout_partial)
            stderr_parts.append(stderr_partial)
            exit_code = -1
            timed_out = True
    except FileNotFoundError:
        return {
            "status": "failed",
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Binary not found: {opencode_bin}",
            "started_at": started_at,
            "finished_at": _now_iso(),
            "timed_out": False,
        }

    finished_at = _now_iso()
    stdout_full = "".join(stdout_parts)
    stderr_full = "".join(stderr_parts)

    if timed_out:
        status = "timeout"
    elif exit_code == 0:
        status = "completed"
    else:
        status = "failed"

    return {
        "status": status,
        "exit_code": exit_code,
        "stdout": _tail(stdout_full),
        "stderr": _tail(stderr_full),
        "stdout_full_size": len(stdout_full),
        "stderr_full_size": len(stderr_full),
        "started_at": started_at,
        "finished_at": finished_at,
        "timed_out": timed_out,
    }


def build_result_summary(
    task_id: str,
    run_result: dict[str, Any],
    command: str,
    opencode_bin: str,
) -> str:
    return (
        f"# OpenCode Runner Result — {task_id}\n\n"
        f"- **Status**: {run_result['status']}\n"
        f"- **Exit code**: {run_result['exit_code']}\n"
        f"- **Command**: `{command}`\n"
        f"- **Binary**: `{opencode_bin}`\n"
        f"- **Started**: {run_result['started_at']}\n"
        f"- **Finished**: {run_result['finished_at']}\n"
        f"- **Timed out**: {run_result.get('timed_out', False)}\n\n"
        f"## stdout (tail)\n\n```\n{run_result['stdout']}\n```\n\n"
        f"## stderr (tail)\n\n```\n{run_result['stderr']}\n```\n"
    )


def run_wrapper(
    *,
    task_id: str,
    project: str,
    command: str | None = None,
    opencode_bin: str | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    workdir: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    validate_task_id(task_id)

    project_root = resolve_project_root(project)
    started_at = _now_iso()

    if not os.path.isdir(project_root):
        return {
            "task_id": task_id,
            "status": "failed",
            "exit_code": None,
            "stdout": "",
            "stderr": f"Project root not found: {project_root}",
            "log_file": "",
            "result_file": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    os.path.join(project_root, _task_dir(task_id))

    if command:
        resolved_cmd = command
    else:
        plan = read_task_file(project_root, task_id, "current-plan.md")
        if plan:
            resolved_cmd = (
                f"opencode run --never-ask "
                f'"Read .ai-bridge/tasks/{task_id}/current-plan.md and execute the plan. '
                f"Save diff to .ai-bridge/tasks/{task_id}/implementation-diff.patch. "
                f'Update agent-status.md as you go."'
            )
        else:
            resolved_cmd = f"echo 'No plan found for {task_id}'"

    if dry_run:
        return {
            "task_id": task_id,
            "status": "dry-run",
            "exit_code": None,
            "stdout": f"[DRY-RUN] Would run: {resolved_cmd}",
            "stderr": "",
            "log_file": "",
            "result_file": "",
            "started_at": started_at,
            "finished_at": _now_iso(),
        }

    opencode_bin_resolved = find_opencode_bin(opencode_bin)

    exec_workdir = workdir or project_root

    run_result = run_opencode(
        opencode_bin=opencode_bin_resolved,
        workdir=exec_workdir,
        args=resolved_cmd.split()[1:] if " " in resolved_cmd else [resolved_cmd],
        timeout_sec=timeout_sec,
    )

    result_summary = build_result_summary(
        task_id=task_id,
        run_result=run_result,
        command=resolved_cmd,
        opencode_bin=opencode_bin_resolved,
    )

    log_path = write_task_file(
        project_root,
        task_id,
        "opencode-run.log",
        f"COMMAND: {resolved_cmd}\n"
        f"STARTED: {run_result['started_at']}\n"
        f"FINISHED: {run_result['finished_at']}\n"
        f"EXIT CODE: {run_result['exit_code']}\n"
        f"STATUS: {run_result['status']}\n"
        f"TIMED OUT: {run_result.get('timed_out', False)}\n"
        f"{'=' * 60}\n"
        f"STDOUT:\n{run_result['stdout']}\n"
        f"{'=' * 60}\n"
        f"STDERR:\n{run_result['stderr']}\n",
    )

    result_path = write_task_file(
        project_root,
        task_id,
        "opencode-result.md",
        result_summary,
    )

    return {
        "task_id": task_id,
        "status": run_result["status"],
        "exit_code": run_result["exit_code"],
        "stdout": run_result["stdout"],
        "stderr": run_result["stderr"],
        "log_file": log_path,
        "result_file": result_path,
        "started_at": run_result["started_at"],
        "finished_at": run_result["finished_at"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenCode runner wrapper for agent handoff tasks")
    parser.add_argument("--task-id", help="Agent handoff task ID")
    parser.add_argument("--project", help="Project name")
    parser.add_argument("--command", help="Override command (default: read current-plan.md)")
    parser.add_argument("--opencode-bin", help="Path to opencode binary")
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, help="Timeout in seconds"
    )
    parser.add_argument("--workdir", help="Working directory override")
    parser.add_argument("--dry-run", action="store_true", help="Log intent without executing")
    parser.add_argument("--self-test", action="store_true", help="Run self-test and exit")

    args = parser.parse_args()

    if args.self_test:
        result = self_test()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(0 if result.get("status") == "completed" else 1)

    if not args.task_id or not args.project:
        parser.error("--task-id and --project are required (unless --self-test)")

    result = run_wrapper(
        task_id=args.task_id,
        project=args.project,
        command=args.command,
        opencode_bin=args.opencode_bin,
        timeout_sec=args.timeout,
        workdir=args.workdir,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def self_test() -> dict[str, Any]:
    tests = []
    all_passed = True

    try:
        validate_task_id("a12345678901")
        tests.append({"name": "validate_valid", "passed": True})
    except ValueError as e:
        tests.append({"name": "validate_valid", "passed": False, "error": str(e)})
        all_passed = False

    for bad_id in ["", "too-short", "UPPERCASE"]:
        try:
            validate_task_id(bad_id)
            tests.append(
                {"name": f"reject_{bad_id}", "passed": False, "error": "should have raised"}
            )
            all_passed = False
        except ValueError:
            tests.append({"name": f"reject_{bad_id}", "passed": True})

    try:
        op = find_opencode_bin(None)
        tests.append({"name": "find_opencode_bin_default", "passed": True, "found": op})
    except FileNotFoundError as e:
        tests.append({"name": "find_opencode_bin_default", "passed": False, "error": str(e)})
        all_passed = False

    result = run_wrapper(
        task_id="b23456789012",
        project="",
        dry_run=True,
    )
    if result["status"] == "dry-run":
        tests.append({"name": "dry_run_mode", "passed": True})
    else:
        tests.append({"name": "dry_run_mode", "passed": False, "error": f"got {result['status']}"})
        all_passed = False

    return {
        "status": "completed" if all_passed else "failed",
        "tests": tests,
        "summary": f"{sum(1 for t in tests if t['passed'])}/{len(tests)} passed",
    }


if __name__ == "__main__":
    main()
