"""Command policy engine — profile-based command authorization.

Modes:
    off     — policy disabled, all commands allowed
    audit   — policy logs decisions but does not block
    enforce — policy blocks commands not matching the selected profile

Profiles:
    default  — deny obviously dangerous root commands
    readonly — allow only read-only inspection commands
    ops      — allow read-only commands plus limited service/docker operations
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum


class CommandPolicyMode(str, Enum):
    OFF = "off"
    AUDIT = "audit"
    ENFORCE = "enforce"


class CommandPolicyProfile(str, Enum):
    DEFAULT = "default"
    READONLY = "readonly"
    OPS = "ops"


@dataclass(frozen=True)
class CommandPolicyDecision:
    allowed: bool
    reason: str
    profile: str
    mode: str
    command_root: str | None = None


READONLY_ROOT_COMMANDS: set[str] = {
    "cat",
    "cd",
    "df",
    "du",
    "env",
    "free",
    "grep",
    "head",
    "hostname",
    "id",
    "ip",
    "journalctl",
    "ls",
    "netstat",
    "pgrep",
    "ping",
    "ps",
    "pwd",
    "ss",
    "stat",
    "tail",
    "top",
    "uname",
    "uptime",
    "whoami",
}

OPS_ROOT_COMMANDS: set[str] = READONLY_ROOT_COMMANDS | {
    "docker",
    "docker-compose",
    "systemctl",
    "service",
    "supervisorctl",
}

DEFAULT_DENIED_ROOT_COMMANDS: set[str] = {
    "mkfs",
    "fdisk",
    "parted",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
}

DANGEROUS_TOKENS: tuple[str, ...] = (
    " rm -rf ",
    " mkfs ",
    " dd if=",
    " :(){",
    "chmod 777",
    "chown -R",
    "curl ",
    "wget ",
    " nc ",
    " netcat ",
    "bash -c",
    "sh -c",
)

OPS_ALLOWED_SYSTEMCTL_ACTIONS: set[str] = {
    "status",
    "restart",
    "reload",
    "try-restart",
    "is-active",
    "is-enabled",
}

OPS_ALLOWED_DOCKER_ACTIONS: set[str] = {
    "ps",
    "logs",
    "inspect",
    "restart",
    "compose",
}


def normalize_command(command: str) -> str:
    return f" {command.strip()} "


def get_command_root(command: str) -> str | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None

    if not parts:
        return None

    root = parts[0].strip()

    if root == "sudo" and len(parts) > 1:
        return parts[1].strip()

    return root


def get_command_parts(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def contains_dangerous_token(command: str) -> str | None:
    normalized = normalize_command(command).lower()

    for token in DANGEROUS_TOKENS:
        if token in normalized:
            return token.strip()

    return None


def evaluate_readonly(command: str, root: str | None) -> tuple[bool, str]:
    if root is None:
        return False, "Command cannot be parsed"

    if root not in READONLY_ROOT_COMMANDS:
        return False, f"Root command '{root}' is not allowed in readonly profile"

    dangerous = contains_dangerous_token(command)
    if dangerous:
        return False, f"Dangerous token detected: {dangerous}"

    return True, "Command allowed by readonly profile"


def evaluate_ops(command: str, root: str | None) -> tuple[bool, str]:
    if root is None:
        return False, "Command cannot be parsed"

    if root not in OPS_ROOT_COMMANDS:
        return False, f"Root command '{root}' is not allowed in ops profile"

    dangerous = contains_dangerous_token(command)
    if dangerous:
        return False, f"Dangerous token detected: {dangerous}"

    parts = get_command_parts(command)
    effective_parts = parts[1:] if parts and parts[0] == "sudo" else parts

    if not effective_parts:
        return False, "Empty command"

    if effective_parts[0] in {"systemctl", "service"}:
        if len(effective_parts) < 2:
            return False, "Missing service action"

        action = effective_parts[1]
        if action not in OPS_ALLOWED_SYSTEMCTL_ACTIONS:
            return False, f"systemctl action '{action}' is not allowed in ops profile"

    if effective_parts[0] in {"docker", "docker-compose"}:
        if len(effective_parts) < 2:
            return False, "Missing docker action"

        action = effective_parts[1]
        if action not in OPS_ALLOWED_DOCKER_ACTIONS:
            return False, f"docker action '{action}' is not allowed in ops profile"

    return True, "Command allowed by ops profile"


def evaluate_default(command: str, root: str | None) -> tuple[bool, str]:
    if root is None:
        return False, "Command cannot be parsed"

    if root in DEFAULT_DENIED_ROOT_COMMANDS:
        return False, f"Root command '{root}' is denied by default profile"

    dangerous = contains_dangerous_token(command)
    if dangerous:
        return False, f"Dangerous token detected: {dangerous}"

    return True, "Command allowed by default profile"


def evaluate_command_policy(
    command: str,
    *,
    mode: str,
    profile: str,
) -> CommandPolicyDecision:
    mode_value = (mode or CommandPolicyMode.AUDIT.value).lower()
    profile_value = (profile or CommandPolicyProfile.DEFAULT.value).lower()
    root = get_command_root(command)

    if mode_value == CommandPolicyMode.OFF.value:
        return CommandPolicyDecision(
            allowed=True,
            reason="Command policy is disabled",
            profile=profile_value,
            mode=mode_value,
            command_root=root,
        )

    if profile_value == CommandPolicyProfile.READONLY.value:
        allowed, reason = evaluate_readonly(command, root)
    elif profile_value == CommandPolicyProfile.OPS.value:
        allowed, reason = evaluate_ops(command, root)
    else:
        allowed, reason = evaluate_default(command, root)

    if mode_value == CommandPolicyMode.AUDIT.value:
        return CommandPolicyDecision(
            allowed=True,
            reason=f"AUDIT_ONLY: would_allow={allowed}; {reason}",
            profile=profile_value,
            mode=mode_value,
            command_root=root,
        )

    return CommandPolicyDecision(
        allowed=allowed,
        reason=reason,
        profile=profile_value,
        mode=mode_value,
        command_root=root,
    )
