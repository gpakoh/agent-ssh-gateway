"""Command policy engine — profile-based command authorization (C3).

Modes (CommandPolicyMode):
    off     — policy disabled, all commands allowed
    audit   — policy evaluates and logs decisions but does not block
    enforce — policy blocks commands not matching the selected profile
    ask     — policy creates ApprovalRequest for blocked commands (operator
              approves/denies via API, 300s TTL). The caller must retry the
              identical command; the retry is what actually consumes the
              approval and lets it through (see find_and_consume_approval)
              — approving a request does not itself execute anything.

Profiles:
    readonly             — read-only inspection only (cat, ls, git status, ...)
    testlint             — pytest/ruff/mypy/compileall + readonly
    project-automation   — project-automation + testlint + git read-only
    ops/docker-admin     — limited service/docker operations + project-automation
    default              — deny obviously dangerous root commands (defense-in-depth)

Agent override:
    COMMAND_POLICY_AGENT_MODES maps agent name → mode (bypasses global mode).
    Resolution: caller-provided mode → agent_modes[agent] → global mode.
    Agent auto-detected via env/proc (detect_agent()).

Evaluation pipeline (evaluate_command_policy):
    0. Allowlist (Gate 0)      — agent/project/user/system four-layer hierarchy
                                 with TTL; exact/prefix/regex/rule_id selectors.
                                 Bypasses ALL subsequent gates.
    1. Metachar denial (Gate 1) — blanket block on | ; && || ` $() $() $[]
                                 Always enforced in enforce mode.
    2. Argument shape (Gate 2)  — language interpreters (python -c, bash -c),
                                 find -exec, dangerous patterns, URL w/ password.
    2b. Heredoc scanner (Gate 2b) — inline scripts, heredocs, herestrings,
                                 command substitutions; extracted content runs
                                 through full profile evaluation recursively.
    3. Profile eval (Gate 3)    — profile-specific root allowlist match.
    4. Denylist (Gate 4)        — defense-in-depth denylist (rarely hit).

Gate behavior by mode:
    off:     all gates skipped, commands always allowed.
    audit:   gates run, decisions logged but not enforced.
    enforce: gates 1-2 always block; gate 2b blocks; gate 3 blocks;
             gate 4 blocks.
    ask:     gates 1-2 always block; gates 2b-3 create ApprovalRequest;
             gate 4 blocks.

Decision output (CommandPolicyDecision):
    allowed: bool
    mode: CommandPolicyMode
    profile: str
    blocked_by: str | None  — which gate blocked (None if allowed)
    reason: str
    agent: str | None
    suggestion: str | None  — first matching destructive-pattern suggestion
    requires_approval: bool
    approval_id: str | None
    effective_packs: list[str]

Key modules:
    app/command_policy.py   — gates, profiles, evaluate (982 loc)
    app/command_policy.py   — per-agent mode resolution in §804-814
    app/command_policy.py   — parse_agent_modes() parser in §969-980
    app/policy_ask.py       — ApprovalRequest store (95 loc, in-memory, 300s TTL)
    app/allowlist.py        — four-layer allowlist (agent/project/user/system)
    app/heredoc_scanner.py  — 2-tier heredoc extraction + recursive check
    app/agent_profiles.py   — TrustLevel, AgentProfile, effective_packs()
    app/config.py           — COMMAND_POLICY_AGENT_MODES env var

Destructive pattern packs (app/packs/):
    docker, filesystem, kubernetes, cloud, database,
    git, firewall, loadbalancer, system
"""





from __future__ import annotations

import re
import shlex
from collections.abc import Callable
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


class SuggestionKind(StrEnum):
    """Category of a suggestion. Mirrors DCG's suggestion kinds.

    - PREVIEW_FIRST: inspect/check state before acting (look before you leap)
    - SAFER_ALTERNATIVE: do this less destructive thing instead
    - WORKFLOW_FIX: fix the workflow that led to the dangerous command
    - DOCUMENTATION: point to docs for more context
    - ALLOW_SAFELY: how to allowlist this specific rule safely
    """
    PREVIEW_FIRST = "preview_first"
    SAFER_ALTERNATIVE = "safer_alternative"
    WORKFLOW_FIX = "workflow_fix"
    DOCUMENTATION = "documentation"
    ALLOW_SAFELY = "allow_safely"


