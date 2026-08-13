"""Central safe serializer for job records.

Every API surface that exposes a job (result, list, wait, dead-letter,
bulk) funnels the record through :func:`serialize_job` so command output
redaction and host-path leakage apply uniformly. JobRecord and Redis
storage keep raw values (audit trail); redaction happens at the API
boundary.
"""

from __future__ import annotations

from typing import Any

from app.job_manager import TERMINAL_STATES, JobRecord
from app.security import redact_secrets


def serialize_job(
    job: JobRecord | dict,
    *,
    redact: bool,
    include_output: bool = True,
) -> dict[str, Any]:
    """Serialize a job record into a safe public dict.

    ``redact`` applies :func:`redact_secrets` to command/stdout/stderr/
    error_message. ``include_output=False`` drops stdout/stderr entirely —
    the right shape for list endpoints (status/exit_code/timestamps only).

    Accepts a JobRecord or the plain-dict shape stored in Redis (which
    keys the id as ``id`` and the failure reason as ``error``). Shape of
    the input is preserved (extra keys like ``progress`` or
    ``wait_timed_out`` pass through), only the sensitive fields are
    replaced with their redacted form.
    """
    if isinstance(job, JobRecord):
        redact_path_prefix = job.redact_path_prefix
        data = job.to_dict()
    else:
        data = dict(job)
        redact_path_prefix = data.pop("redact_path_prefix", None)
        if "id" in data and "job_id" not in data:
            data["job_id"] = data.pop("id")
        if "error" in data and "error_message" not in data:
            data["error_message"] = data.pop("error")
        if str(data.get("status", "")) in TERMINAL_STATES:
            if "created_at" not in data:
                completed_at = data.get("completed_at")
                data["created_at"] = (
                    completed_at if isinstance(completed_at, (int, float)) else 0.0
                )
            data.setdefault("session_id", "")
            data.setdefault("command", "")
            data.setdefault("progress", {})

    def _redact(value: Any) -> Any:
        if not redact or not isinstance(value, str):
            return value
        if redact_path_prefix:
            prefix = redact_path_prefix.rstrip("/")
            if prefix and prefix != "/":
                value = value.replace(f"{prefix}/", "./").replace(prefix, ".")
        return redact_secrets(value)

    out = dict(data)
    if "command" in out:
        out["command"] = _redact(out["command"])
    if "error_message" in out:
        out["error_message"] = _redact(out["error_message"])
    if not include_output:
        out.pop("stdout", None)
        out.pop("stderr", None)
    else:
        if "stdout" in out:
            out["stdout"] = _redact(out["stdout"])
        if "stderr" in out:
            out["stderr"] = _redact(out["stderr"])
    return out
