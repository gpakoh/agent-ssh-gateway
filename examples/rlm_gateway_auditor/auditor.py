"""Experimental RLM-based CI/release auditor for agent-ssh-gateway."""

from __future__ import annotations

import os
import sys

from gateway_tools import (
    gateway_execute,
    gateway_job_result,
    gateway_job_status,
    gateway_read_file,
    gateway_repo_status,
    gateway_wait_job,
)
from rlm import RLM
from rlm.logger import RLMLogger

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


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit('Usage: python auditor.py "Investigate why CI is failing"')

    session_id = os.environ.get("GATEWAY_SESSION_ID")
    if not session_id:
        raise SystemExit("GATEWAY_SESSION_ID is required")

    task = sys.argv[1]
    logger = RLMLogger(log_dir=os.environ.get("RLM_LOG_DIR", "./logs"))

    rlm = RLM(
        backend="openai",
        backend_kwargs={"model_name": os.environ.get("RLM_MODEL", "gpt-5-nano")},
        environment=os.environ.get("RLM_ENVIRONMENT", "local"),
        max_depth=int(os.environ.get("RLM_MAX_DEPTH", "1")),
        max_iterations=int(os.environ.get("RLM_MAX_ITERATIONS", "8")),
        max_timeout=int(os.environ.get("RLM_MAX_TIMEOUT", "180")),
        max_concurrent_subcalls=int(os.environ.get("RLM_MAX_CONCURRENT_SUBCALLS", "2")),
        custom_tools={
            "gateway_execute": gateway_execute,
            "gateway_job_status": gateway_job_status,
            "gateway_job_result": gateway_job_result,
            "gateway_wait_job": gateway_wait_job,
            "gateway_read_file": gateway_read_file,
            "gateway_repo_status": gateway_repo_status,
        },
        custom_sub_tools={},
        logger=logger,
        verbose=True,
    )

    prompt = f"{SYSTEM_CONTEXT}\n\nSession ID: {session_id}\n\nTask: {task}"
    result = rlm.completion(prompt)
    print(result.response)


if __name__ == "__main__":
    main()
