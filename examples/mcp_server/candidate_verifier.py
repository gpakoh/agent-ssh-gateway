"""Ephemeral verifier execution for trusted task-candidate delivery.

Required checks execute candidate-controlled code, so process isolation must
not be shared across verifications.  Every verification therefore runs in its
own disposable Docker container.  Candidate storage is mounted read-only; the
container receives no Docker socket, agent runtime, authoritative workspace,
OAuth data, registry data, or project-internal Docker networks.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tool_results import _redact_error_message

from examples.mcp_server.git_trust import with_scoped_safe_directories
from examples.mcp_server.registered_source_clone import (
    RegisteredSourceCloneError,
    clone_registered_commit_via_bundle,
)


class CandidateVerificationError(RuntimeError):
    """An isolated candidate verification failure safe to expose as denial.

    The failure carries a stable machine ``code``, a ``phase`` label, a
    ``retryable`` flag and sanitized, bounded ``details`` so a calling tool
    can map it into a structured MCP error without casting to a generic
    ``CHECK_FAILED`` and without surfacing raw verifier output.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "CANDIDATE_VERIFICATION_FAILED",
        phase: str = "push_preflight",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.retryable = bool(retryable)
        self.details = dict(details) if details else {}


_VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CONTAINER_NAME_PREFIX = "mcp-candidate-verifier"
_CONTAINER_SOURCE = "/candidate-src"

# Cap on the failed-check output tail a verifier returns.  The verifier only
# forwards a fixed number of bytes (see the script); this slices it further
# and redacts it before anything reaches a tool response.
_OUTPUT_TAIL_BYTES = 7000
_DETAIL_OUTPUT_CHARS = 4000
_MCP_VERIFY_EXIT_RE = re.compile(r"^MCP_VERIFY_EXIT=(\d+)$", re.MULTILINE)
_MCP_VERIFY_CHECK_RE = re.compile(r"^MCP_VERIFY_CHECK=(\d+)$", re.MULTILINE)

# Candidate-controlled output may try to echo credentials; scrub common
# credential shapes in addition to the shared path/endpoint redaction.
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)\b(?:authorization\s*[:=]\s*basic\s+"
    r"|(?:[a-z0-9_]*)(?:token|secret|password|passwd|api[_-]?key)\b\s*(?::=|=|:)\s*)"
    r"[^\s'\"{}]+"
)

# Verifier-owned machine receipt framing.  Check stdout/stderr never reaches
# the parser as raw lines: they are stored in files and only these encoded
# frames are emitted by the verifier after each check returns.
_RECEIPT_PREFIX = "MCP_CHECK_RECEIPT_V1:"
_RECEIPT_VERSION = 1
_RECEIPT_TAIL_CHARS = 1500
_MAX_FRAME_PAYLOAD_CHARS = 12_000
_RECEIPT_TOTAL_BASE_CHARS = 32_000
_RECEIPT_TOTAL_PER_CHECK_CHARS = 8_000
_MAX_DURATION_MS = 604_800_000
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_RECEIPT_CWD = "."
_VERSION_PROBE_TOOLS = frozenset(
    {"python", "python3", "uv", "pytest", "ruff", "mypy", "git"}
)