@dataclass(frozen=True)
class PatternSuggestion:
    """Safe alternative command suggestion."""
    command: str
    description: str
    kind: SuggestionKind = SuggestionKind.SAFER_ALTERNATIVE


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
    suggestions: tuple[PatternSuggestion, ...] = field(default_factory=tuple)
    confidence: float = 0.5


@dataclass(frozen=True)
class CommandPolicyDecision:
    allowed: bool
    reason: str
    profile: str
    mode: str
    command_root: str | None = None
    requires_approval: bool = False
    approval_id: str | None = None
    suggestion: str | None = None
    suggestions: tuple[str, ...] = ()


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


def _is_safe_bash_syntax_check(effective: list[str]) -> bool:
    """Return True only for the narrow non-executing ``bash -n`` form.

    Bash's ``-n`` parses a script without executing commands, but it is not
    safe to treat ``-n`` generically because combined/additional flags can
    re-enable execution or interactive/startup-file behavior.  Allow only:
    ``bash -n script`` and ``bash -n -- script``.
    """
    if not effective or effective[0] != "bash":
        return False
    args = effective[1:]
    if len(args) == 2:
        flag, script = args
        return flag == "-n" and script != "-" and not script.startswith("-")
    if len(args) == 3:
        flag, separator, script = args
        return flag == "-n" and separator == "--" and script != "-"
    return False


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

    # Check language interpreters with exec flags (anywhere in args).
    # Bash has one deliberately narrow syntax-check exception; all other
    # interpreter/flag combinations keep the generic fail-closed behavior.
    if effective[0] in BLOCKED_INTERPRETERS and not _is_safe_bash_syntax_check(effective):
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

# branch/tag/remote/reflog are only conditionally read-only: the
# subcommand NAME is safe, but plenty of their own flags/positional
# arguments mutate repo state (git branch -D <name> deletes a branch,
# git tag <name> creates one, git remote set-url rewrites a remote).
# See _validate_git_subcommand.
_GIT_INFO_ONLY_FLAGS: set[str] = {
    "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
    "-l", "--list", "--color", "--no-color", "--column", "--no-column",
    "-n", "--contains", "--merged", "--no-merged",
}
_GIT_MUTATING_REMOTE_SUBCOMMANDS: set[str] = {
    "add", "remove", "rm", "rename", "set-url", "set-head",
    "set-branches", "prune", "update",
}
_GIT_READONLY_REMOTE_SUBCOMMANDS: set[str] = {"show", "get-url"}

# `env` is in every profile's root allowlist to let a caller set env vars
# before an otherwise-allowed command (e.g. `env FOO=bar pytest ...`), but
# the command env actually executes was never itself re-validated against
# the active profile -- any command reachable via `env <cmd>` bypassed
# every profile's allowlist and destructive-pattern checks entirely. See
# _unwrap_env / the `root == "env"` branch in each evaluate_* function.
_ENV_NO_ARG_FLAGS: set[str] = {
    "-i", "--ignore-environment", "-0", "--null", "-v", "--verbose",
    "-h", "--help",
}
_ENV_ONE_ARG_FLAGS: set[str] = {
    "-u", "--unset", "-C", "--chdir", "-S", "--split-string",
    "-P", "--default-signal", "-a", "--argv0", "--block-signal",
    "--list-signal-names",
}
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

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
    "powershell", "pwsh", "cmd",
}

# Root-command prefixes that are always denied regardless of suffix, e.g.
# the real-world `mkfs.ext4` / `mkfs.xfs` / `mkfs.vfat` wrapper binaries
# (bare `mkfs -t <type>` is rare in practice; the `mkfs.<fstype>` form is
# the one actually shipped on most distros and must be denied the same way).
DENIED_ROOT_PREFIXES: tuple[str, ...] = ("mkfs.",)


def _normalize_root(root: str) -> str:
    """Lowercase and strip a Windows-style ``.exe`` suffix for denylist checks.

    Root-command comparisons must not be case-sensitive: on case-insensitive
    targets (macOS, Windows-via-OpenSSH) ``Shutdown``/``REBOOT``/``Mv`` run
    identically to their lowercase form, so a case-sensitive ``in`` check is
    a trivial denylist bypass.
    """
    lowered = root.lower()
    if lowered.endswith(".exe"):
        lowered = lowered[: -len(".exe")]
    return lowered


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
    suggestions: tuple[dict, ...] = ()
    confidence: float | None = None


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


