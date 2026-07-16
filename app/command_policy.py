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
from enum import StrEnum


class CommandPolicyMode(StrEnum):
    OFF = "off"
    AUDIT = "audit"
    ENFORCE = "enforce"


class CommandPolicyProfile(StrEnum):
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


# ---------------------------------------------------------------------------
# Shell redirection scanner — detects unquoted redirection operators
# ---------------------------------------------------------------------------

# Ordered by length (longest first) so greedy matching works correctly.
_REDIRECTION_OPS: tuple[str, ...] = (
    "1>>", "2>>",  # fd-append (must precede single-char variants)
    "1>", "2>", "&>", ">|",  # fd-redirect / clobber / stderr
    ">>", "<<",   # append / heredoc
    ">", "<",     # simple redirect
)


def contains_shell_redirection(command: str) -> str | None:
    """Detect unquoted shell redirection operators in *command*.

    Walks the command string character-by-character, tracking single/double
    quote state, and returns the first redirection operator found outside of
    any quoted region.  Returns ``None`` when the command is clean.

    Detected operators (file-write guardrail):
        ``>`` ``>>`` ``>|`` ``1>`` ``2>`` ``&>`` ``1>>`` ``2>>``

    Input redirections ``<`` ``<<`` are also flagged because they can
    read arbitrary files and are rarely needed in agent-issued commands.

    Quoted strings are **not** flagged::

        >>> contains_shell_redirection('echo "a > b"')
        >>> contains_shell_redirection('echo x>file')
        '>'
    """
    n = len(command)
    i = 0
    in_single = False
    in_double = False

    while i < n:
        ch = command[i]

        # --- quote tracking ---------------------------------------------------
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\":
                i += 2  # skip escaped char inside double quotes
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue

        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue

        # --- skip backslash-escaped char outside quotes -----------------------
        if ch == "\\":
            i += 2
            continue

        # --- redirection detection (outside quotes) ---------------------------
        for op in _REDIRECTION_OPS:
            if command[i : i + len(op)] == op:
                return op

        i += 1

    return None


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

    redir = contains_shell_redirection(command)
    if redir:
        return redir

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