# In-container evidence builder embedded into the verifier script.  It runs
# only under the verifier's control after a check returns; it never executes
# candidate command semantics for provenance (static builtin classification
# plus trusted-PATH resolution only).
_FRAME_BUILDER_SOURCE = r'''
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

PREFIX = "MCP_CHECK_RECEIPT_V1:"
TAIL_CHARS = 1500
VERSION_TOOLS = frozenset({"python", "python3", "uv", "pytest", "ruff", "mypy", "git"})
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
BUILTINS = frozenset({
    ".", ":", "[", "alias", "bg", "break", "builtin", "cd", "command",
    "continue", "echo", "eval", "exec", "exit", "export", "false", "fg",
    "getopts", "hash", "help", "history", "jobs", "kill", "let", "local",
    "logout", "printf", "pwd", "read", "readonly", "return", "set", "shift",
    "source", "test", "times", "trap", "true", "type", "ulimit", "umask",
    "unalias", "unset", "wait",
})
RESERVED = frozenset({
    "if", "then", "else", "elif", "fi", "case", "esac", "for", "while",
    "until", "do", "done", "in", "function", "select", "time", "{", "}",
    "!", "[[", "]]",
})


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def status_info(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        data = b""
    fields = data.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    entries = 0
    index = 0
    while index < len(fields):
        record = fields[index]
        status = record[:2].decode("ascii", "replace") if len(record) >= 2 else ""
        entries += 1
        index += 1
        if ("R" in status or "C" in status) and index < len(fields):
            index += 1
    return hashlib.sha256(data).hexdigest(), len(data), entries


def tail_text(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", "replace")
    if len(text) > TAIL_CHARS:
        text = text[-TAIL_CHARS:]
    return text


def probe_version(path, trusted_path):
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": trusted_path, "HOME": os.environ.get("HOME", "/tmp")},
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    text = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    if not text:
        return None
    return text.splitlines()[0].strip()[:200]


def resolve_primary(command, trusted_path, repo):
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return {"kind": "unresolved", "reason": "unparseable_command"}
    index = 0
    while index < len(tokens) and ASSIGN_RE.match(tokens[index]):
        index += 1
    if index >= len(tokens) or not tokens[index]:
        return {"kind": "unresolved", "reason": "empty_command"}
    name = tokens[index]
    if any(ch in name for ch in "\0\n\r"):
        return {"kind": "unresolved", "reason": "unsafe_command_name"}
    if name in BUILTINS:
        shell_path = shutil.which("sh", path=trusted_path) or shutil.which("sh") or "/bin/sh"
        shell_sha = ""
        if os.path.isfile(shell_path):
            shell_sha = sha256_file(shell_path)
        return {
            "kind": "builtin",
            "name": name,
            "class": "builtin",
            "shell_path": shell_path,
            "shell_sha256": shell_sha,
            "shell_identity": "verifier-sh",
        }
    if name in RESERVED:
        shell_path = shutil.which("sh", path=trusted_path) or shutil.which("sh") or "/bin/sh"
        shell_sha = ""
        if os.path.isfile(shell_path):
            shell_sha = sha256_file(shell_path)
        return {
            "kind": "builtin",
            "name": name,
            "class": "reserved_word",
            "shell_path": shell_path,
            "shell_sha256": shell_sha,
            "shell_identity": "verifier-sh",
        }
    resolved = shutil.which(name, path=trusted_path)
    if resolved is None and "/" in name:
        candidate = name if os.path.isabs(name) else os.path.join(repo, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            resolved = os.path.abspath(candidate)
    if resolved is None:
        return {"kind": "unresolved", "reason": "not_found_on_trusted_path"}
    basename = os.path.basename(resolved)
    info = {
        "kind": "external",
        "name": name,
        "basename": basename,
        "path": resolved,
        "sha256": sha256_file(resolved),
    }
    if basename in VERSION_TOOLS:
        version = probe_version(resolved, trusted_path)
        if version is not None:
            info["version"] = version
    return info


def main(argv):
    if len(argv) != 13:
        return 2
    (run_id, image, index_s, command, rc_s, before_p, after_p,
     t0_p, t1_p, out_p, err_p, repo) = argv[1:]
    try:
        index = int(index_s)
        rc = int(rc_s)
        with open(t0_p, "r", encoding="ascii") as handle:
            t0 = int(handle.read().strip())
        with open(t1_p, "r", encoding="ascii") as handle:
            t1 = int(handle.read().strip())
    except (OSError, ValueError):
        return 2
    duration = max(0, t1 - t0)
    before_sha, before_bytes, before_entries = status_info(before_p)
    after_sha, after_bytes, after_entries = status_info(after_p)
    trusted_path = os.environ.get("MCP_TRUSTED_PATH") or "/usr/local/bin:/usr/bin:/bin"
    payload = {
        "v": 1,
        "run_id": run_id,
        "verifier_image": image,
        "check_index": index,
        "command": command,
        "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "cwd": ".",
        "duration_ms": duration,
        "exit_code": rc,
        "stdout_tail": tail_text(out_p),
        "stderr_tail": tail_text(err_p),
        "primary_tool": resolve_primary(command, trusted_path, repo),
        "before_status_sha256": before_sha,
        "before_status_bytes": before_bytes,
        "before_status_entries": before_entries,
        "after_status_sha256": after_sha,
        "after_status_bytes": after_bytes,
        "after_status_entries": after_entries,
        "mutation_changed": before_sha != after_sha,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")
    sys.stdout.write(PREFIX + encoded + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
'''


def _q(value: str) -> str:
    return shlex.quote(value)


def _new_verifier_container_name() -> str:
    """Return a process-independent identity for one verifier execution."""
    return f"{_CONTAINER_NAME_PREFIX}-{secrets.token_hex(12)}"


def sanitize_verifier_output_tail(text: str) -> str:
    """Return a bounded, redacted tail of candidate-verifier output.

    The output is candidate-controlled, so it is never echoed verbatim.  It is
    capped in bytes at the source and again in characters here, and passed
    through the shared gateway redaction policy (internal paths, API endpoints)
    plus credential-shape scrubbing before it can be surfaced.
    """
    raw = str(text or "")
    if not raw:
        return ""
    redacted, _was_redacted = _redact_error_message(raw)
    redacted = _CREDENTIAL_VALUE_RE.sub("[REDACTED]", redacted)
    lines = [line for line in redacted.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines)[-_DETAIL_OUTPUT_CHARS:]


def _encode_receipt_frame(payload: dict[str, Any]) -> str:
    """Encode one verifier-owned receipt frame as ``PREFIX + urlsafe-b64(JSON)``.

    The payload is the machine readable per-check receipt.  It is opaque to
    candidate output because check stdout/stderr is collected into files and
    only the verifier emits frames after the check returns; the host parser
    additionally binds every frame to the per-run ``run_id``.
    """
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{_RECEIPT_PREFIX}{encoded}"