def _get_suggestion(command: str) -> str | None:
    """Return the first suggestion command from destructive patterns matching command."""
    for m in _check_all_destructive(command):
        if m.suggestion:
            return m.suggestion
        if m.suggestions:
            return m.suggestions[0].command
    return None


def _suggestion_dict(s: PatternSuggestion) -> dict:
    """Serialize a PatternSuggestion for API output."""
    return {"command": s.command, "description": s.description, "kind": s.kind.value}


def _get_suggestions(command: str) -> tuple[str, ...]:
    """Return formatted suggestions from destructive patterns matching command."""
    formatted: list[str] = []
    for m in _check_all_destructive(command):
        for s in m.suggestions:
            formatted.append(f"{s.command} — {s.description}")
    return tuple(formatted)


def scan_command(command: str) -> ScanReport:
    """Evaluate a command string against ALL registered destructive patterns.

    Unlike the policy engine (which returns ALLOW/BLOCK based on profile),
    scan_command returns ALL matching destructive patterns regardless of
    profile — for introspection, debugging, and CI.

    The scanner is INFORMATIONAL, not an enforcement control: enforcement
    happens in the policy gates (metachar, interpreter-shape, heredoc gates
    in evaluate_command_policy). To reduce blind spots from shell obfuscation
    (e.g. ``bash -c 'x=rm; $x -rf /'`` where the regex sees no literal
    ``rm -rf /``), the command is normalized before matching: ``VAR=value``
    assignments are resolved against ``$VAR``/``${VAR}`` references and
    nested inline scripts (``bash -c '...'``, heredocs, ``$(...)``) are
    extracted and scanned as separate candidates. Findings are deduplicated
    by pattern name so the same pattern matched in multiple variants is
    reported once.
    """
    from app.ast_matcher import check_ast
    from app.heredoc_scanner import extract_python_scripts, normalize_scan_candidates

    matches: list[DestructiveMatch] = []
    seen_patterns: set[str] = set()
    for candidate in normalize_scan_candidates(command):
        for m in _check_all_destructive(candidate):
            if m.pattern_name not in seen_patterns:
                seen_patterns.add(m.pattern_name)
                matches.append(m)

    # AST pass over extracted Python bodies: regex packs cannot see
    # ``shutil.rmtree`` behind an import, the AST matcher can.
    for script in extract_python_scripts(command):
        for ast_match in check_ast(script, "python"):
            if ast_match.rule_id not in seen_patterns:
                seen_patterns.add(ast_match.rule_id)
                matches.append(
                    DestructiveMatch(
                        pattern_name=ast_match.rule_id,
                        reason=ast_match.reason,
                        severity=Severity(ast_match.severity.value),
                        suggestion=ast_match.suggestion,
                    )
                )

    findings = [
        ScanFinding(
            pattern_name=m.pattern_name,
            severity=m.severity.value,
            reason=m.reason,
            suggestion=m.suggestion,
            suggestions=tuple(_suggestion_dict(s) for s in m.suggestions),
            confidence=m.confidence,
        )
        for m in matches
    ]

    return ScanReport(
        findings=tuple(findings),
        total=len(findings),
    )


def _validate_git_subcommand(parts: list[str]) -> tuple[bool, str]:
    """Validate git subcommand for read-only profiles.

    GIT_READONLY_SUBCOMMANDS only vets the subcommand NAME. branch/tag/
    remote/reflog are each read-only in SOME invocations and mutating in
    others depending on their own flags/positional args -- confirmed live:
    `git branch -D x` (deletes a branch), `git tag -d v1` (deletes a tag),
    and `git remote set-url origin ...` (rewrites a remote) all passed the
    subcommand-only check with zero findings despite being writes.
    """
    if len(parts) < 2:
        return True, ""

    subcmd = parts[1]
    if subcmd not in GIT_READONLY_SUBCOMMANDS:
        return False, f"git subcommand '{subcmd}' not allowed (only read-only: {', '.join(sorted(GIT_READONLY_SUBCOMMANDS))})"

    rest = parts[2:]

    if subcmd in ("branch", "tag"):
        # Any positional (non-flag) argument names a branch/tag to
        # create, delete, or rename -- always a write. `git branch` /
        # `git tag` with only informational flags (-a, -v, -l, ...) just
        # lists, which is the only read-only shape either subcommand has.
        positional = [a for a in rest if not a.startswith("-")]
        if positional:
            return False, f"git {subcmd} with a name argument is a write operation, not allowed"
        unknown_flags = [a for a in rest if a.startswith("-") and a not in _GIT_INFO_ONLY_FLAGS]
        if unknown_flags:
            return False, f"git {subcmd} flag '{unknown_flags[0]}' not recognized as read-only"
        return True, ""

    if subcmd == "remote":
        if not rest:
            return True, ""
        first = rest[0]
        if first in _GIT_MUTATING_REMOTE_SUBCOMMANDS:
            return False, f"git remote {first} is a write operation, not allowed"
        if first.startswith("-"):
            if first not in _GIT_INFO_ONLY_FLAGS:
                return False, f"git remote flag '{first}' not recognized as read-only"
            return True, ""
        if first in _GIT_READONLY_REMOTE_SUBCOMMANDS:
            return True, ""
        return False, f"git remote '{first}' not recognized as read-only"

    if subcmd == "reflog":
        if rest and rest[0] in ("expire", "delete"):
            return False, f"git reflog {rest[0]} is a write operation, not allowed"
        return True, ""

    return True, ""


