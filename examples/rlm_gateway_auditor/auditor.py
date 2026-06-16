"""Experimental RLM-based CI/release auditor for agent-ssh-gateway."""

from __future__ import annotations

import os
import sys

from gateway_tools import (
    ALLOWED_COMMAND_PREFIXES,
    READ_ONLY_SUB_TOOLS,
    gateway_check_auth,
    gateway_check_session,
    gateway_execute_restricted,
    gateway_health,
    gateway_job_result,
    gateway_job_status,
    gateway_read_file,
    gateway_repo_status,
    gateway_wait_job,
)

SYSTEM_CONTEXT = """
You are an experimental OSS maintainer auditor.

Use only the provided gateway_* tools for infrastructure access.
Do not ask for direct SSH, local filesystem access, secrets, or unrestricted shell.
Prefer read-only commands.
Always return:
1. likely root cause
2. evidence
3. minimal fix plan
4. verification commands
"""


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def _subagents_enabled() -> bool:
    return (
        "--enable-subagents" in sys.argv
        or os.environ.get("RLM_ENABLE_SUBAGENTS", "0") == "1"
    )


def _dry_run() -> None:
    print("=" * 60)
    print("RLM Auditor — dry-run mode")
    print("Gateway connectivity & session check")
    print("=" * 60)

    subagents = _subagents_enabled()
    print(f"\n  Subagents:           {'enabled' if subagents else 'disabled'}")
    if subagents:
        print(f"  Subagent tools:      {', '.join(sorted(READ_ONLY_SUB_TOOLS))}")
        print("  Max depth:           2")
    else:
        print("  Subagent tools:      (none)")
        print("  Max depth:           1")
    print(f"  Command allowlist:   enabled ({len(ALLOWED_COMMAND_PREFIXES)} prefixes)")

    checks: list[tuple[str, bool]] = []

    url = os.environ.get("GATEWAY_BASE_URL", "http://localhost:8085").rstrip("/")
    healthy = gateway_health()
    checks.append((f"GET {url}/health → {'200' if healthy else 'FAIL'}", healthy))

    if healthy:
        has_key = bool(os.environ.get("GATEWAY_API_KEY"))
        checks.append(("GATEWAY_API_KEY set", has_key))
        if has_key:
            authed = gateway_check_auth()
            checks.append(
                ("GET /api/ssh/sessions → API key accepted", authed)
            )

        session_id = os.environ.get("GATEWAY_SESSION_ID")
        checks.append(("GATEWAY_SESSION_ID set", bool(session_id)))
        if session_id:
            alive = gateway_check_session(session_id)
            checks.append(
                (f"GET /api/ssh/session/{session_id}/health → alive", alive)
            )
            if alive:
                print("\n--- repo_status smoke ---")
                try:
                    repo = gateway_repo_status(session_id)
                    for name, result in repo.items():
                        ok = result.get("status") == "completed"
                        checks.append((f"  git {name}: {result.get('status', 'FAIL')}", ok))
                        if ok and result.get("stdout"):
                            for line in result["stdout"].strip().splitlines()[:5]:
                                print(f"    {line}")
                except Exception as exc:
                    checks.append((f"  repo_status failed: {exc}", False))

    print("\n--- summary ---")
    failures = 0
    for label, ok in checks:
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"  [{status}] {label}")

    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    if failures:
        raise SystemExit(1)


def main() -> None:
    if "--dry-run" in sys.argv:
        _dry_run()
        return

    if len(sys.argv) < 2:
        raise SystemExit(
            'Usage:'
            '\n  python auditor.py "Investigate why CI is failing"'
            '\n  python auditor.py --dry-run'
            '\n  python auditor.py --enable-subagents "Investigate CI failure"'
        )

    session_id = _require_env("GATEWAY_SESSION_ID")
    task = sys.argv[1]

    from rlm import RLM  # noqa: PLC0415
    from rlm.logger import RLMLogger  # noqa: PLC0415
    logger = RLMLogger(log_dir=os.environ.get("RLM_LOG_DIR", "./logs"))

    subagents = _subagents_enabled()

    rlm = RLM(
        backend="openai",
        backend_kwargs={"model_name": os.environ.get("RLM_MODEL", "gpt-5-nano")},
        environment=os.environ.get("RLM_ENVIRONMENT", "local"),
        max_depth=int(os.environ.get("RLM_MAX_DEPTH", "2" if subagents else "1")),
        max_iterations=int(os.environ.get("RLM_MAX_ITERATIONS", "8")),
        max_timeout=int(os.environ.get("RLM_MAX_TIMEOUT", "180")),
        max_concurrent_subcalls=int(os.environ.get("RLM_MAX_CONCURRENT_SUBCALLS", "2")),
        custom_tools={
            "gateway_execute_restricted": gateway_execute_restricted,
            "gateway_job_status": gateway_job_status,
            "gateway_job_result": gateway_job_result,
            "gateway_wait_job": gateway_wait_job,
            "gateway_read_file": gateway_read_file,
            "gateway_repo_status": gateway_repo_status,
        },
        custom_sub_tools=READ_ONLY_SUB_TOOLS if subagents else {},
        logger=logger,
        verbose=True,
    )

    prompt = f"{SYSTEM_CONTEXT}\n\nSession ID: {session_id}\n\nTask: {task}"
    result = rlm.completion(prompt)
    print(result.response)


if __name__ == "__main__":
    main()