def _decode_receipt_frame(line: str) -> dict[str, Any]:
    payload_b64 = line[len(_RECEIPT_PREFIX) :]
    if not payload_b64 or len(payload_b64) > _MAX_FRAME_PAYLOAD_CHARS:
        raise _receipt_failure("verifier receipt frame is malformed")
    padding = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise _receipt_failure("verifier receipt frame is malformed") from exc
    if not isinstance(payload, dict):
        raise _receipt_failure("verifier receipt frame is malformed")
    return payload


def _receipt_failure(
    message: str,
    *,
    code: str = "VERIFIER_RECEIPT_INVALID",
) -> CandidateVerificationError:
    """Fail closed on untrustworthy receipt evidence."""
    return CandidateVerificationError(
        message,
        code=code,
        phase="receipt_validation",
        retryable=False,
        details={"phase": "receipt_validation", "mutation_occurred": False},
    )


def _max_total_receipt_chars(check_count: int) -> int:
    return _RECEIPT_TOTAL_BASE_CHARS + _RECEIPT_TOTAL_PER_CHECK_CHARS * max(
        1, int(check_count)
    )


def _bounded_tail(value: str) -> str:
    text = str(value or "")
    if len(text) > _RECEIPT_TAIL_CHARS:
        text = text[-_RECEIPT_TAIL_CHARS:]
    return sanitize_verifier_output_tail(text)


def _validated_provenance(primary_tool: Any) -> None:
    if not isinstance(primary_tool, dict):
        raise _receipt_failure("verifier receipt provenance is malformed")
    kind = primary_tool.get("kind")
    if kind == "builtin":
        if not isinstance(primary_tool.get("name"), str) or not primary_tool["name"]:
            raise _receipt_failure("verifier receipt builtin provenance is malformed")
        shell_sha = primary_tool.get("shell_sha256")
        if isinstance(shell_sha, str) and shell_sha and not _SHA256_HEX_RE.fullmatch(shell_sha):
            raise _receipt_failure("verifier receipt builtin provenance is malformed")
        if not isinstance(primary_tool.get("shell_path"), str) or not primary_tool["shell_path"]:
            raise _receipt_failure("verifier receipt builtin provenance is malformed")
        if not isinstance(primary_tool.get("shell_identity"), str):
            raise _receipt_failure("verifier receipt builtin provenance is malformed")
        return
    if kind == "external":
        for key in ("name", "basename", "path", "sha256"):
            if not isinstance(primary_tool.get(key), str) or not primary_tool[key]:
                raise _receipt_failure("verifier receipt external provenance is malformed")
        if not _SHA256_HEX_RE.fullmatch(primary_tool["sha256"]):
            raise _receipt_failure("verifier receipt external provenance is malformed")
        version = primary_tool.get("version")
        if version is not None and not isinstance(version, str):
            raise _receipt_failure("verifier receipt external provenance is malformed")
        return
    raise CandidateVerificationError(
        "unresolved or unsafe primary tool provenance in verifier receipt",
        code="VERIFIER_PROVENANCE_UNRESOLVED",
        phase="receipt_validation",
        retryable=False,
        details={"phase": "receipt_validation", "mutation_occurred": False},
    )


def _validated_receipt_check(
    payload: dict[str, Any],
    *,
    run_id: str,
    verifier_image: str,
    expected_index: int,
    expected_command: str,
) -> dict[str, Any]:
    if payload.get("v") != _RECEIPT_VERSION:
        raise _receipt_failure("verifier receipt frame has an unknown version")
    if payload.get("run_id") != run_id:
        raise _receipt_failure("verifier receipt frame is not bound to this run")
    if payload.get("verifier_image") != verifier_image:
        raise _receipt_failure("verifier receipt frame has a mismatched verifier image")
    if payload.get("cwd") != _ALLOWED_RECEIPT_CWD:
        raise _receipt_failure("verifier receipt frame has an unexpected cwd")
    if payload.get("command") != expected_command:
        raise _receipt_failure("verifier receipt frame has a mismatched command")
    command_sha = payload.get("command_sha256")
    if (
        not isinstance(command_sha, str)
        or not _SHA256_HEX_RE.fullmatch(command_sha)
        or command_sha != hashlib.sha256(expected_command.encode("utf-8")).hexdigest()
    ):
        raise _receipt_failure("verifier receipt frame has a mismatched command hash")
    check_index = payload.get("check_index")
    if not isinstance(check_index, int) or isinstance(check_index, bool):
        raise _receipt_failure("verifier receipt frame has an invalid check index")
    if check_index != expected_index:
        raise _receipt_failure("verifier receipt frames are missing, duplicated, or out of order")
    duration = payload.get("duration_ms")
    if (
        not isinstance(duration, int)
        or isinstance(duration, bool)
        or duration < 0
        or duration > _MAX_DURATION_MS
    ):
        raise _receipt_failure("verifier receipt frame has an invalid duration")
    exit_code = payload.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise _receipt_failure("verifier receipt frame has an invalid exit code")
    for key in ("before_status_sha256", "after_status_sha256"):
        value = payload.get(key)
        if not isinstance(value, str) or not _SHA256_HEX_RE.fullmatch(value):
            raise _receipt_failure(f"verifier receipt frame has an invalid {key}")
    for key in (
        "before_status_bytes",
        "before_status_entries",
        "after_status_bytes",
        "after_status_entries",
    ):
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise _receipt_failure(f"verifier receipt frame has an invalid {key}")
    mutation_changed = payload.get("mutation_changed")
    if not isinstance(mutation_changed, bool):
        raise _receipt_failure("verifier receipt frame has an invalid mutation marker")
    if (
        mutation_changed
        != (payload["before_status_sha256"] != payload["after_status_sha256"])
    ):
        raise _receipt_failure("verifier receipt frame has inconsistent mutation evidence")
    _validated_provenance(payload.get("primary_tool"))
    return {
        "check_index": check_index,
        "command": payload["command"],
        "command_sha256": command_sha,
        "cwd": _ALLOWED_RECEIPT_CWD,
        "duration_ms": duration,
        "exit_code": exit_code,
        "stdout_tail": _bounded_tail(str(payload.get("stdout_tail") or "")),
        "stderr_tail": _bounded_tail(str(payload.get("stderr_tail") or "")),
        "verifier_image": verifier_image,
        "primary_tool": payload["primary_tool"],
        "before_status_sha256": payload["before_status_sha256"],
        "before_status_bytes": payload["before_status_bytes"],
        "before_status_entries": payload["before_status_entries"],
        "after_status_sha256": payload["after_status_sha256"],
        "after_status_bytes": payload["after_status_bytes"],
        "after_status_entries": payload["after_status_entries"],
        "mutation_changed": mutation_changed,
    }


