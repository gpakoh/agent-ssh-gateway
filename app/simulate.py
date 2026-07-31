"""Simulate Mode — replay commands through the policy engine for testing.

DCG-inspired: parses command logs in multiple formats, evaluates each
command against the policy engine, and returns structured results.

Input formats:
    plain       — each line is a shell command
    hook_json   — agent hook JSON payload (tool_input.command)
    decision_log — DCG decision log format (DCG_LOG_V1|ts|decision|base64)
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from enum import StrEnum


class InputFormat(StrEnum):
    PLAIN = "plain"
    HOOK_JSON = "hook_json"
    DECISION_LOG = "decision_log"


@dataclass
class SimLimits:
    max_lines: int | None = None
    max_bytes: int | None = None
    max_command_bytes: int | None = 65536


@dataclass
class SimStats:
    total_lines: int = 0
    total_bytes: int = 0
    commands_extracted: int = 0
    malformed: int = 0
    ignored: int = 0
    empty: int = 0
    stopped_at_limit: bool = False


@dataclass
class ParsedCommand:
    command: str
    format: InputFormat
    line_number: int


@dataclass
class SimulateResult:
    """Result of evaluating one command through the policy engine."""

    command: str
    allowed: bool
    reason: str
    mode: str
    profile: str
    requires_approval: bool = False
    suggestion: str | None = None
    findings: list[dict] = field(default_factory=list)


def _parse_line(line: str, limits: SimLimits) -> ParsedCommand | None:
    """Parse a single line; returns None for empty/ignored/malformed lines.

    Returns a ParsedCommand or None (line should be skipped).
    """
    trimmed = line.rstrip("\n\r")

    if not trimmed.strip():
        return None

    # Decision log format: DCG_LOG_V1|ts|decision|base64_command
    if trimmed.startswith("DCG_LOG_V"):
        parts = trimmed.split("|", 4)
        if len(parts) < 4:
            return None
        try:
            decoded = base64.b64decode(parts[3]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        if limits.max_command_bytes and len(decoded) > limits.max_command_bytes:
            return None
        return ParsedCommand(decoded, InputFormat.DECISION_LOG, 0)

    # Hook JSON format
    stripped = trimmed.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        cmd = _try_extract_hook_json(stripped)
        if cmd is not None:
            if limits.max_command_bytes and len(cmd) > limits.max_command_bytes:
                return None
            return ParsedCommand(cmd, InputFormat.HOOK_JSON, 0)
        # If it looks like JSON but isn't hook input, try as plain command

    # Plain command
    if limits.max_command_bytes and len(trimmed) > limits.max_command_bytes:
        return None
    return ParsedCommand(trimmed, InputFormat.PLAIN, 0)


def _try_extract_hook_json(line: str) -> str | None:
    """Try to extract a shell command from hook JSON.

    Returns the command string, or None if not valid hook JSON.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    # Check if this looks like hook input
    if not any(k in data for k in ("tool_name", "toolName", "tool_input", "toolInput")):
        return None

    # Extract command from tool_input or toolInput
    tool_input = data.get("tool_input") or data.get("toolInput")
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return cmd

    return None


def parse_commands(text: str, limits: SimLimits | None = None) -> tuple[list[ParsedCommand], SimStats]:
    """Parse a text blob into commands.

    Supports multiple input formats auto-detected per line.
    """
    limits = limits or SimLimits()
    stats = SimStats()
    commands: list[ParsedCommand] = []

    for line_num, line in enumerate(text.splitlines(keepends=True), 1):
        if limits.max_lines and stats.total_lines >= limits.max_lines:
            stats.stopped_at_limit = True
            break

        stats.total_lines += 1
        stats.total_bytes += len(line)

        if limits.max_bytes and stats.total_bytes > limits.max_bytes:
            stats.stopped_at_limit = True
            break

        parsed = _parse_line(line, limits)
        if parsed is None:
            if line.strip():
                stats.malformed += 1
            else:
                stats.empty += 1
            continue

        parsed.line_number = line_num
        commands.append(parsed)
        stats.commands_extracted += 1

    return commands, stats


def simulate(
    content: str,
    *,
    mode: str = "audit",
    profile: str = "default",
    agent: str | None = None,
    project: str | None = None,
    limits: SimLimits | None = None,
) -> dict:
    """Replay commands through the policy engine.

    Parses multi-format input, evaluates each command, and returns
    structured results with stats.

    Returns:
        dict with ``results`` (list of per-command decisions) and
        ``stats`` (parsing/evaluation summary).
    """
    from app.command_policy import evaluate_command_policy, scan_command

    commands, stats = parse_commands(content, limits)

    results: list[dict] = []
    for pc in commands:
        decision = evaluate_command_policy(
            pc.command,
            mode=mode,
            profile=profile,
            agent=agent,
            project=project,
        )

        scan = scan_command(pc.command)
        findings = [
            {
                "pattern": f.pattern_name,
                "severity": f.severity,
                "confidence": f.confidence,
                "suggestion": f.suggestion,
                "suggestions": f.suggestions,
            }
            for f in scan.findings
        ]

        results.append(
            {
                "command": pc.command,
                "format": pc.format,
                "line": pc.line_number,
                "allowed": decision.allowed,
                "reason": decision.reason,
                "mode": decision.mode,
                "profile": decision.profile,
                "requires_approval": decision.requires_approval,
                "suggestions": decision.suggestions,
                "findings": findings,
            }
        )

    return {
        "results": results,
        "stats": {
            "total_lines": stats.total_lines,
            "commands_extracted": stats.commands_extracted,
            "malformed": stats.malformed,
            "empty": stats.empty,
            "stopped_at_limit": stats.stopped_at_limit,
        },
    }