def _unwrap_env(parts: list[str]) -> list[str]:
    """Return the argv of the command `env` actually executes.

    Skips env's own flags and leading NAME=VALUE assignments so profile
    checks (allowlist, dangerous-token scan, git-subcommand validation)
    run against the real target instead of stopping at "env" itself.
    ``parts[0]`` is assumed to be "env".
    """
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok in _ENV_NO_ARG_FLAGS:
            i += 1
            continue
        if tok in _ENV_ONE_ARG_FLAGS:
            i += 2
            continue
        if _ENV_ASSIGNMENT_RE.match(tok):
            i += 1
            continue
        break
    return parts[i:]


def _evaluate_env_wrapped(
    command: str,
    allowed_roots: set[str],
    evaluator: Callable[[str, str | None], tuple[bool, str]],
) -> tuple[bool, str]:
    """Unwrap `env ...` and re-run the given profile evaluator on the
    command env actually executes, instead of trusting env's own
    root-allowlist membership (which says nothing about its target)."""
    wrapped = _unwrap_env(get_command_parts(command))
    if not wrapped:
        return False, "env with no wrapped command not allowed"
    wrapped_root = wrapped[0]
    if wrapped_root not in allowed_roots:
        return False, f"env-wrapped command '{wrapped_root}' not in allowlist"
    return evaluator(shlex.join(wrapped), wrapped_root)


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
        if match.suggestions:
            msg += " (safer: " + "; ".join(f"{s.command} — {s.description}" for s in match.suggestions) + ")"
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

    # env's own root-allowlist membership says nothing about the command
    # it actually executes -- re-validate that command against this same
    # profile instead of stopping at "env" itself.
    if root == "env":
        return _evaluate_env_wrapped(command, READONLY_ROOTS, evaluate_readonly)

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

    if root == "env":
        return _evaluate_env_wrapped(command, TESTLINT_ROOTS, evaluate_testlint)

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

    if root == "env":
        return _evaluate_env_wrapped(command, PROJECT_AUTOMATION_ROOTS, evaluate_project_automation)

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

    if root == "env":
        return _evaluate_env_wrapped(command, OPS_ROOTS, evaluate_ops)

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

    if root == "env":
        return _evaluate_env_wrapped(command, DOCKER_ADMIN_ROOTS, evaluate_docker_admin)

    ok, reason = _validate_docker_command(get_command_parts(command), DOCKER_ADMIN_ALLOWED_ACTIONS)
    if not ok:
        return False, reason

    return True, "Allowed by docker-admin profile"