def _parse_check_receipts(
    stdout: str,
    *,
    run_id: str,
    verifier_image: str,
    required_checks: list[str],
) -> list[dict[str, Any]]:
    """Parse and strictly validate the verifier's per-check receipt frames.

    Any missing, duplicated, malformed, out-of-order, or spoofed frame fails
    closed rather than yielding partial trust.
    """
    text = str(stdout or "")
    total = 0
    payloads: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith(_RECEIPT_PREFIX):
            continue
        total += len(line)
        if total > _max_total_receipt_chars(len(required_checks)):
            raise _receipt_failure("verifier receipt evidence exceeds its bound")
        payloads.append(_decode_receipt_frame(line))
    if len(payloads) != len(required_checks):
        raise _receipt_failure("verifier receipt frame count does not match required checks")
    return [
        _validated_receipt_check(
            payload,
            run_id=run_id,
            verifier_image=verifier_image,
            expected_index=index,
            expected_command=command,
        )
        for index, (payload, command) in enumerate(zip(payloads, required_checks, strict=True))
    ]


def _try_validated_evidence(
    stdout: str,
    *,
    run_id: str,
    verifier_image: str,
    required_checks: list[str],
) -> list[dict[str, Any]] | None:
    """Best-effort validated evidence for failure details; None when untrustworthy."""
    try:
        return _parse_check_receipts(
            stdout,
            run_id=run_id,
            verifier_image=verifier_image,
            required_checks=required_checks,
        )
    except CandidateVerificationError:
        return None


