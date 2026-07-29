"""Command policy engine — profile-based command authorization (C3).

Modes:
    off     — policy disabled, all commands allowed
    audit   — policy logs decisions but does not block
    enforce — policy blocks commands not matching the selected profile

Profiles:
    readonly          — read-only inspection only
    testlint          — pytest/ruff/mypy/compileall + readonly
    project-automation — project-automation + testlint + git read-only
    ops/docker-admin  — limited service/docker operations + project-automation
    default           — deny obviously dangerous root commands (defense-in-depth)

Security model:
    1. Blanket metachar denial (| ; && || ` $(...)) — always enforced in enforce mode
    2. Argument-shape checks — language interpreters, find -exec, dangerous patterns
    3. Profile-specific root allowlist
    4. Denylist as defense-in-depth only
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


class CommandPolicyMode(StrEnum):
    OFF = "off"
    AUDIT = "audit"
    ENFORCE = "enforce"
    ASK = "ask"


class CommandPolicyProfile(StrEnum):
    DEFAULT = "default"
    READONLY = "readonly"
    TESTLINT = "testlint"
    PROJECT_AUTOMATION = "project-automation"
    OPS = "ops"
    DOCKER_ADMIN = "docker-admin"


class Severity(StrEnum):
    """Severity level for destructive docker/compose patterns.

    Mirrors DCG severity levels:
    - Critical: irreversible, always block
    - High: block by default (allowlistable)
    - Medium: warn (log + allow)
    - Low: log only
    """
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class PatternSuggestion:
    """Safe alternative command suggestion."""
    command: str
    description: str


@dataclass(frozen=True)
class DestructivePattern:
    """Docker/compose destructive pattern definition (ported from DCG)."""
    name: str
    regex: str
    reason: str
    severity: Severity
    description: str
    suggestions: tuple[PatternSuggestion, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DestructiveMatch:
    """Result of a destructive pattern match."""
    pattern_name: str
    reason: str
    severity: Severity
    suggestion: str | None = None


@dataclass(frozen=True)
class CommandPolicyDecision:
    allowed: bool
    reason: str
    profile: str
    mode: str
    command_root: str | None = None
    requires_approval: bool = False
    approval_id: str | None = None


# ---------------------------------------------------------------------------
# Blanket metachar denial — always enforced in enforce mode
# ---------------------------------------------------------------------------

METACHAR_DENYLIST: tuple[str, ...] = (
    "|",    # pipe
    ";",    # statement separator
    "&&",   # logical AND
    "||",   # logical OR
    "`",    # backtick command substitution
    "$(",   # dollar-paren command substitution
)


def contains_metachar(command: str) -> str | None:
    """Detect forbidden metacharacters outside quoted regions.

    Returns the first metachar found, or None.
    """
    n = len(command)
    i = 0
    in_single = False
    in_double = False

    while i < n:
        ch = command[i]

        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\":
                i += 2
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

        if ch == "\\":
            i += 2
            continue

        # Check metachars
        for mc in METACHAR_DENYLIST:
            if command[i : i + len(mc)] == mc:
                return mc

        i += 1

    return None


# ---------------------------------------------------------------------------
# Shell redirection scanner
# ---------------------------------------------------------------------------

_REDIRECT_OPS: tuple[str, ...] = (
    "1>>", "2>>", "1>", "2>", "&>", ">|",
    ">>", "<<", ">", "<",
)


def contains_shell_redirection(command: str) -> str | None:
    """Detect unquoted shell redirection operators."""
    n = len(command)
    i = 0
    in_single = False
    in_double = False

    while i < n:
        ch = command[i]

        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\":
                i += 2
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

        if ch == "\\":
            i += 2
            continue

        for op in _REDIRECT_OPS:
            if command[i : i + len(op)] == op:
                return op

        i += 1

    return None


# ---------------------------------------------------------------------------
# Argument-shape checks
# ---------------------------------------------------------------------------

# Language interpreters that execute arbitrary code
BLOCKED_INTERPRETERS: set[str] = {
    "python", "python2", "python3",
    "perl", "perl5",
    "ruby", "ruby3",
    "bash", "sh", "ash", "zsh", "ksh",
    "node", "deno", "bun",
}
# Flags that enable code execution (single-letter, used in combined flags too)
EXEC_FLAGS: set[str] = {
    "-c",   # sh/bash/python: execute command string
    "-e",   # perl/ruby: execute code; sh: exit on error
    "-E",   # ruby: enable warnings
    "-es",  # perl: switch + source
    "-ex",  # sh/bash: set -ex
    "-0",   # perl: set record separator (can slurp entire files)
    "-n",   # perl: implicit loop (pairs with -e for code execution)
    "-w",   # ruby: enable warnings (dangerous when combined with -e)
}

# find -exec / write-capability flags are dangerous
FIND_DENYLIST: set[str] = {
    "-exec", "-execdir", "-ok", "-okdir",
    "-delete", "-fprintf", "-fls",
}

# sed -i / --in-place mutates files in place — not read-only
SED_INPLACE_DENYLIST: set[str] = {"-i", "--in-place"}

# command -v is safe (existence check); command <anything-else> can execute
COMMAND_ALLOWED_FLAGS: set[str] = {"-v"}


def _extract_single_flags(arg: str) -> list[str]:
    """Extract single-letter flags from a combined flag argument.

    ``-uc`` → ["-u", "-c"], ``-0e`` → ["-0", "-e"], ``--verbose`` → [].
    """
    if not arg.startswith("-") or arg.startswith("--") or len(arg) < 2:
        return []
    return ["-" + ch for ch in arg[1:]]


def check_argument_shape(command: str) -> tuple[bool, str]:
    """Check for dangerous argument patterns.

    Scans ALL arguments (not only args[1]) for:
    - Combined flags: ``python3 -uc``, ``perl -0e``, ``ruby -we``
    - Separated flags: ``python3 -u -c``, ``sh -e -c``
    - find -exec patterns

    Returns (is_dangerous, reason).
    """
    parts = get_command_parts(command)
    if not parts:
        return False, ""

    root = parts[0]
    effective = parts[1:] if root == "sudo" else parts

    if not effective:
        return False, ""

    # Check language interpreters with exec flags (anywhere in args)
    if effective[0] in BLOCKED_INTERPRETERS:
        for arg in effective[1:]:
            # Combined: python3 -uc → "-uc" contains "-c"
            if any(flag in EXEC_FLAGS for flag in _extract_single_flags(arg)):
                return True, (
                    f"Language interpreter '{effective[0]}' with exec flag "
                    f"in '{arg}' blocked"
                )
            # Separated: python3 -u -c → "-c" is a standalone flag
            if arg in EXEC_FLAGS:
                return True, (
                    f"Language interpreter '{effective[0]}' with exec flag "
                    f"'{arg}' blocked"
                )

    # Check find -exec
    if effective[0] == "find":
        for arg in effective[1:]:
            if arg in FIND_DENYLIST:
                return True, f"find argument '{arg}' blocked (arbitrary execution)"

    # Check sed -i / --in-place (in-place file mutation)
    if effective[0] == "sed":
        for arg in effective[1:]:
            if arg in SED_INPLACE_DENYLIST:
                return True, f"sed argument '{arg}' blocked (in-place mutation not allowed)"
            # Combined: sed -ni → contains -i
            if any(flag in SED_INPLACE_DENYLIST for flag in _extract_single_flags(arg)):
                return True, f"sed argument '{arg}' contains in-place flag (not allowed)"

    # Check command: only "command -v <tool>" (existence check) is safe
    if effective[0] == "command":
        flags = [a for a in effective[1:] if a.startswith("-")]
        # Only -v is allowed; no other flags
        if not flags or any(f not in COMMAND_ALLOWED_FLAGS for f in flags):
            return True, "command without -v flag blocked (only command -v <tool> allowed)"

    return False, ""


# ---------------------------------------------------------------------------
# Root command allowlists by profile
# ---------------------------------------------------------------------------

READONLY_ROOTS: set[str] = {
    "cat", "cd", "df", "du", "env", "free", "grep", "head", "hostname",
    "id", "ip", "journalctl", "ls", "netstat", "pgrep", "ping", "ps",
    "pwd", "readlink", "realpath", "ss", "stat", "tail", "top", "tree",
    "uname", "uptime", "wc", "whoami", "file", "less",
    "git",
}

# Git read-only subcommands
GIT_READONLY_SUBCOMMANDS: set[str] = {
    "status", "log", "diff", "show", "branch", "remote", "tag",
    "rev-parse", "describe", "shortlog", "blame", "reflog",
}

TESTLINT_ROOTS: set[str] = READONLY_ROOTS | {
    "pytest", "ruff", "mypy", "pyright", "flake8", "black", "isort",
    "compileall", "python", "uv",
    "command", "find", "sed",
}

PROJECT_AUTOMATION_ROOTS: set[str] = TESTLINT_ROOTS | {
    "git",
}

OPS_ROOTS: set[str] = PROJECT_AUTOMATION_ROOTS | {
    "docker", "docker-compose",
    "systemctl", "service", "supervisorctl",
    "journalctl", "systemd-analyze",
}

DOCKER_ADMIN_ROOTS: set[str] = OPS_ROOTS | {
    "docker", "docker-compose",
}

DENIED_ROOTS: set[str] = {
    "mkfs", "fdisk", "parted", "shutdown", "reboot", "halt", "poweroff",
    "dd", "tee", "cp", "mv", "rm", "rmdir",
}


# ---------------------------------------------------------------------------
# Argument validators
# ---------------------------------------------------------------------------

OPS_ALLOWED_SYSTEMCTL_ACTIONS: set[str] = {
    "status", "restart", "reload", "try-restart", "is-active", "is-enabled",
    "start", "stop",
}

OPS_ALLOWED_DOCKER_ACTIONS: set[str] = {
    "ps", "logs", "inspect", "restart", "compose", "images", "stats",
}

DOCKER_ADMIN_ALLOWED_ACTIONS: set[str] = OPS_ALLOWED_DOCKER_ACTIONS | {
    "exec", "rm", "rmi", "volume", "run", "start", "stop",
    "kill", "cp", "wait", "rename", "update", "pause", "unpause",
}


def _check_docker_destructive(command: str) -> DestructiveMatch | None:
    """Check a docker command against destructive patterns via PackRegistry."""
    from app.packs.registry import get_registry
    matches = get_registry().evaluate_pack("docker", command)
    return matches[0] if matches else None


def _check_compose_destructive(command: str) -> DestructiveMatch | None:
    """Check a docker-compose command against compose-specific destructive patterns."""
    from app.packs.registry import get_registry
    matches = get_registry().evaluate_pack("docker", command)
    compose_names = {"down-volumes", "down-rmi-all", "rm-volumes", "compose-rm-force"}
    for m in matches:
        if m.pattern_name in compose_names:
            return m
    return None


# ---------------------------------------------------------------------------
# Scan tool — evaluate command against ALL registered patterns
# ---------------------------------------------------------------------------

# Registry of all scan-checker functions for extensibility
# Each checker is callable(command: str) -> DestructiveMatch | None
_SCAN_CHECKERS: list[DestructiveMatch | None] = []  # type: ignore[valid-type]


@dataclass(frozen=True)
class ScanFinding:
    """One finding from scanning a command against destructive patterns."""
    pattern_name: str
    severity: str
    reason: str
    suggestion: str | None = None


@dataclass(frozen=True)
class ScanReport:
    """Complete scan result for a single command."""
    findings: tuple[ScanFinding, ...]
    total: int


def _check_all_destructive(command: str) -> list[DestructiveMatch]:
    """Check a command against ALL compiled patterns and return ALL matches.

    Uses the PackRegistry (``app/packs``) to evaluate all registered packs
    with keyword-based quick-reject for performance.
    """
    from app.packs.registry import get_registry
    return get_registry().evaluate(command)


def scan_command(command: str) -> ScanReport:
    """Evaluate a command string against ALL registered destructive patterns.

    Unlike the policy engine (which returns ALLOW/BLOCK based on profile),
    scan_command returns ALL matching destructive patterns regardless of
    profile — for introspection, debugging, and CI.

    Currently checks:
    - Docker destructive patterns (Phase 1)
    - Docker Compose destructive patterns (Phase 1)

    Extensible: add new checkers to ``_SCAN_CHECKERS`` as new packs are ported.
    """
    matches = _check_all_destructive(command)

    findings = [
        ScanFinding(
            pattern_name=m.pattern_name,
            severity=m.severity.value,
            reason=m.reason,
            suggestion=m.suggestion,
        )
        for m in matches
    ]

    return ScanReport(
        findings=tuple(findings),
        total=len(findings),
    )


def _validate_git_subcommand(parts: list[str]) -> tuple[bool, str]:
    """Validate git subcommand for read-only profiles."""
    if len(parts) < 2:
        return True, ""

    subcmd = parts[1]
    if subcmd not in GIT_READONLY_SUBCOMMANDS:
        return False, f"git subcommand '{subcmd}' not allowed (only read-only: {', '.join(sorted(GIT_READONLY_SUBCOMMANDS))})"

    return True, ""


def _validate_ops_command(parts: list[str]) -> tuple[bool, str]:
    """Validate ops-level command."""
    if not parts:
        return False, "Empty command"

    root = parts[0]
    effective = parts[1:] if root == "sudo" else parts

    if not effective:
        return False, "Empty command after sudo"

    if effective[0] in {"systemctl", "service"}:
        if len(effective) < 2:
            return False, "Missing service action"
        action = effective[1]
        if action not in OPS_ALLOWED_SYSTEMCTL_ACTIONS:
            return False, f"systemctl action '{action}' not allowed"
        return True, ""

    if effective[0] in {"docker", "docker-compose"}:
        return _validate_docker_action(effective, OPS_ALLOWED_DOCKER_ACTIONS)

    return True, ""


def _validate_docker_command(parts: list[str], allowed_actions: set[str]) -> tuple[bool, str]:
    """Validate docker command against a specific allowed actions set."""
    if not parts:
        return False, "Empty command"

    root = parts[0]
    effective = parts[1:] if root == "sudo" else parts

    if not effective:
        return False, "Empty command after sudo"

    if effective[0] in {"docker", "docker-compose"}:
        return _validate_docker_action(effective, allowed_actions)

    return True, ""


def _validate_docker_action(effective: list[str], allowed_actions: set[str]) -> tuple[bool, str]:
    """Validate docker action against allowed actions set.

    After the allowlist check, runs defense-in-depth destructive pattern
    matching (ported from DCG containers pack) to catch dangerous flag
    combinations like ``rm -f``, ``volume prune``, ``down -v``.
    """
    if len(effective) < 2:
        return False, "Missing docker action"
    action = effective[1]
    if action not in allowed_actions:
        return False, f"docker action '{action}' not allowed"

    # Defense-in-depth: check destructive docker/compose patterns
    from app.packs.registry import get_registry
    cmd = " ".join(effective)
    matches = get_registry().evaluate_pack("docker", cmd)
    for match in matches:
        msg = f"Destructive docker operation blocked: {match.reason}"
        if match.suggestion:
            msg += f" (safer: {match.suggestion})"
        return False, msg

    return True, ""


# ---------------------------------------------------------------------------
# Profile evaluators
# ---------------------------------------------------------------------------

def evaluate_readonly(command: str, root: str | None) -> tuple[bool, str]:
    if root is None:
        return False, "Command cannot be parsed"

    if root not in READONLY_ROOTS:
        return False, f"Root command '{root}' not in readonly allowlist"

    dangerous = contains_dangerous_token(command)
    if dangerous:
        return False, f"Dangerous token detected: {dangerous}"

    # Git read-only check
    if root == "git":
        ok, reason = _validate_git_subcommand(get_command_parts(command))
        if not ok:
            return False, reason

    return True, "Allowed by readonly profile"


def evaluate_testlint(command: str, root: str | None) -> tuple[bool, str]:
    if root is None:
        return False, "Command cannot be parsed"

    if root not in TESTLINT_ROOTS:
        return False, f"Root command '{root}' not in testlint allowlist"

    dangerous = contains_dangerous_token(command)
    if dangerous:
        return False, f"Dangerous token detected: {dangerous}"

    # Git read-only check
    if root == "git":
        ok, reason = _validate_git_subcommand(get_command_parts(command))
        if not ok:
            return False, reason

    return True, "Allowed by testlint profile"


def evaluate_project_automation(command: str, root: str | None) -> tuple[bool, str]:
    if root is None:
        return False, "Command cannot be parsed"

    if root not in PROJECT_AUTOMATION_ROOTS:
        return False, f"Root command '{root}' not in project-automation allowlist"

    dangerous = contains_dangerous_token(command)
    if dangerous:
        return False, f"Dangerous token detected: {dangerous}"

    # Git read-only check
    if root == "git":
        ok, reason = _validate_git_subcommand(get_command_parts(command))
        if not ok:
            return False, reason

    return True, "Allowed by project-automation profile"


def evaluate_ops(command: str, root: str | None) -> tuple[bool, str]:
    if root is None:
        return False, "Command cannot be parsed"

    if root not in OPS_ROOTS:
        return False, f"Root command '{root}' not in ops allowlist"

    dangerous = contains_dangerous_token(command)
    if dangerous:
        return False, f"Dangerous token detected: {dangerous}"

    ok, reason = _validate_ops_command(get_command_parts(command))
    if not ok:
        return False, reason

    return True, "Allowed by ops profile"


def evaluate_docker_admin(command: str, root: str | None) -> tuple[bool, str]:
    if root is None:
        return False, "Command cannot be parsed"

    if root not in DOCKER_ADMIN_ROOTS:
        return False, f"Root command '{root}' not in docker-admin allowlist"

    dangerous = contains_dangerous_token(command)
    if dangerous:
        return False, f"Dangerous token detected: {dangerous}"

    ok, reason = _validate_docker_command(get_command_parts(command), DOCKER_ADMIN_ALLOWED_ACTIONS)
    if not ok:
        return False, reason

    return True, "Allowed by docker-admin profile"


def evaluate_default(command: str, root: str | None) -> tuple[bool, str]:
    """Default profile: deny known dangerous roots + defense-in-depth denylist."""
    if root is None:
        return False, "Command cannot be parsed"

    if root in DENIED_ROOTS:
        return False, f"Root command '{root}' denied (defense-in-depth)"

    dangerous = contains_dangerous_token(command)
    if dangerous:
        return False, f"Dangerous token detected: {dangerous}"

    return True, "Allowed by default profile"


# ---------------------------------------------------------------------------
# Main evaluator
# ---------------------------------------------------------------------------

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
    """Defense-in-depth: detect known dangerous tokens."""
    normalized = normalize_command(command).lower()

    DENYLIST_TOKENS: tuple[str, ...] = (
        " rm -rf ", " mkfs ", " dd if=", " :(){",
        "chmod 777", "chown -R", "curl ", "wget ",
        " nc ", " netcat ",
    )
    for token in DENYLIST_TOKENS:
        if token in normalized:
            return token.strip()

    redir = contains_shell_redirection(command)
    if redir:
        return redir

    return None


def _evaluate_enforce_decision(
    command: str,
    profile_value: str,
) -> tuple[bool, str, str | None]:
    """Run the full enforce-mode decision pipeline.

    Returns (allowed, reason, blocked_by) where blocked_by is the gate that
    rejected the command (metachar / argument_shape / profile) or None.
    """
    root = get_command_root(command)

    # Gate 1: blanket metachar denial
    metachar = contains_metachar(command)
    if metachar:
        return False, f"Metacharacter '{metachar}' blocked by blanket denial", "metachar"

    # Gate 2: argument-shape checks
    is_dangerous, shape_reason = check_argument_shape(command)
    if is_dangerous:
        return False, shape_reason, "argument_shape"

    # Gate 2b: heredoc/inline script nested command scan
    # Extracts and recursively scans scripts inside python -c, bash -c,
    # <<EOF heredocs, $(...) substitutions, etc.
    from app.heredoc_scanner import check_nested_commands
    nested_findings = check_nested_commands(command)
    if nested_findings:
        names = sorted({f.pattern_name for f in nested_findings})
        reasons = [f.reason for f in nested_findings[:3]]
        detail = "; ".join(reasons)
        return False, (
            f"Heredoc/inline script contains destructive pattern(s): "
            f"{', '.join(names)}. {detail}"
        ), "heredoc"

    # Gate 3: profile evaluation
    evaluators = {
        CommandPolicyProfile.READONLY.value: evaluate_readonly,
        CommandPolicyProfile.TESTLINT.value: evaluate_testlint,
        CommandPolicyProfile.PROJECT_AUTOMATION.value: evaluate_project_automation,
        CommandPolicyProfile.OPS.value: evaluate_ops,
        CommandPolicyProfile.DOCKER_ADMIN.value: evaluate_docker_admin,
    }
    evaluator = evaluators.get(profile_value, evaluate_default)
    allowed, reason = evaluator(command, root)

    if not allowed:
        return False, reason, "profile"

    return True, reason, None


def evaluate_command_policy(
    command: str,
    *,
    mode: str,
    profile: str,
) -> CommandPolicyDecision:
    """Evaluate a command against the policy engine.

    Enforce and audit modes run the **same** decision pipeline.  Enforce
    returns the result directly; audit always returns ``allowed=True`` but
    sets ``reason`` to ``"AUDIT_ONLY: would_allow=<bool>; <reason>"`` so
    callers can observe what *would* have happened.

    Ask mode: gates 1 (metachar) and 2 (argument_shape) still block;
    gates 2b (heredoc) and 3 (profile) create a pending approval request
    instead of blocking.
    """
    mode_value = (mode or CommandPolicyMode.AUDIT.value).lower()
    profile_value = (profile or CommandPolicyProfile.DEFAULT.value).lower()
    root = get_command_root(command)

    # OFF mode: everything allowed
    if mode_value == CommandPolicyMode.OFF.value:
        return CommandPolicyDecision(
            allowed=True,
            reason="Command policy is disabled",
            profile=profile_value,
            mode=mode_value,
            command_root=root,
        )

    # ASK mode: same pipeline, but non-critical gates can be escalated to operator
    if mode_value == CommandPolicyMode.ASK.value:
        allowed, reason, blocked_by = _evaluate_enforce_decision(
            command, profile_value,
        )
        if allowed:
            return CommandPolicyDecision(
                allowed=True,
                reason=reason,
                profile=profile_value,
                mode=mode_value,
                command_root=root,
            )
        # Gates 1 and 2 always block even in ASK mode (too dangerous)
        if blocked_by in ("metachar", "argument_shape"):
            return CommandPolicyDecision(
                allowed=False,
                reason=reason,
                profile=profile_value,
                mode=mode_value,
                command_root=root,
            )
        # Gates 2b (heredoc) and 3 (profile) → ask for approval
        from app.policy_ask import create_approval_request
        approval = create_approval_request(
            command=command,
            profile=profile_value,
            blocked_by=blocked_by,
            reason=reason,
        )
        return CommandPolicyDecision(
            allowed=False,
            reason=reason,
            profile=profile_value,
            mode=mode_value,
            command_root=root,
            requires_approval=True,
            approval_id=approval.approval_id,
        )

    # Enforce mode: run the full decision pipeline
    if mode_value == CommandPolicyMode.ENFORCE.value:
        allowed, reason, _blocked_by = _evaluate_enforce_decision(
            command, profile_value,
        )
        return CommandPolicyDecision(
            allowed=allowed,
            reason=reason,
            profile=profile_value,
            mode=mode_value,
            command_root=root,
        )

    # AUDIT mode: same pipeline, but always allow and report would_allow
    allowed, reason, _blocked_by = _evaluate_enforce_decision(
        command, profile_value,
    )
    return CommandPolicyDecision(
        allowed=True,
        reason=f"AUDIT_ONLY: would_allow={allowed}; {reason}",
        profile=profile_value,
        mode=mode_value,
        command_root=root,
    )


# ---------------------------------------------------------------------------
# Server-owned profile resolution
# ---------------------------------------------------------------------------


def profile_for_identity(
    identity_fingerprint: str | None = None,
    *,
    key_profiles: dict[str, str] | None = None,
    default_profile: str = "default",
) -> str:
    """Resolve the effective command policy profile for an authenticated identity.

    Server-owned mapping: API key fingerprint → profile name.
    If no mapping exists for the fingerprint, falls back to default_profile.

    Args:
        identity_fingerprint: truncated API key hash (first 12 chars).
        key_profiles: mapping from fingerprint to profile name.
        default_profile: fallback profile if no mapping found.

    Returns:
        Profile name string.
    """
    if not identity_fingerprint or not key_profiles:
        return default_profile

    return key_profiles.get(identity_fingerprint, default_profile)


def parse_key_profiles(raw: str) -> dict[str, str]:
    """Parse COMMAND_POLICY_KEY_PROFILES JSON string into dict.

    Returns empty dict on parse failure (fail-open to default profile).
    """
    import json
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, TypeError):
        pass
    return {}