def evaluate_default(command: str, root: str | None) -> tuple[bool, str]:
    """Default profile: deny known dangerous roots + defense-in-depth denylist.

    Every other profile (readonly/testlint/project-automation/ops/docker-admin)
    is an allowlist keyed on root command, so a root like kubectl/terraform/aws/
    mysql/psql/iptables is already rejected outright by "not in allowlist" --
    default is the *only* profile that can reach those roots at all. Without
    also consulting the pack registry here, none of the kubernetes/cloud/
    database/firewall/... destructive-pattern packs were ever checked for a
    plain top-level command -- only for content nested inside heredocs/inline
    scripts (heredoc_scanner.check_nested_commands) and for docker specifically
    (_validate_docker_action, ops/docker-admin only). A bare
    `kubectl delete namespace prod --force` or `terraform destroy -auto-approve`
    passed default clean despite matching patterns that exist precisely to
    catch them.
    """
    if root is None:
        return False, "Command cannot be parsed"

    normalized_root = _normalize_root(root)
    if normalized_root in DENIED_ROOTS or normalized_root.startswith(DENIED_ROOT_PREFIXES):
        return False, f"Root command '{root}' denied (defense-in-depth)"

    dangerous = contains_dangerous_token(command)
    if dangerous:
        return False, f"Dangerous token detected: {dangerous}"

    destructive = _check_all_destructive(command)
    if destructive:
        m = destructive[0]
        return False, f"Destructive pattern blocked: {m.reason}"

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
    """Defense-in-depth: detect known dangerous tokens.

    Matches against the shlex-resolved token stream, not the raw string.
    shlex.split() collapses shell quote-concatenation (r''m -> rm) and
    backslash-escaping (\\rm -> rm) per POSIX parsing rules the same way a
    real shell would -- a naive substring match on the untouched original
    text does not, and confirmed live let `env r''m -rf /` and
    `env \\rm -rf /` both slip past the " rm -rf " denylist entry
    undetected. Falls back to the raw string when the command doesn't even
    shlex-parse (unusual enough on its own to keep checking conservatively).
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = []
    normalized = f" {' '.join(tokens).lower()} " if tokens else normalize_command(command).lower()

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
    agent: str | None = None,
    project: str | None = None,
    user: str | None = None,
) -> CommandPolicyDecision:
    """Evaluate a command against the policy engine.

    Enforce and audit modes run the **same** decision pipeline.  Enforce
    returns the result directly; audit always returns ``allowed=True`` but
    sets ``reason`` to ``"AUDIT_ONLY: would_allow=<bool>; <reason>"`` so
    callers can observe what *would* have happened.

    Ask mode: gates 1 (metachar) and 2 (argument_shape) still block;
    gates 2b (heredoc) and 3 (profile) first check for an already-approved
    request matching this exact command+profile (consuming it and
    allowing through if found), otherwise create a new pending approval
    request instead of blocking.

    The allowlist (agent > project > user > system) is checked before any
    gates — if matched, the command is allowed immediately.

    Per‐agent mode: if ``*_AGENT_MODES`` is configured and the calling
    agent (detected or explicitly passed) has a mapping, it overrides the
    global ``mode`` parameter.
    """
    # Resolve per-agent mode override
    if agent is None:
        from app.agent_profiles import detect_agent
        agent = detect_agent()
    if agent:
        from app.config import settings
        agent_modes = parse_agent_modes(settings.command_policy_agent_modes)
        if agent in agent_modes:
            mode = agent_modes[agent]

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

    # Gate 0: allowlist — bypasses all policy gates if matched
    from app.allowlist import get_allowlist
    allowlist = get_allowlist()
    allow_match = allowlist.check(
        command,
        agent=agent,
        project=project,
        user=user,
    )
    if allow_match is not None:
        entry = allow_match.entry
        return CommandPolicyDecision(
            allowed=True,
            reason=(
                f"Allowlist match [{entry.layer}] "
                f"{entry.selector_type}={entry.selector_value}"
                f"{'; ' + entry.reason if entry.reason else ''}"
            ),
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
                suggestion=_get_suggestion(command),
                suggestions=_get_suggestions(command),
            )
        # Already approved by an operator? Consume that approval and let
        # this (re-)evaluation through instead of creating yet another
        # pending request the caller has no way to ever collect on.
        from app.policy_ask import create_approval_request, find_and_consume_approval
        approved = find_and_consume_approval(command, profile_value)
        if approved is not None:
            return CommandPolicyDecision(
                allowed=True,
                reason=f"Approved by operator ({approved.approved_by}), approval_id={approved.approval_id}",
                profile=profile_value,
                mode=mode_value,
                command_root=root,
            )

        # Gates 2b (heredoc) and 3 (profile) → ask for approval
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
            suggestion=_get_suggestion(command),
            suggestions=_get_suggestions(command),
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
            suggestion=_get_suggestion(command) if not allowed else None,
            suggestions=_get_suggestions(command) if not allowed else (),
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


def parse_agent_modes(raw: str) -> dict[str, str]:
    """Parse COMMAND_POLICY_AGENT_MODES JSON string into dict.

    Returns empty dict on parse failure (fail-open to global mode).
    """
    import json
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v).lower() for k, v in data.items()}
    except (json.JSONDecodeError, TypeError):
        pass
    return {}