def _structured_verifier_failure(
    exit_code: int,
    checks: list[str],
    stdout: str,
    stderr: str,
    *,
    run_id: str | None = None,
    verifier_image: str | None = None,
) -> CandidateVerificationError:
    """Build a structured CandidateVerificationError from a nonzero verifier exit.

    Only the failed-check command (operator-supplied via ``required_checks``),
    the phase and a bounded, redacted output tail are surfaced -- never the raw
    remote or any unredacted verifier output.
    """
    details: dict[str, Any] = {
        "phase": "required_checks",
        "exit_code": int(exit_code),
        "mutation_occurred": False,
    }
    check_match = _MCP_VERIFY_CHECK_RE.search(str(stdout or ""))
    if check_match:
        try:
            idx = int(check_match.group(1))
            if 0 <= idx < len(checks):
                details["check_index"] = idx
                details["failed_check"] = checks[idx]
        except (TypeError, ValueError):
            pass
    exit_match = _MCP_VERIFY_EXIT_RE.search(str(stdout or ""))
    if exit_match:
        try:
            details["check_exit_code"] = int(exit_match.group(1))
        except (TypeError, ValueError):
            pass
    tail = sanitize_verifier_output_tail(f"{stdout or ''}\n{stderr or ''}")

    evidence: list[dict[str, Any]] = []
    if run_id is not None and verifier_image is not None:
        parsed = _try_validated_evidence(
            stdout,
            run_id=run_id,
            verifier_image=verifier_image,
            required_checks=checks,
        )
        if parsed is not None:
            evidence = parsed
            legacy_index = details.get("check_index")
            failed_receipt: dict[str, Any] | None = None
            if isinstance(legacy_index, int) and 0 <= legacy_index < len(parsed):
                failed_receipt = parsed[legacy_index]
            else:
                nonzero = [item for item in parsed if item["exit_code"] != 0]
                failed_receipt = nonzero[-1] if nonzero else None
            if failed_receipt is not None:
                details["check_index"] = failed_receipt["check_index"]
                if 0 <= failed_receipt["check_index"] < len(checks):
                    details["failed_check"] = checks[failed_receipt["check_index"]]
                details["check_exit_code"] = failed_receipt["exit_code"]
                details["failed_receipt"] = failed_receipt
                frame_tail = sanitize_verifier_output_tail(
                    f"{failed_receipt['stdout_tail']}\n{failed_receipt['stderr_tail']}"
                )
                if frame_tail:
                    tail = frame_tail
    if evidence:
        details["check_evidence"] = evidence
        if "failed_receipt" not in details and isinstance(
            details.get("check_index"), int
        ):
            index = int(details["check_index"])
            if 0 <= index < len(evidence):
                details["failed_receipt"] = evidence[index]
    if tail:
        details["output_tail"] = tail

    if exit_code == 86:
        details["phase"] = "receipt_validation"
        return CandidateVerificationError(
            "candidate verifier could not collect check evidence",
            code="VERIFIER_EVIDENCE_UNAVAILABLE",
            phase="receipt_validation",
            retryable=True,
            details=details,
        )

    if exit_code == 83:
        details["phase"] = "candidate_check"
        return CandidateVerificationError(
            f"a required verification check failed with exit code {exit_code}",
            code="CANDIDATE_CHECK_FAILED",
            phase="candidate_check",
            retryable=False,
            details=details,
        )
    if exit_code == 81:
        details["phase"] = "candidate_checkout"
        return CandidateVerificationError(
            "candidate could not be checked out at the expected head "
            f"(exit code {exit_code})",
            code="CANDIDATE_SOURCE_UNAVAILABLE",
            phase="candidate_checkout",
            retryable=False,
            details=details,
        )
    if exit_code == 82:
        details["phase"] = "verifier_env"
        return CandidateVerificationError(
            f"candidate dependency bootstrap failed before required checks "
            f"(exit code {exit_code})",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
            retryable=True,
            details=details,
        )
    if exit_code == 80:
        details["phase"] = "verifier_bootstrap"
        return CandidateVerificationError(
            f"candidate verifier could not materialize its disposable environment "
            f"(exit code {exit_code})",
            code="VERIFIER_BOOTSTRAP_FAILED",
            phase="verifier_bootstrap",
            retryable=True,
            details=details,
        )
    details["phase"] = "verifier_bootstrap"
    return CandidateVerificationError(
        f"isolated candidate verification failed with exit code {exit_code}",
        code="VERIFIER_BOOTSTRAP_FAILED",
        phase="verifier_bootstrap",
        retryable=True,
        details=details,
    )


def build_candidate_verifier_script(
    *,
    staging_root: Path,
    expected_sha: str,
    required_checks: list[str],
    run_id: str | None = None,
    verifier_image: str = "",
) -> str:
    """Build a fail-closed verifier script that never writes candidate storage.

    The script emits one verifier-owned receipt frame per check, plus the legacy
    ``MCP_VERIFY_EXIT``/``MCP_VERIFY_CHECK`` markers on failure.  ``run_id``
    binds every frame to this specific execution so a check that somehow
    reaches the parser's stdout still cannot forge a valid receipt.
    """
    # The host-side staging path is deliberately not embedded in the script.
    # Docker mounts only that exact volume subpath at this fixed location, so
    # candidate-controlled checks cannot traverse sibling task receipts or
    # staging repositories in the candidate store.
    receipt_run_id = run_id or secrets.token_hex(16)
    source = _CONTAINER_SOURCE
    lines = [
        "set -u",
        f"SOURCE={_q(source)}",
        f"EXPECTED={_q(expected_sha)}",
        f"RUN_ID={_q(receipt_run_id)}",
        f"IMAGE={_q(verifier_image)}",
        'MCP_TRUSTED_PATH=${PATH:-/usr/local/bin:/usr/bin:/bin}',
        'export MCP_TRUSTED_PATH',
        'VERIFY_ROOT=$(mktemp -d /tmp/mcp-candidate-verify.XXXXXX) || exit 80',
        'cleanup() { chmod -R u+w "$VERIFY_ROOT" 2>/dev/null || true; rm -rf "$VERIFY_ROOT"; }',
        "trap cleanup EXIT",
        "trap 'exit 85' HUP INT TERM",
        'export HOME="$VERIFY_ROOT/home"',
        'export XDG_CACHE_HOME="$VERIFY_ROOT/cache"',
        'export XDG_DATA_HOME="$VERIFY_ROOT/data"',
        'mkdir -p "$HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" || exit 80',
        "cat >\"$VERIFY_ROOT/frame_builder.py\" <<'MCP_FRAME_BUILDER_EOF'",
        *_FRAME_BUILDER_SOURCE.splitlines(),
        "MCP_FRAME_BUILDER_EOF",
        'git clone --no-hardlinks --no-checkout "$SOURCE" "$VERIFY_ROOT/repo" >/dev/null 2>&1 || exit 81',
        'git -C "$VERIFY_ROOT/repo" checkout --detach --quiet "$EXPECTED" >/dev/null 2>&1 || exit 81',
        'ACTUAL=$(git -C "$VERIFY_ROOT/repo" rev-parse HEAD 2>/dev/null || true)',
        '[ "$ACTUAL" = "$EXPECTED" ] || exit 81',
        f"CHECKS_PRESENT={'1' if required_checks else '0'}",
        'command -v python3 >/dev/null 2>&1 || exit 82',
        'if [ "$CHECKS_PRESENT" = "1" ] && [ -f "$VERIFY_ROOT/repo/uv.lock" ] && [ -f "$VERIFY_ROOT/repo/pyproject.toml" ]; then',
        "  DEV_EXTRA=$(cd \"$VERIFY_ROOT/repo\" && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV python3 -c 'import tomllib; data=tomllib.load(open(\"pyproject.toml\", \"rb\")); print(\"1\" if \"dev\" in data.get(\"project\", {}).get(\"optional-dependencies\", {}) else \"0\")' 2>/dev/null) || exit 82",
        '  if [ "$DEV_EXTRA" = "1" ]; then',
        '    (cd "$VERIFY_ROOT/repo" && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV uv sync --frozen --extra dev) >/dev/null 2>&1 || exit 82',
        "  fi",
        "fi",
    ]
    for index, check in enumerate(required_checks):
        lines.extend(
            [
                f"CHECK={_q(check)}",
                'git -C "$VERIFY_ROOT/repo" status --porcelain=v1 -z --untracked-files=all >"$VERIFY_ROOT/before.status" 2>/dev/null || exit 86',
                "python3 -c 'import time; print(time.monotonic_ns() // 1000000)' >\"$VERIFY_ROOT/t0\" || exit 86",
                '(cd "$VERIFY_ROOT/repo" && env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV sh -c "$CHECK") '
                '>"$VERIFY_ROOT/check.out" 2>"$VERIFY_ROOT/check.err"; rc=$?',
                "python3 -c 'import time; print(time.monotonic_ns() // 1000000)' >\"$VERIFY_ROOT/t1\" || exit 86",
                'git -C "$VERIFY_ROOT/repo" status --porcelain=v1 -z --untracked-files=all >"$VERIFY_ROOT/after.status" 2>/dev/null || exit 86',
                "python3 \"$VERIFY_ROOT/frame_builder.py\" \"$RUN_ID\" \"$IMAGE\" "
                f"{index} \"$CHECK\" \"$rc\" "
                '"$VERIFY_ROOT/before.status" "$VERIFY_ROOT/after.status" '
                '"$VERIFY_ROOT/t0" "$VERIFY_ROOT/t1" '
                '"$VERIFY_ROOT/check.out" "$VERIFY_ROOT/check.err" '
                '"$VERIFY_ROOT/repo" >"$VERIFY_ROOT/frame.out" 2>"$VERIFY_ROOT/frame.err" || exit 86',
                'cat "$VERIFY_ROOT/frame.out"',
                f'[ "$rc" -eq 0 ] || {{ printf "MCP_VERIFY_EXIT=%s\\nMCP_VERIFY_CHECK=%s\\n" "$rc" "{index}"; exit 83; }}',
            ]
        )
    lines.append("exit 0")
    return "\n".join(lines)


def _cfg_failure(
    message: str,
    *,
    code: str = "CANDIDATE_VERIFICATION_FAILED",
    phase: str = "push_preflight",
    retryable: bool = False,
) -> CandidateVerificationError:
    """A verifier preflight (config/boundary) denial, not retryable as-is."""
    return CandidateVerificationError(
        message,
        code=code,
        phase=phase,
        retryable=retryable,
        details={"phase": phase, "mutation_occurred": False},
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise _cfg_failure(
            f"{name} is required for isolated verification",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
            retryable=False,
        )
    return value


def _verification_timeout() -> int:
    raw = os.environ.get("MCP_CANDIDATE_VERIFY_TIMEOUT_SECONDS", "1800").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise _cfg_failure(
            "invalid candidate verifier timeout",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
        ) from exc
    if value < 1 or value > 7200:
        raise _cfg_failure(
            "candidate verifier timeout is out of bounds",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
        )
    return value


def _validated_volume_name() -> str:
    value = _required_env("MCP_TASK_CANDIDATE_VOLUME_NAME")
    if not _VOLUME_RE.fullmatch(value):
        raise _cfg_failure(
            "invalid candidate verifier volume name",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
        )
    return value


def _validated_image() -> str:
    value = _required_env("MCP_VERIFIER_IMAGE")
    if (
        len(value) > 512
        or value.startswith("-")
        or any(ch.isspace() or ord(ch) < 32 for ch in value)
    ):
        raise _cfg_failure(
            "invalid candidate verifier image reference",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
        )
    return value


def _candidate_store_root() -> Path:
    candidate_root_raw = _required_env("MCP_TASK_CANDIDATE_ROOT")
    candidate_root = Path(candidate_root_raw)
    if not candidate_root.is_absolute() or candidate_root == Path("/"):
        raise _cfg_failure(
            "invalid candidate verifier root",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
        )
    return candidate_root.resolve()


def _validated_staging_root(staging_root: Path) -> Path:
    candidate_root = _candidate_store_root()
    staging = staging_root.resolve()
    try:
        staging.relative_to(candidate_root)
    except ValueError as exc:
        raise _cfg_failure(
            "candidate verifier source escapes candidate root",
            code="CANDIDATE_VOLUME_SUBPATH_INVALID",
            phase="source_resolution",
        ) from exc
    if not staging.is_dir():
        raise _cfg_failure(
            "candidate verifier source is unavailable",
            code="CANDIDATE_SOURCE_UNAVAILABLE",
            phase="source_resolution",
        )
    return staging


def _validated_volume_subpath(staging_root: Path) -> str:
    candidate_root = _candidate_store_root()
    staging = _validated_staging_root(staging_root)
    relative = staging.relative_to(candidate_root)
    if relative == Path(".") or not relative.parts:
        raise _cfg_failure(
            "candidate verifier must mount a task-scoped subpath",
            code="CANDIDATE_VOLUME_SUBPATH_INVALID",
            phase="source_resolution",
        )
    for part in relative.parts:
        if (
            part in {"", ".", ".."}
            or "," in part
            or any(ord(ch) < 32 for ch in part)
        ):
            raise _cfg_failure(
                "invalid candidate verifier volume subpath",
                code="CANDIDATE_VOLUME_SUBPATH_INVALID",
                phase="source_resolution",
            )
    return relative.as_posix()


def build_ephemeral_verifier_argv(
    *,
    staging_root: Path,
    volume_name: str,
    image: str,
    timeout_seconds: int,
    container_name: str | None = None,
) -> list[str]:
    """Build the fixed-security docker argv for one disposable verifier."""
    volume_subpath = _validated_volume_subpath(staging_root)
    execution_container_name = container_name or _new_verifier_container_name()
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        execution_container_name,
        "--init",
        "--network",
        "bridge",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--memory",
        "16g",
        "--cpus",
        "2.0",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,exec,size=8g",
        "--mount",
        f"type=volume,src={volume_name},dst={_CONTAINER_SOURCE},volume-subpath={volume_subpath},readonly",
        "--user",
        "mcpuser",
        "--entrypoint",
        "/usr/bin/timeout",
        image,
        "-s",
        "KILL",
        str(timeout_seconds),
        "/bin/sh",
        "-s",
    ]


def _run(
    runner: Callable[..., Any],
    argv: list[str],
    *,
    input_text: str | None = None,
    timeout: int,
) -> Any:
    return runner(
        argv,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _force_remove(runner: Callable[..., Any], container_name: str) -> None:
    try:
        _run(runner, ["docker", "rm", "-f", container_name], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


def verify_candidate_via_docker(
    *,
    staging_root: Path,
    expected_sha: str,
    required_checks: list[str],
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Verify one candidate in a fresh container; return the check receipt.

    Returns a structured, validated receipt:
    ``{"expected_sha", "verifier_image", "check_count", "checks"}`` where each
    ``checks`` entry is a redacted, bounded per-check receipt (command identity,
    duration, exit code, primary-tool provenance, and before/after git status
    digests).  Any untrustworthy or malformed frame fails closed.  Existing
    callers that ignore the return value remain compatible.
    """
    staging = _validated_staging_root(staging_root)
    volume_name = _validated_volume_name()
    image = _validated_image()
    timeout = _verification_timeout()
    container_name = _new_verifier_container_name()
    run_id = secrets.token_hex(16)
    script = build_candidate_verifier_script(
        staging_root=staging,
        expected_sha=expected_sha,
        required_checks=required_checks,
        run_id=run_id,
        verifier_image=image,
    )
    argv = build_ephemeral_verifier_argv(
        staging_root=staging,
        volume_name=volume_name,
        image=image,
        timeout_seconds=timeout,
        container_name=container_name,
    )

    try:
        result = _run(runner, argv, input_text=script, timeout=timeout + 60)
    except subprocess.TimeoutExpired as exc:
        raise CandidateVerificationError(
            "isolated candidate verification timed out",
            code="VERIFIER_BOOTSTRAP_FAILED",
            phase="verifier_bootstrap",
            retryable=True,
            details={
                "phase": "verifier_bootstrap",
                "mutation_occurred": False,
                "exit_code": 124,
            },
        ) from exc
    except OSError as exc:
        raise CandidateVerificationError(
            "isolated candidate verifier is unavailable",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
            retryable=True,
            details={
                "phase": "verifier_env",
                "mutation_occurred": False,
            },
        ) from exc
    finally:
        # `--rm` handles the normal path; force removal covers timeout, client
        # transport failure, or a verifier whose descendants kept PID 1 alive.
        # The execution-scoped name guarantees cleanup cannot target another
        # concurrent verifier. If the control-plane process itself dies, the
        # in-container hard timeout bounds the orphan and `--rm` removes it.
        _force_remove(runner, container_name)

    returncode = getattr(result, "returncode", None)
    exit_code = int(returncode) if returncode is not None else 1
    if exit_code != 0:
        raise _structured_verifier_failure(
            exit_code,
            checks=required_checks,
            stdout=getattr(result, "stdout", "") or "",
            stderr=getattr(result, "stderr", "") or "",
            run_id=run_id,
            verifier_image=image,
        )

    checks = _parse_check_receipts(
        getattr(result, "stdout", "") or "",
        run_id=run_id,
        verifier_image=image,
        required_checks=required_checks,
    )
    return {
        "expected_sha": expected_sha,
        "verifier_image": image,
        "check_count": len(checks),
        "checks": checks,
    }


def _is_under_candidate_store(path: Path) -> bool:
    candidate_root = _candidate_store_root()
    try:
        path.resolve().relative_to(candidate_root)
    except ValueError:
        return False
    return True


def _git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_materialize_git(
    argv: list[str],
    *,
    cwd: Path,
    home: Path,
    timeout: int = 120,
    safe_directories: tuple[Path, ...] = (),
) -> str:
    try:
        env = _git_env(home)
        if safe_directories:
            env = with_scoped_safe_directories(safe_directories, base_env=env)
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
            env=env,
        )
    except ValueError as exc:
        raise CandidateVerificationError(
            "candidate verifier Git trust configuration is invalid",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
            retryable=False,
            details={"phase": "verifier_env", "mutation_occurred": False},
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateVerificationError(
            "candidate verifier could not materialize registered workspace source",
            code="CANDIDATE_SOURCE_UNAVAILABLE",
            phase="source_resolution",
            retryable=True,
            details={"phase": "source_resolution", "mutation_occurred": False},
        ) from exc
    if result.returncode != 0:
        tail = sanitize_verifier_output_tail(f"{result.stdout or ''}\n{result.stderr or ''}")
        details: dict[str, Any] = {
            "phase": "source_resolution",
            "mutation_occurred": False,
            "exit_code": result.returncode,
        }
        if tail:
            details["output_tail"] = tail
        raise CandidateVerificationError(
            "candidate verifier could not materialize registered workspace source",
            code="CANDIDATE_SOURCE_UNAVAILABLE",
            phase="source_resolution",
            retryable=True,
            details=details,
        )
    return result.stdout.strip()


def _materialize_workspace_source(workspace_root: Path, expected_sha: str) -> Path:
    """Copy one verified delivery workspace into the candidate volume for Docker.

    Docker verification deliberately mounts only subpaths of
    ``MCP_TASK_CANDIDATE_ROOT``. Prepared delivery workspaces can live in the
    workspace registry instead, so materialize an exact, no-hardlinks Git clone
    under the candidate volume and verify that disposable source. The caller is
    responsible for removing the returned path.
    """
    workspace = workspace_root.resolve()
    if not workspace.is_dir() or not (workspace / ".git").exists():
        raise _cfg_failure(
            "registered delivery workspace must be a Git worktree",
            code="CANDIDATE_SOURCE_UNAVAILABLE",
            phase="source_resolution",
        )
    candidate_root = _candidate_store_root()
    materialized_root = candidate_root / "verified-workspaces"
    try:
        materialized_root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f"verified-workspace-{expected_sha[:12]}-",
                dir=str(materialized_root),
            )
        )
    except OSError as exc:
        raise _cfg_failure(
            "candidate verifier staging root is unavailable",
            code="VERIFIER_ENV_UNAVAILABLE",
            phase="verifier_env",
            retryable=True,
        ) from exc

    try:
        try:
            clone_registered_commit_via_bundle(
                source_root=workspace,
                expected_sha=expected_sha,
                destination=staging,
                base_env=_git_env(staging),
                timeout=120,
            )
        except RegisteredSourceCloneError as exc:
            raise CandidateVerificationError(
                "candidate verifier could not materialize registered workspace source",
                code="CANDIDATE_SOURCE_UNAVAILABLE",
                phase="source_resolution",
                retryable=exc.retryable,
                details={
                    "phase": "source_resolution",
                    "materialization_phase": exc.phase,
                    "exit_code": exc.exit_code,
                    "mutation_occurred": False,
                },
            ) from exc
        _run_materialize_git(
            ["git", "-C", str(staging), "checkout", "--detach", "--quiet", expected_sha],
            cwd=staging,
            home=staging,
        )
        actual = _run_materialize_git(
            ["git", "-C", str(staging), "rev-parse", "HEAD"],
            cwd=staging,
            home=staging,
        ).strip().lower()
        if actual != expected_sha:
            raise CandidateVerificationError(
                "candidate verifier materialized the wrong registered workspace commit",
                code="CANDIDATE_SOURCE_UNAVAILABLE",
                phase="source_resolution",
                retryable=False,
                details={"phase": "source_resolution", "mutation_occurred": False},
            )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def verify_workspace_via_docker(
    *,
    workspace_root: Path,
    expected_sha: str,
    required_checks: list[str],
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Verify a registered delivery workspace in the isolated Docker verifier.

    Forwards the structured candidate receipt unchanged.
    """
    workspace = workspace_root.resolve()
    if _is_under_candidate_store(workspace):
        return verify_candidate_via_docker(
            staging_root=workspace,
            expected_sha=expected_sha,
            required_checks=required_checks,
            runner=runner,
        )

    staging = _materialize_workspace_source(workspace, expected_sha)
    try:
        return verify_candidate_via_docker(
            staging_root=staging,
            expected_sha=expected_sha,
            required_checks=required_checks,
            runner=runner,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
