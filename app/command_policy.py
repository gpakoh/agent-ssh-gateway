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

import re
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

# ---------------------------------------------------------------------------
# DCG-ported destructive docker patterns — defense-in-depth
# ---------------------------------------------------------------------------

DOCKER_DESTRUCTIVE_PATTERNS: tuple[DestructivePattern, ...] = (
    DestructivePattern(
        name="system-prune",
        regex=r"docker\b.*?\bsystem\s+prune",
        reason="docker system prune removes ALL unused containers, networks, images, and build cache",
        severity=Severity.HIGH,
        description="docker system prune is Docker's most aggressive cleanup command. "
        "With --volumes it also removes all unused volumes (data loss!).",
        suggestions=(
            PatternSuggestion("docker system df -v", "Preview what would be removed without deleting"),
            PatternSuggestion("docker container prune", "Remove only stopped containers"),
            PatternSuggestion("docker image prune", "Remove only dangling images"),
        ),
    ),
    DestructivePattern(
        name="volume-prune",
        regex=r"docker\b.*?\bvolume\s+prune",
        reason="docker volume prune permanently deletes ALL unused volumes and their data",
        severity=Severity.HIGH,
        description="docker volume prune deletes all volumes not attached to a running container. "
        "Database data, uploads, and application state are lost permanently.",
        suggestions=(
            PatternSuggestion("docker volume ls", "List all volumes first"),
            PatternSuggestion("docker volume rm <specific-volume>", "Remove specific volumes by name"),
        ),
    ),
    DestructivePattern(
        name="network-prune",
        regex=r"docker\b.*?\bnetwork\s+prune",
        reason="docker network prune removes ALL unused networks",
        severity=Severity.HIGH,
        description="docker network prune removes all user-defined networks not used by any container.",
        suggestions=(
            PatternSuggestion("docker network ls", "List networks before pruning"),
            PatternSuggestion("docker network rm <specific-network>", "Remove specific networks"),
        ),
    ),
    DestructivePattern(
        name="image-prune",
        regex=r"docker\b.*?\bimage\s+prune",
        reason="docker image prune removes unused (dangling) images",
        severity=Severity.MEDIUM,
        description="docker image prune removes dangling images. With -a flag, removes ALL unused images.",
        suggestions=(
            PatternSuggestion("docker images -f dangling=true", "List dangling images first"),
            PatternSuggestion("docker rmi <image-id>", "Remove specific images by ID"),
        ),
    ),
    DestructivePattern(
        name="container-prune",
        regex=r"docker\b.*?\bcontainer\s+prune",
        reason="docker container prune removes ALL stopped containers",
        severity=Severity.MEDIUM,
        description="docker container prune removes all stopped containers. "
        "Container logs and filesystem layers are lost.",
        suggestions=(
            PatternSuggestion("docker ps -a -f status=exited", "List stopped containers first"),
            PatternSuggestion("docker rm <specific-container>", "Remove specific containers"),
        ),
    ),
    DestructivePattern(
        name="rm-force",
        regex=r"docker\b.*?\brm\s+.*(?:-[a-zA-Z0-9]*f|--force)",
        reason="docker rm -f forcibly removes containers without graceful shutdown",
        severity=Severity.HIGH,
        description="docker rm -f sends SIGKILL instead of SIGTERM. "
        "Running processes are killed immediately, in-flight requests dropped, data may be corrupted.",
        suggestions=(
            PatternSuggestion("docker stop <container> && docker rm <container>",
                              "Graceful shutdown (SIGTERM) before removal"),
            PatternSuggestion("docker ps -a | grep <container>", "Check container status before removal"),
        ),
    ),
    DestructivePattern(
        name="rmi-force",
        regex=r"docker\b.*?\brmi\s+.*(?:-[a-zA-Z0-9]*f|--force)",
        reason="docker rmi -f forcibly removes images even if in use by containers",
        severity=Severity.HIGH,
        description="docker rmi -f forces image removal, potentially breaking running containers.",
        suggestions=(
            PatternSuggestion("docker rmi <image>", "Remove without force (fails safely if in use)"),
            PatternSuggestion("docker image prune", "Remove only dangling images"),
        ),
    ),
    DestructivePattern(
        name="volume-rm",
        regex=r"docker\b.*?\bvolume\s+rm",
        reason="docker volume rm permanently deletes volumes and their data",
        severity=Severity.HIGH,
        description="docker volume rm permanently deletes named volumes. "
        "Database files, uploads, and configuration in the volume are lost forever.",
        suggestions=(
            PatternSuggestion("docker volume inspect <volume>", "Inspect volume metadata first"),
            PatternSuggestion("docker run --rm -v <volume>:/data alpine ls -la /data",
                              "List volume contents before deletion"),
        ),
    ),
    DestructivePattern(
        name="stop-all",
        regex=r"docker\b.*?\b(?:stop|kill)\s+\$\(",
        reason="Stopping/killing all containers can disrupt running services",
        severity=Severity.HIGH,
        description="This pattern stops or kills ALL running containers via command substitution. "
        "Production services go down, database connections are severed.",
        suggestions=(
            PatternSuggestion("docker stop <container-name>", "Stop specific containers by name"),
            PatternSuggestion("docker ps --format '{{.Names}}: {{.Status}}'",
                              "List running containers before stopping"),
        ),
    ),
)

COMPOSE_DESTRUCTIVE_PATTERNS: tuple[DestructivePattern, ...] = (
    DestructivePattern(
        name="down-volumes",
        regex=r"(?:docker-compose|docker\s+compose)\s+down\s+.*(?:-v\b|--volumes)",
        reason="docker-compose down -v removes volumes and their data permanently",
        severity=Severity.CRITICAL,
        description="The -v/--volumes flag causes down to remove named volumes declared in Compose. "
        "Database data, uploads, and persistent state are permanently destroyed.",
        suggestions=(
            PatternSuggestion("docker-compose down", "Stops containers without touching volumes"),
            PatternSuggestion("docker-compose stop", "Stops containers, preserves everything"),
        ),
    ),
    DestructivePattern(
        name="down-rmi-all",
        regex=r"(?:docker-compose|docker\s+compose)\s+down\s+.*--rmi\s+all",
        reason="docker-compose down --rmi all removes all images used by services",
        severity=Severity.HIGH,
        description="The --rmi all flag removes all images used by services. "
        "Base images must be re-pulled, custom images need rebuilding.",
        suggestions=(
            PatternSuggestion("docker-compose down", "Preserves images for faster restarts"),
            PatternSuggestion("docker-compose down --rmi local", "Only removes images without custom tag"),
        ),
    ),
    DestructivePattern(
        name="rm-volumes",
        regex=r"(?:docker-compose|docker\s+compose)\s+rm\s+.*(?:-v\b|--volumes)",
        reason="docker-compose rm -v removes volumes attached to containers",
        severity=Severity.HIGH,
        description="The -v flag with rm removes anonymous volumes. "
        "Application state, session data, and caches may be lost.",
        suggestions=(
            PatternSuggestion("docker-compose rm", "Removes containers without touching volumes"),
            PatternSuggestion("docker-compose stop", "Stops without removing anything"),
        ),
    ),
    DestructivePattern(
        name="rm-force",
        regex=r"(?:docker-compose|docker\s+compose)\s+rm\s+.*(?:-f\b|--force)",
        reason="docker-compose rm -f forcibly removes containers without confirmation",
        severity=Severity.MEDIUM,
        description="The -f flag removes containers without asking. "
        "Running containers are stopped abruptly (SIGKILL).",
        suggestions=(
            PatternSuggestion("docker-compose stop", "Graceful shutdown first"),
            PatternSuggestion("docker-compose rm", "Asks for confirmation"),
        ),
    ),
)

# Precompile patterns
_COMPILED_DOCKER_PATTERNS: list[tuple[re.Pattern, DestructivePattern]] = [
    (re.compile(p.regex, re.IGNORECASE), p) for p in DOCKER_DESTRUCTIVE_PATTERNS
]
_COMPILED_COMPOSE_PATTERNS: list[tuple[re.Pattern, DestructivePattern]] = [
    (re.compile(p.regex, re.IGNORECASE), p) for p in COMPOSE_DESTRUCTIVE_PATTERNS
]

# ---------------------------------------------------------------------------
# DCG-ported destructive filesystem patterns
# ---------------------------------------------------------------------------

FILESYSTEM_DESTRUCTIVE_PATTERNS: tuple[DestructivePattern, ...] = (
    DestructivePattern(
        name="rm-rf-root",
        regex=r"\brm\b.*(?:-[a-zA-Z0-9]*(?:r[a-zA-Z0-9]*f|f[a-zA-Z0-9]*r)"
        r"|--recursive\s+--force|--force\s+--recursive)"
        r'\s+["\']?(?:/|/\*)["\']?(?:\s|&&|\|\||;|\||$|#)',
        reason="rm -rf targeting root filesystem (/) will DESTROY THE OPERATING SYSTEM",
        severity=Severity.CRITICAL,
        description="Recursive force-delete on / wipes the entire filesystem. "
        "In a container this is unrecoverable without rebuild. In Docker it "
        "destroys the container's writable layer including application data.",
        suggestions=(
            PatternSuggestion(
                "rm -rf /path/to/specific/directory",
                "Always target specific directories, never the root filesystem",
            ),
            PatternSuggestion(
                "ls -la /important/path",
                "Verify what you intend to delete before running rm",
            ),
            PatternSuggestion(
                "find / -maxdepth 1 | head -20",
                "List top-level directories before considering deletion",
            ),
        ),
    ),
    DestructivePattern(
        name="rm-rf-sensitive",
        regex=r"\brm\b.*(?:-[a-zA-Z0-9]*(?:r[a-zA-Z0-9]*f|f[a-zA-Z0-9]*r)"
        r"|--recursive\s+--force|--force\s+--recursive)"
        r'\s+/'
        r"(?:etc(?=[ /\t]|$)|var(?=[ /\t]|$)|boot(?=[ /\t]|$)|dev(?=[ /\t]|$)"
        r"|proc(?=[ /\t]|$)|sys(?=[ /\t]|$)|usr(?=[ /\t]|$)"
        r"|lib(?=[ /\t]|$)|bin(?=[ /\t]|$)|sbin(?=[ /\t]|$)"
        r"|opt(?=[ /\t]|$)|root(?=[ /\t]|$))",
        reason="rm -rf targeting a system-critical directory (/{path}) "
        "will BREAK the operating system or container",
        severity=Severity.CRITICAL,
        description="System directories like /etc, /var, /usr contain critical files. "
        "Deleting them will break applications, networking, package management, or ssh.",
        suggestions=(
            PatternSuggestion(
                "rm -rf /tmp/specific/subdir",
                "Use /tmp for temporary deletions (safe temp directory)",
            ),
            PatternSuggestion(
                "ls -la /etc/specific-path",
                "List the directory before considering any deletion",
            ),
            PatternSuggestion(
                "cp -a /etc /etc.bak && rm -rf /etc/specific-path",
                "Backup first, then remove only the specific sub-path",
            ),
        ),
    ),
    DestructivePattern(
        name="rm-rf",
        regex=r"\brm\b.*(?:-[a-zA-Z0-9]*(?:r[a-zA-Z0-9]*f|f[a-zA-Z0-9]*r)"
        r"|--recursive\s+--force|--force\s+--recursive)",
        reason="rm -rf is destructive — recursively forces deletion without confirmation",
        severity=Severity.HIGH,
        description="Recursive force-delete is the most dangerous filesystem command. "
        "Use interactive removal or trash for safety.",
        suggestions=(
            PatternSuggestion(
                "rm -ri {path}",
                "Interactive mode: confirms each file before deletion",
            ),
            PatternSuggestion(
                "ls -la {path}",
                "List directory contents to verify before deletion",
            ),
            PatternSuggestion(
                "mv {path} /tmp/delete-me-$(date +%s)",
                "Move to temp area instead of deleting immediately",
            ),
            PatternSuggestion(
                "find {path} -type f | head -20",
                "Preview files that would be deleted",
            ),
        ),
    ),
    DestructivePattern(
        name="rm-recursive",
        regex=r"\brm\b.*\s+(?:-(?:[a-zA-Z]*[rR][a-zA-Z]*)|--recursive)(?:\s|$)",
        reason="Recursive rm can silently remove an entire directory tree",
        severity=Severity.MEDIUM,
        description="Recursive rm without --force is less aggressive but still "
        "deletes entire directory trees without individual file prompts.",
        suggestions=(
            PatternSuggestion(
                "rm -ri {path}",
                "Add -i for interactive confirmation per file",
            ),
            PatternSuggestion(
                "ls -laR {path}",
                "Recursively list all contents before deleting",
            ),
            PatternSuggestion(
                "mv {path} /tmp/delete-me",
                "Move to /tmp instead of immediate deletion",
            ),
        ),
    ),
    DestructivePattern(
        name="find-delete",
        regex=r"\bfind\b.*\s+-delete\b",
        reason="find -delete recursively removes files matching the search — "
        "bytewise equivalent to rm -rf on the matching tree",
        severity=Severity.HIGH,
        description="find -delete silently removes every matched file. "
        "Combine with -print to preview; never use on system directories.",
        suggestions=(
            PatternSuggestion(
                "find /tmp -type f | head -20",
                "Preview files before adding -delete",
            ),
            PatternSuggestion(
                "find /tmp -type f -print -delete",
                "Use -print to log every deleted file",
            ),
            PatternSuggestion(
                "find /tmp -type f | wc -l",
                "Count files that would be deleted before proceeding",
            ),
        ),
    ),
    DestructivePattern(
        name="find-exec-rm",
        regex=r"\bfind\b.*\s+-exec\b.*\brm\b",
        reason="find -exec rm runs rm on every matched file — bulk deletion",
        severity=Severity.HIGH,
        description="find combined with -exec rm deletes every file matched by "
        "the find expression. Particularly dangerous with -prune or -type.",
        suggestions=(
            PatternSuggestion(
                "find /tmp -type f | head -20",
                "Preview files first, then add -exec rm",
            ),
            PatternSuggestion(
                "find /tmp -type f -ok rm {} \\;",
                "Use -ok for confirmation prompts",
            ),
            PatternSuggestion(
                "find /tmp -type f -print -exec rm {} \\;",
                "Log every deletion with -print",
            ),
        ),
    ),
    DestructivePattern(
        name="dd-block-device",
        regex=r"\bdd\b.*\bof=\s*/dev/(?:sd[a-z]|nvme\d+n\d+|vd[a-z]|mmcblk\d+|loop\d+|dm-\d+|md\d+)",
        reason="dd writing directly to a block device will DESTROY filesystem and data",
        severity=Severity.CRITICAL,
        description="dd of=/dev/sdX overwrites the raw block device, destroying "
        "the partition table, filesystem, and all data. Equivalent to physically "
        "destroying the disk.",
        suggestions=(
            PatternSuggestion(
                "lsblk",
                "List block devices to verify the correct target",
            ),
            PatternSuggestion(
                "dd if=/dev/zero of=/tmp/test.img bs=1M count=100",
                "Write to a file instead of a block device",
            ),
            PatternSuggestion(
                "dd if=/dev/urandom of=/tmp/output.dat bs=4k count=1000",
                "Use a file path, not a block device, for testing",
            ),
        ),
    ),
    DestructivePattern(
        name="mkfs-destructive",
        regex=r"\bmkfs\b",
        reason="mkfs formats a filesystem, ERASING ALL DATA on the target device",
        severity=Severity.CRITICAL,
        description="mkfs creates a new filesystem, destroying all existing data "
        "on the partition. Common variants: mkfs.ext4, mkfs.xfs, mkfs.btrfs.",
        suggestions=(
            PatternSuggestion(
                "lsblk -f",
                "Check existing filesystems before formatting",
            ),
            PatternSuggestion(
                "blkid /dev/sdX1",
                "Verify the target partition and its current contents",
            ),
            PatternSuggestion(
                "mount | grep /dev/sdX",
                "Ensure the target is not currently mounted",
            ),
        ),
    ),
    DestructivePattern(
        name="shred-destructive",
        regex=r"\bshred\b.*(?:-[a-zA-Z0-9]*u\b|--remove)",
        reason="shred -u overwrites AND removes files — no recovery possible",
        severity=Severity.HIGH,
        description="shred -u overwrites the file with random data (multiple passes) "
        "then unlinks it. Contents are unrecoverable even with forensic tools.",
        suggestions=(
            PatternSuggestion(
                "ls -la {path}",
                "Verify the path before shredding (no recovery)",
            ),
            PatternSuggestion(
                "cp {path} {path}.bak && shred -u {path}",
                "Make a backup first if you might need the data",
            ),
            PatternSuggestion(
                "rm -P {path}",
                "Single-pass overwrite then delete (FreeBSD/macOS)",
            ),
        ),
    ),
)

_COMPILED_FILESYSTEM_PATTERNS: list[tuple[re.Pattern, DestructivePattern]] = [
    (re.compile(p.regex, re.IGNORECASE), p) for p in FILESYSTEM_DESTRUCTIVE_PATTERNS
]


# ---------------------------------------------------------------------------
# DCG-ported destructive kubernetes patterns
# ---------------------------------------------------------------------------

KUBERNETES_DESTRUCTIVE_PATTERNS: tuple[DestructivePattern, ...] = (
    # kubectl delete namespace
    DestructivePattern(
        name="kubectl-delete-namespace",
        regex=r"kubectl\b.*?\bdelete\s+(?:namespace|ns)\b",
        reason="kubectl delete namespace removes the entire namespace and ALL resources within it",
        severity=Severity.CRITICAL,
        description="Deleting a namespace destroys EVERYTHING inside it:\n\n"
        "- All deployments, pods, services\n"
        "- All configmaps and secrets\n"
        "- All persistent volume claims (data may be lost)\n"
        "- All ingresses and network policies\n\n"
        "This is irreversible.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete ns {ns} --dry-run=client -o yaml",
                "Preview what would be deleted without making changes",
            ),
            PatternSuggestion(
                "kubectl get all -n {ns}",
                "See all resources in the namespace before deleting",
            ),
            PatternSuggestion(
                "kubectl delete ns {ns} --grace-period=60",
                "Allow graceful shutdown with 60-second grace period",
            ),
        ),
    ),
    # kubectl delete --all
    DestructivePattern(
        name="kubectl-delete-all",
        regex=r"kubectl\b.*?\bdelete\s+.*--all\b",
        reason="kubectl delete --all removes ALL resources of that type",
        severity=Severity.HIGH,
        description="The --all flag deletes EVERY resource of the specified type. "
        "kubectl delete pods --all kills all pods. "
        "kubectl delete pvc --all may delete all persistent data.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete {resource} --all --dry-run=client",
                "Preview what would be deleted without making changes",
            ),
            PatternSuggestion(
                "kubectl rollout restart deployment/{name}",
                "Restart pods via deployment for graceful recreation",
            ),
            PatternSuggestion(
                "kubectl delete {resource} {specific-name}",
                "Delete a specific resource instead of all",
            ),
        ),
    ),
    # kubectl delete -A / --all-namespaces
    DestructivePattern(
        name="kubectl-delete-all-namespaces",
        regex=r"kubectl\b.*?\bdelete\s+.*(?:-A\b|--all-namespaces)",
        reason="kubectl delete with -A/--all-namespaces affects ALL namespaces — very dangerous",
        severity=Severity.CRITICAL,
        description="The -A/--all-namespaces flag expands deletion to EVERY namespace "
        "including system namespaces (kube-system). This can take down your entire cluster.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete {resource} -n {namespace}",
                "Always specify a namespace explicitly",
            ),
            PatternSuggestion(
                "kubectl get {resource} -A",
                "Preview cluster-wide resources before making changes",
            ),
        ),
    ),
    # kubectl drain node
    DestructivePattern(
        name="kubectl-drain-node",
        regex=r"kubectl\b.*?\bdrain\b",
        reason="kubectl drain evicts all pods from a node — can cause service disruption",
        severity=Severity.HIGH,
        description="kubectl drain evicts ALL pods from a node. "
        "Use PodDisruptionBudgets to protect critical workloads. "
        "DaemonSet pods remain unless --ignore-daemonsets is used.",
        suggestions=(
            PatternSuggestion(
                "kubectl get pods -o wide | grep {node}",
                "Check what's running on the node before draining",
            ),
            PatternSuggestion(
                "kubectl get pdb -A",
                "Check disruption budgets before eviction",
            ),
            PatternSuggestion(
                "kubectl cordon {node}",
                "Cordon first: prevent new pods, then drain gradually",
            ),
        ),
    ),
    # kubectl cordon node
    DestructivePattern(
        name="kubectl-cordon-node",
        regex=r"kubectl\b.*?\bcordon\b",
        reason="kubectl cordon marks a node unschedulable. Existing pods continue running.",
        severity=Severity.MEDIUM,
        description="kubectl cordon marks a node as unschedulable. "
        "Existing pods continue running but no new pods will be scheduled.",
        suggestions=(
            PatternSuggestion(
                "kubectl uncordon {node}",
                "To reverse: uncordon the node",
            ),
            PatternSuggestion(
                "kubectl describe node {node} | grep Taints",
                "Check node status and taints",
            ),
        ),
    ),
    # kubectl taint with NoExecute
    DestructivePattern(
        name="kubectl-taint-noexecute",
        regex=r"kubectl\b.*?\btaint\s+.*:NoExecute\b",
        reason="kubectl taint with NoExecute evicts existing pods without toleration",
        severity=Severity.HIGH,
        description="A NoExecute taint immediately evicts pods that don't have a matching "
        "toleration. More aggressive than NoSchedule — existing pods are evicted.",
        suggestions=(
            PatternSuggestion(
                "kubectl describe node {node} | grep Taints",
                "Check current taints before modifying",
            ),
            PatternSuggestion(
                "kubectl taint nodes {node} key=value:NoSchedule",
                "Consider NoSchedule first (only blocks new pods)",
            ),
            PatternSuggestion(
                "kubectl taint nodes {node} key=value:NoExecute-",
                "Remove a NoExecute taint",
            ),
        ),
    ),
    # kubectl delete deployment/statefulset/daemonset/replicaset
    DestructivePattern(
        name="kubectl-delete-workload",
        regex=r"kubectl\b.*?\bdelete\s+(?:deployment|statefulset|daemonset|replicaset)\b",
        reason="kubectl delete deployment/statefulset/daemonset removes the workload and all pods",
        severity=Severity.HIGH,
        description="Deleting a workload terminates all its pods. "
        "Consider scaling down first for controlled shutdown.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete {type} {name} --dry-run=client",
                "Preview without making changes",
            ),
            PatternSuggestion(
                "kubectl get pods -l app={name}",
                "Check affected pods before deleting",
            ),
            PatternSuggestion(
                "kubectl scale deployment {name} --replicas=0",
                "Scale to zero first for controlled shutdown",
            ),
        ),
    ),
    # kubectl delete pvc
    DestructivePattern(
        name="kubectl-delete-pvc",
        regex=r"kubectl\b.*?\bdelete\s+(?:pvc|persistentvolumeclaim)\b",
        reason="kubectl delete pvc may permanently delete data (depends on ReclaimPolicy)",
        severity=Severity.CRITICAL,
        description="Deleting a PVC can cause permanent data loss. "
        "Check the PV's reclaimPolicy: Delete → data lost, Retain → manual recovery.",
        suggestions=(
            PatternSuggestion(
                "kubectl describe pvc {name}",
                "Check PVC status and usage before deleting",
            ),
            PatternSuggestion(
                "kubectl get pv $(kubectl get pvc {name} -o jsonpath='{.spec.volumeName}')",
                "Check the reclaim policy of the backing PV",
            ),
            PatternSuggestion(
                "kubectl delete pvc {name} --dry-run=client",
                "Preview deletion without making changes",
            ),
        ),
    ),
    # kubectl delete pv
    DestructivePattern(
        name="kubectl-delete-pv",
        regex=r"kubectl\b.*?\bdelete\s+(?:pv|persistentvolume)\b",
        reason="kubectl delete pv may permanently delete the underlying storage",
        severity=Severity.CRITICAL,
        description="Deleting a PersistentVolume can destroy the underlying storage: "
        "cloud disks (EBS, GCE PD) may be deleted, NFS mounts orphaned.",
        suggestions=(
            PatternSuggestion(
                "kubectl get pvc -A | grep {pv-name}",
                "Check what PVCs use this PV",
            ),
            PatternSuggestion(
                "kubectl get storageclass {class} -o yaml",
                "Check storage class reclaim policy",
            ),
            PatternSuggestion(
                "kubectl delete pv {name} --dry-run=client",
                "Preview without making changes",
            ),
        ),
    ),
    # kubectl scale --replicas=0
    DestructivePattern(
        name="kubectl-scale-to-zero",
        regex=r"kubectl\b.*?\bscale\s+.*--replicas=0\b",
        reason="kubectl scale --replicas=0 stops ALL pods for the workload",
        severity=Severity.HIGH,
        description="Scaling to zero terminates ALL pods. "
        "Service becomes unavailable, in-flight requests dropped.",
        suggestions=(
            PatternSuggestion(
                "kubectl get deployment {name} -o jsonpath='{.spec.replicas}'",
                "Check current replica count before scaling",
            ),
            PatternSuggestion(
                "kubectl scale deployment {name} --replicas={N}",
                "Scale to a non-zero value to restore service",
            ),
        ),
    ),
    # kubectl delete --force --grace-period=0
    DestructivePattern(
        name="kubectl-delete-force",
        regex=r"kubectl\b.*?\bdelete\s+.*--force.*--grace-period=0|kubectl\b.*?\bdelete\s+.*--grace-period=0.*--force",
        reason="kubectl delete --force --grace-period=0 immediately removes resources "
        "without graceful shutdown",
        severity=Severity.CRITICAL,
        description="Force deletion with zero grace period kills pods immediately "
        "(no SIGTERM). In-flight requests fail, finalizers may be skipped.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete pod {name}",
                "Use default 30-second grace period for graceful shutdown",
            ),
            PatternSuggestion(
                "kubectl describe pod {name} | grep -A5 Status",
                "Check why pod is stuck before force-deleting",
            ),
        ),
    ),
    # kubectl apply --force
    DestructivePattern(
        name="kubectl-apply-force",
        regex=r"kubectl\b.*?\bapply\s+.*--force\b",
        reason="kubectl apply --force deletes and recreates resources, causing downtime",
        severity=Severity.HIGH,
        description="kubectl apply --force deletes the resource and recreates it. "
        "Causes downtime and potential data loss for stateful workloads.",
        suggestions=(
            PatternSuggestion(
                "kubectl diff -f {file}",
                "Preview what changes would be applied",
            ),
            PatternSuggestion(
                "kubectl apply --server-side -f {file}",
                "Use server-side apply for safer updates",
            ),
        ),
    ),
    # kubectl delete -f - (stdin)
    DestructivePattern(
        name="kubectl-delete-from-stdin",
        regex=r"kubectl\b.*?\bdelete\b.*?(?:-f(?:=|\s+)?|--filename(?:=|\s+))"
        r"""["']?(?:[^,"'\s]+,)*-(?:,[^,"'\s]+)*["']?(?=\s|$)""",
        reason="kubectl delete -f - deletes resources piped from stdin "
        "without a reviewable manifest path",
        severity=Severity.HIGH,
        description="Deleting from stdin means the manifest isn't reviewable. "
        "Materialize the manifest first and use --dry-run=client.",
        suggestions=(
            PatternSuggestion(
                "kustomize build {dir} > /tmp/manifest.yaml",
                "Save manifest to a file for review first",
            ),
            PatternSuggestion(
                "kubectl diff -f /tmp/manifest.yaml",
                "Preview what will change before deleting",
            ),
        ),
    ),
    # kubectl delete -f with directory or --recursive
    DestructivePattern(
        name="kubectl-delete-from-directory",
        regex=r"kubectl\b.*?\bdelete\s+-f\s+\.\s*$|kubectl\b.*?\bdelete\s+-f\s+\./|"
        r"kubectl\b.*?\bdelete\s+--recursive\s+-f|kubectl\b.*?\bdelete\s+-f.*--recursive",
        reason="kubectl delete -f with directories or --recursive "
        "deletes many resources at once",
        severity=Severity.HIGH,
        description="Deleting from a directory removes ALL resources defined in those files. "
        "Multiple deployments, services, configmaps deleted at once.",
        suggestions=(
            PatternSuggestion(
                "ls -la {dir}/*.yaml",
                "List files in the directory before deleting",
            ),
            PatternSuggestion(
                "kubectl diff -f {dir}",
                "Preview what would change",
            ),
            PatternSuggestion(
                "kubectl delete -f {specific-file.yaml}",
                "Delete specific files instead of entire directory",
            ),
        ),
    ),
    # helm uninstall/delete
    DestructivePattern(
        name="helm-uninstall",
        regex=r"helm\b.*?\b(?:uninstall|delete)\b",
        reason="helm uninstall removes the release and ALL its Kubernetes resources",
        severity=Severity.CRITICAL,
        description="helm uninstall deletes all resources created by the chart: "
        "deployments, services, configmaps, secrets, PVCs. Use --dry-run first.",
        suggestions=(
            PatternSuggestion(
                "helm uninstall {release} --dry-run",
                "Preview what will be deleted",
            ),
            PatternSuggestion(
                "helm status {release}",
                "Review current release state before deleting",
            ),
            PatternSuggestion(
                "helm get manifest {release}",
                "See all resources managed by the release",
            ),
        ),
    ),
    # helm rollback
    DestructivePattern(
        name="helm-rollback",
        regex=r"helm\b.*?\brollback\b",
        reason="helm rollback reverts to a previous release — can cause unexpected changes",
        severity=Severity.HIGH,
        description="helm rollback reverts to a previous revision. "
        "Database migrations are NOT automatically undone. "
        "Use --dry-run to preview changes.",
        suggestions=(
            PatternSuggestion(
                "helm rollback {release} {revision} --dry-run",
                "Preview changes before rolling back",
            ),
            PatternSuggestion(
                "helm history {release}",
                "Review available revisions before rolling back",
            ),
            PatternSuggestion(
                "helm diff rollback {release} {revision}",
                "Compare changes before rolling back (requires diff plugin)",
            ),
        ),
    ),
    # helm upgrade --force
    DestructivePattern(
        name="helm-upgrade-force",
        regex=r"helm\b.*?\bupgrade\s+.*--force\b",
        reason="helm upgrade --force deletes and recreates resources, causing downtime",
        severity=Severity.HIGH,
        description="The --force flag causes Helm to delete and recreate resources "
        "instead of updating them in place. Pods are terminated and recreated.",
        suggestions=(
            PatternSuggestion(
                "helm upgrade {release} {chart}",
                "Remove --force to use rolling updates",
            ),
            PatternSuggestion(
                "helm upgrade --dry-run --debug",
                "Preview changes before upgrading",
            ),
            PatternSuggestion(
                "helm diff upgrade {release} {chart}",
                "Compare before upgrading (requires diff plugin)",
            ),
        ),
    ),
    # helm upgrade --reset-values
    DestructivePattern(
        name="helm-upgrade-reset-values",
        regex=r"helm\b.*?\bupgrade\s+.*--reset-values\b",
        reason="helm upgrade --reset-values discards all previously set values",
        severity=Severity.HIGH,
        description="The --reset-values flag discards all values from previous releases. "
        "Resource limits, replica counts, connection strings may change unexpectedly.",
        suggestions=(
            PatternSuggestion(
                "helm get values {release}",
                "Review current values before upgrading",
            ),
            PatternSuggestion(
                "helm upgrade --reuse-values",
                "Keep existing values",
            ),
        ),
    ),
    # kustomize build | kubectl delete
    DestructivePattern(
        name="kustomize-build-delete",
        regex=r"kustomize\b.*?\bbuild\s+.*\|\s*kubectl\b.*?\bdelete\b",
        reason="kustomize build | kubectl delete removes ALL resources in the kustomization",
        severity=Severity.CRITICAL,
        description="Piping kustomize build to kubectl delete removes ALL resources. "
        "Use --dry-run=client first.",
        suggestions=(
            PatternSuggestion(
                "kustomize build {dir} > /tmp/manifest.yaml",
                "Save and review manifests before deleting",
            ),
            PatternSuggestion(
                "kustomize build {dir} | kubectl delete --dry-run=client -f -",
                "Preview with dry-run first",
            ),
            PatternSuggestion(
                "kustomize build {dir} | kubectl diff -f -",
                "Compare with cluster state before deleting",
            ),
        ),
    ),
    # kubectl kustomize | kubectl delete
    DestructivePattern(
        name="kubectl-kustomize-delete",
        regex=r"kubectl\b.*?\bkustomize\s+.*\|\s*kubectl\b.*?\bdelete\b",
        reason="kubectl kustomize | kubectl delete removes ALL resources in the kustomization",
        severity=Severity.CRITICAL,
        description="Piping kubectl kustomize to kubectl delete removes ALL resources. "
        "Equivalent to kustomize build | kubectl delete.",
        suggestions=(
            PatternSuggestion(
                "kubectl kustomize {dir}",
                "Review manifests first",
            ),
            PatternSuggestion(
                "kubectl delete --dry-run=client -k {dir}",
                "Preview deletion with dry-run",
            ),
        ),
    ),
    # kubectl delete -k
    DestructivePattern(
        name="kubectl-delete-k",
        regex=r"kubectl\b.*?\bdelete\s+-k\b",
        reason="kubectl delete -k removes all resources defined in the kustomization",
        severity=Severity.CRITICAL,
        description="kubectl delete -k removes all resources in a kustomization directory. "
        "Use --dry-run=client first to preview.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete -k {dir} --dry-run=client",
                "Preview what will be deleted",
            ),
            PatternSuggestion(
                "kubectl kustomize {dir}",
                "Review manifests before deleting",
            ),
            PatternSuggestion(
                "kubectl get -k {dir}",
                "List resources that would be affected",
            ),
        ),
    ),
)

_COMPILED_KUBERNETES_PATTERNS: list[tuple[re.Pattern, DestructivePattern]] = [
    (re.compile(p.regex, re.IGNORECASE), p) for p in KUBERNETES_DESTRUCTIVE_PATTERNS
]


# ---------------------------------------------------------------------------
# DCG-ported destructive cloud provider patterns (AWS, GCP, Azure)
# ---------------------------------------------------------------------------

CLOUD_DESTRUCTIVE_PATTERNS: tuple[DestructivePattern, ...] = (
    # ---- AWS CLI ----
    DestructivePattern(
        name="aws-ec2-terminate",
        regex=r"aws\b.*?\bec2\s+terminate-instances",
        reason="aws ec2 terminate-instances permanently destroys EC2 instances",
        severity=Severity.CRITICAL,
        description="Instance is stopped and deleted. EBS volumes and Elastic IPs are lost.",
        suggestions=(
            PatternSuggestion("aws ec2 stop-instances --instance-ids i-xxx",
                              "Stop instead of terminate for recoverable pause"),
            PatternSuggestion("aws ec2 describe-instances --instance-ids i-xxx",
                              "Verify instance details before terminating"),
        ),
    ),
    DestructivePattern(
        name="aws-ec2-delete",
        regex=r"aws\b.*?\bec2\s+delete-",
        reason="aws ec2 delete-* permanently removes EC2 resources (snapshots, volumes, VPCs, AMIs)",
        severity=Severity.HIGH,
        description="EC2 delete commands: delete-snapshot, delete-volume, delete-vpc, delete-image.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-s3-rm-recursive",
        regex=r"aws\b.*?\bs3\s+rm\s+.*--recursive",
        reason="aws s3 rm --recursive permanently deletes ALL objects in the S3 path",
        severity=Severity.CRITICAL,
        description="Recursive deletion of all objects under the prefix. "
        "No recovery unless bucket versioning is enabled.",
        suggestions=(
            PatternSuggestion("aws s3 rm s3://bucket/path/ --recursive --dryrun",
                              "Preview deletions with --dryrun"),
            PatternSuggestion("aws s3 ls s3://bucket/path/ --recursive",
                              "List objects to verify before deleting"),
        ),
    ),
    DestructivePattern(
        name="aws-s3-rb",
        regex=r"aws\b.*?\bs3\s+rb\b",
        reason="aws s3 rb removes the entire S3 bucket",
        severity=Severity.CRITICAL,
        description="rb removes an S3 bucket. With --force, deletes all contents first.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-s3api-delete-bucket",
        regex=r"aws\b.*?\bs3api\s+delete-bucket",
        reason="aws s3api delete-bucket removes the S3 bucket",
        severity=Severity.CRITICAL,
        description="Bucket must be empty. Check contents before deleting.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-s3api-delete-object",
        regex=r"aws\b.*?\bs3api\s+delete-object",
        reason="aws s3api delete-object[s] permanently removes objects unless versioning is enabled",
        severity=Severity.HIGH,
        description="delete-objects is BATCH (up to 1000 keys). "
        "Without versioning, objects are permanently gone.",
        suggestions=(
            PatternSuggestion("aws s3api get-bucket-versioning --bucket xxx",
                              "Check if versioning is enabled first"),
        ),
    ),
    DestructivePattern(
        name="aws-rds-delete",
        regex=r"aws\b.*?\brds\s+delete-",
        reason="aws rds delete-* destroys database resources (instance, cluster, snapshot)",
        severity=Severity.CRITICAL,
        description="RDS delete commands remove database instances, clusters, and snapshots. "
        "Create a final snapshot before deletion.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-cfn-delete-stack",
        regex=r"aws\b.*?\bcloudformation\s+delete-stack",
        reason="aws cloudformation delete-stack removes the stack and ALL resources it created",
        severity=Severity.CRITICAL,
        description="All EC2, RDS, S3, IAM resources created by the stack are deleted.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-lambda-delete",
        regex=r"aws\b.*?\blambda\s+delete-",
        reason="aws lambda delete-* removes Lambda function, alias, or layer version",
        severity=Severity.HIGH,
        description="Function code, versions, aliases, and event source mappings are removed.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-iam-delete",
        regex=r"aws\b.*?\biam\s+delete-",
        reason="aws iam delete-* removes IAM resources (user, role, policy, group)",
        severity=Severity.HIGH,
        description="IAM deletions break authentication for users and services using those resources.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-dynamodb-delete",
        regex=r"aws\b.*?\bdynamodb\s+delete-table",
        reason="aws dynamodb delete-table permanently deletes the table and ALL data",
        severity=Severity.CRITICAL,
        description="All items, indexes, and table configuration are lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-eks-delete",
        regex=r"aws\b.*?\beks\s+delete-cluster",
        reason="aws eks delete-cluster removes the entire EKS cluster",
        severity=Severity.CRITICAL,
        description="Control plane is deleted. Node groups must be deleted separately first.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-ecr-delete-repository",
        regex=r"aws\b.*?\becr\s+delete-repository",
        reason="aws ecr delete-repository permanently deletes the repository and its images",
        severity=Severity.HIGH,
        description="All images in the repository are deleted.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-kms-schedule-key-deletion",
        regex=r"aws\b.*?\bkms\s+schedule-key-deletion",
        reason="aws kms schedule-key-deletion schedules irreversible KMS key destruction",
        severity=Severity.CRITICAL,
        description="Data encrypted with this key becomes permanently undecryptable. "
        "CancelKeyDeletion can abort within the waiting window.",
        suggestions=(
            PatternSuggestion("aws kms disable-key --key-id xxx",
                              "Disable key instead of deletion for reversible deactivation"),
        ),
    ),
    DestructivePattern(
        name="aws-secretsmanager-delete-secret",
        regex=r"aws\b.*?\bsecretsmanager\s+delete-secret",
        reason="aws secretsmanager delete-secret destroys a stored secret",
        severity=Severity.CRITICAL,
        description="30-day recovery window unless --force-delete-without-recovery used.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-route53-delete-hosted-zone",
        regex=r"aws\b.*?\broute53\s+delete-hosted-zone",
        reason="aws route53 delete-hosted-zone removes DNS zone — domains stop resolving",
        severity=Severity.CRITICAL,
        description="All DNS records deleted. Production traffic can become unroutable immediately.",
        suggestions=(
            PatternSuggestion("aws route53 list-resource-record-sets --hosted-zone-id xxx",
                              "Export records first"),
        ),
    ),
    DestructivePattern(
        name="aws-cloudtrail-delete-trail",
        regex=r"aws\b.*?\bcloudtrail\s+delete-trail",
        reason="aws cloudtrail delete-trail removes audit trail — compliance/forensics impact",
        severity=Severity.CRITICAL,
        description="Historical logs in S3 are preserved, but future events stop being recorded.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-redshift-delete-cluster",
        regex=r"aws\b.*?\bredshift\s+delete-cluster",
        reason="aws redshift delete-cluster destroys Redshift cluster and all data",
        severity=Severity.CRITICAL,
        description="With --skip-final-cluster-snapshot, ALL data is destroyed immediately.",
        suggestions=(),
    ),
    DestructivePattern(
        name="aws-logs-delete-log-group",
        regex=r"aws\b.*?\blogs\s+delete-log-group",
        reason="aws logs delete-log-group permanently deletes log group and all events",
        severity=Severity.HIGH,
        description="All log streams, events, metric filters, and subscriptions are lost.",
        suggestions=(),
    ),
    # ---- GCP / gcloud CLI ----
    DestructivePattern(
        name="gcp-compute-delete",
        regex=r"gcloud\b.*?\bcompute\s+instances\s+delete",
        reason="gcloud compute instances delete permanently destroys VM instances",
        severity=Severity.CRITICAL,
        description="Boot disk deleted unless --keep-disks specified. External IPs released.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-disk-delete",
        regex=r"gcloud\b.*?\bcompute\s+disks\s+delete",
        reason="gcloud compute disks delete permanently destroys disk data",
        severity=Severity.CRITICAL,
        description="All data on disk is lost forever without snapshots.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-sql-delete",
        regex=r"gcloud\b.*?\bsql\s+instances\s+delete",
        reason="gcloud sql instances delete permanently destroys Cloud SQL instance",
        severity=Severity.CRITICAL,
        description="Database and all data deleted along with backups and read replicas.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-gsutil-rm-recursive",
        regex=r"gsutil\b.*?\brm\s+.*-r|gsutil\b.*?\brm\s+-[a-z]*r",
        reason="gsutil rm -r permanently deletes ALL objects in the GCS path",
        severity=Severity.CRITICAL,
        description="All objects under path deleted. No recovery without versioning.",
        suggestions=(
            PatternSuggestion("gsutil ls -r gs://bucket/path/",
                              "List objects first"),
            PatternSuggestion("gsutil versioning set on gs://bucket",
                              "Enable versioning for recovery"),
        ),
    ),
    DestructivePattern(
        name="gcp-gsutil-rb",
        regex=r"gsutil\b.*?\brb(?=\s|$)",
        reason="gsutil rb removes the entire GCS bucket",
        severity=Severity.CRITICAL,
        description="Bucket name becomes available to others. Bucket must be empty.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-gke-delete",
        regex=r"gcloud\b.*?\bcontainer\s+clusters\s+delete",
        reason="gcloud container clusters delete removes entire GKE cluster",
        severity=Severity.CRITICAL,
        description="All nodes and workloads terminated. Persistent volumes may be deleted.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-project-delete",
        regex=r"gcloud\b.*?\bprojects\s+delete",
        reason="gcloud projects delete removes the ENTIRE GCP project and ALL resources",
        severity=Severity.CRITICAL,
        description="ALL resources deleted: VMs, databases, storage, functions, IAM. "
        "30-day recovery window, then permanent.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-functions-delete",
        regex=r"gcloud\b.*?\bfunctions\s+delete",
        reason="gcloud functions delete removes Cloud Function",
        severity=Severity.HIGH,
        description="Function code, configuration, triggers, and event subscriptions removed.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-firestore-delete",
        regex=r"gcloud\b.*?\bfirestore\s+.*delete",
        reason="gcloud firestore delete removes Firestore documents and collections",
        severity=Severity.CRITICAL,
        description="Documents and collections deleted. No automatic backups by default.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-secrets-delete",
        regex=r"gcloud\b.*?\bsecrets\s+delete",
        reason="gcloud secrets delete destroys a Secret Manager secret",
        severity=Severity.CRITICAL,
        description="Secret and ALL versions permanently deleted. No recovery window.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-kms-keys-destroy",
        regex=r"gcloud\b.*?\bkms\s+keys\s+versions\s+destroy",
        reason="gcloud kms keys versions destroy schedules key version destruction",
        severity=Severity.CRITICAL,
        description="Data encrypted under this key version becomes unrecoverable.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-iam-service-accounts-delete",
        regex=r"gcloud\b.*?\biam\s+service-accounts\s+delete",
        reason="gcloud iam service-accounts delete removes a service account",
        severity=Severity.CRITICAL,
        description="Workloads using this SA lose access. Can undelete within 30 days.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-dns-managed-zones-delete",
        regex=r"gcloud\b.*?\bdns\s+managed-zones\s+delete",
        reason="gcloud dns managed-zones delete removes DNS zone — domains stop resolving",
        severity=Severity.CRITICAL,
        description="All record sets deleted. Production traffic can go dark.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-spanner-instances-delete",
        regex=r"gcloud\b.*?\bspanner\s+instances\s+delete",
        reason="gcloud spanner instances delete destroys Spanner instance and all data",
        severity=Severity.CRITICAL,
        description="All databases inside instance deleted. Unrecoverable without export.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-bigtable-instances-delete",
        regex=r"gcloud\b.*?\bbigtable\s+instances\s+delete",
        reason="gcloud bigtable instances delete destroys Bigtable instance and all data",
        severity=Severity.CRITICAL,
        description="All tables, clusters, and data permanently deleted.",
        suggestions=(),
    ),
    DestructivePattern(
        name="gcp-bq-rm-recursive",
        regex=r"\bbq\b.*?\brm\s+.*-r\b|\bbq\b.*?\brm\s+.*-f\b",
        reason="bq rm -r/-f removes BigQuery datasets, tables — data lost",
        severity=Severity.CRITICAL,
        description="bq rm -r removes dataset + ALL tables/views/models inside. "
        "bq rm -f removes table without confirmation.",
        suggestions=(),
    ),
    # ---- Azure / az CLI ----
    DestructivePattern(
        name="az-vm-delete",
        regex=r"az\b.*?\bvm\s+delete",
        reason="az vm delete permanently destroys virtual machines",
        severity=Severity.CRITICAL,
        description="VM deallocated and deleted. OS disk deleted unless --os-disk=detach.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-storage-delete",
        regex=r"az\b.*?\bstorage\s+account\s+delete",
        reason="az storage account delete destroys storage account and ALL data",
        severity=Severity.CRITICAL,
        description="ALL blobs, files, queues, tables deleted. Unrecoverable.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-blob-delete",
        regex=r"az\b.*?\bstorage\s+(?:blob|container)\s+delete",
        reason="az storage blob/container delete permanently removes data",
        severity=Severity.HIGH,
        description="Blob delete removes individual blobs. Container delete removes ALL blobs.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-sql-delete",
        regex=r"az\b.*?\bsql\s+(?:server|db)\s+delete",
        reason="az sql server/db delete permanently destroys the database",
        severity=Severity.CRITICAL,
        description="Server delete removes ALL databases. Database delete removes specific DB.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-group-delete",
        regex=r"az\b.*?\bgroup\s+delete",
        reason="az group delete removes entire resource group and ALL resources within it",
        severity=Severity.CRITICAL,
        description="ALL resources in the group deleted: VMs, storage, databases, networks.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-aks-delete",
        regex=r"az\b.*?\baks\s+delete",
        reason="az aks delete removes entire AKS Kubernetes cluster",
        severity=Severity.CRITICAL,
        description="All nodes, workloads, load balancers terminated. Node resource group deleted.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-webapp-delete",
        regex=r"az\b.*?\bwebapp\s+delete",
        reason="az webapp delete removes App Service",
        severity=Severity.HIGH,
        description="Application code, configuration, custom domains, and SSL certificates removed.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-cosmosdb-delete",
        regex=r"az\b.*?\bcosmosdb\s+(?:delete|database\s+delete|collection\s+delete)",
        reason="az cosmosdb delete destroys Cosmos DB resources and data",
        severity=Severity.CRITICAL,
        description="Account delete removes entire Cosmos DB. Database/collection delete removes data.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-keyvault-delete",
        regex=r"az\b.*?\bkeyvault\s+delete",
        reason="az keyvault delete removes Key Vault — secrets may be unrecoverable",
        severity=Severity.CRITICAL,
        description="All secrets, keys, certificates deleted. Soft delete allows recovery if enabled.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-acr-delete",
        regex=r"az\b.*?\bacr\s+delete",
        reason="az acr delete removes container registry and ALL images",
        severity=Severity.CRITICAL,
        description="ALL repositories and images deleted. Registry name becomes available.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-acr-repository-delete",
        regex=r"az\b.*?\bacr\s+repository\s+delete",
        reason="az acr repository delete permanently deletes repository and its images",
        severity=Severity.HIGH,
        description="All tags and images in the repository deleted. New pulls will fail.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-keyvault-item-delete-or-purge",
        regex=r"az\b.*?\bkeyvault\s+(?:key|secret|certificate|storage)\s+(?:delete|purge)",
        reason="Key Vault item delete/purge — purge bypasses soft-delete and is irreversible",
        severity=Severity.CRITICAL,
        description="Purge is PERMANENT. Applications/services bound to the item fail immediately.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-ad-sp-delete",
        regex=r"az\b.*?\bad\s+sp\s+delete",
        reason="az ad sp delete removes service principal — workloads using it lose auth",
        severity=Severity.CRITICAL,
        description="All workloads authenticating via this SP lose access. "
        "Can restore within 30 days via Graph API.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-ad-app-delete",
        regex=r"az\b.*?\bad\s+app\s+delete",
        reason="az ad app delete removes Azure AD app registration — all SPs break",
        severity=Severity.CRITICAL,
        description="All service principals derived from this app stop working. "
        "OAuth grants invalidated.",
        suggestions=(),
    ),
    DestructivePattern(
        name="az-network-dns-zone-delete",
        regex=r"az\b.*?\bnetwork\s+dns\s+zone\s+delete",
        reason="az network dns zone delete removes DNS zone — domains stop resolving",
        severity=Severity.CRITICAL,
        description="All record sets deleted. Production traffic goes dark.",
        suggestions=(),
    ),
)

_COMPILED_CLOUD_PATTERNS: list[tuple[re.Pattern, DestructivePattern]] = [
    (re.compile(p.regex, re.IGNORECASE), p) for p in CLOUD_DESTRUCTIVE_PATTERNS
]


# ---------------------------------------------------------------------------
# DCG-ported destructive database patterns (PostgreSQL, MySQL, SQLite,
# MongoDB, Redis)
# ---------------------------------------------------------------------------

DATABASE_DESTRUCTIVE_PATTERNS: tuple[DestructivePattern, ...] = (
    # ---- PostgreSQL (psql) ----
    DestructivePattern(
        name="psql-drop-database",
        regex=r"(?i)\bDROP\s+DATABASE\b",
        reason="DROP DATABASE permanently deletes the entire database",
        severity=Severity.CRITICAL,
        description="All schemas, tables, indexes, and data lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="psql-drop-table",
        regex=r"(?i)\bDROP\s+TABLE\b",
        reason="DROP TABLE permanently deletes the table and its data",
        severity=Severity.HIGH,
        description="Table definition and all rows lost. Related views/indexes affected.",
        suggestions=(),
    ),
    DestructivePattern(
        name="psql-drop-schema",
        regex=r"(?i)\bDROP\s+SCHEMA\b",
        reason="DROP SCHEMA deletes the schema and all objects within it",
        severity=Severity.CRITICAL,
        description="All tables, views, functions in the schema lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="psql-truncate-table",
        regex=r"(?i)\bTRUNCATE\s+(?:TABLE\s+)?(?!TABLE\b)[a-zA-Z_][a-zA-Z0-9_]*",
        reason="TRUNCATE removes ALL rows from the table irreversibly",
        severity=Severity.HIGH,
        description="All rows deleted. Cannot roll back in many contexts.",
        suggestions=(),
    ),
    DestructivePattern(
        name="psql-delete-without-where",
        regex=r"(?i)DELETE\s+FROM\s+(?:(?:[a-zA-Z_][a-zA-Z0-9_]*|\"[^\"]+\")(?:\.(?:[a-zA-Z_][a-zA-Z0-9_]*|\"[^\"]+\"))?)\s*(?:;|$)",
        reason="DELETE without WHERE clause removes ALL rows",
        severity=Severity.HIGH,
        description="All rows deleted if WHERE clause is missing.",
        suggestions=(),
    ),
    DestructivePattern(
        name="psql-dropdb-cli",
        regex=r"\bdropdb\s+",
        reason="dropdb CLI permanently deletes the entire database",
        severity=Severity.CRITICAL,
        description="The dropdb command-line tool destroys the database cluster-side.",
        suggestions=(),
    ),
    DestructivePattern(
        name="psql-dump-clean",
        regex=r"pg_dump\s+.*(?:--clean|-c\b)",
        reason="pg_dump --clean adds DROP statements to the dump script",
        severity=Severity.HIGH,
        description="Restoring the dump will DROP existing objects before recreating them.",
        suggestions=(),
    ),
    # ---- MySQL / MariaDB ----
    DestructivePattern(
        name="mysql-drop-database",
        regex=r"(?i)\bDROP\s+DATABASE\b",
        reason="DROP DATABASE permanently deletes the entire database",
        severity=Severity.CRITICAL,
        description="All tables and data within the database lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mysql-drop-table",
        regex=r"(?i)\bDROP\s+TABLE\b",
        reason="DROP TABLE permanently deletes the table",
        severity=Severity.HIGH,
        description="Table definition and all rows lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mysql-truncate-table",
        regex=r"(?i)\bTRUNCATE\s+(?:TABLE\s+)?(?!TABLE\b)[a-zA-Z_][a-zA-Z0-9_]*",
        reason="TRUNCATE removes ALL rows — cannot roll back in MySQL",
        severity=Severity.HIGH,
        description="InnoDB: all rows removed implicitly. No per-row delete triggers fired.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mysql-delete-without-where",
        regex=r"(?i)DELETE\s+FROM\s+(?:(?:[a-zA-Z_][a-zA-Z0-9_]*|`[^`]+`)(?:\.(?:[a-zA-Z_][a-zA-Z0-9_]*|`[^`]+`))?)\s*(?:;|$)",
        reason="DELETE without WHERE clause removes ALL rows",
        severity=Severity.HIGH,
        description="All rows deleted if WHERE clause is missing.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mysql-mysqladmin-drop",
        regex=r"mysqladmin\s+.*drop\b",
        reason="mysqladmin drop permanently deletes the entire database",
        severity=Severity.CRITICAL,
        description="mysqladmin drop destroys the database server-side without confirmation.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mysql-mysqldump-add-drop-database",
        regex=r"mysqldump\s+.*--add-drop-database",
        reason="mysqldump --add-drop-database adds DROP DATABASE to the dump",
        severity=Severity.HIGH,
        description="Restoring the dump will DROP the database first.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mysql-mysqldump-add-drop-table",
        regex=r"mysqldump\s+.*--add-drop-table",
        reason="mysqldump --add-drop-table adds DROP TABLE before CREATE TABLE",
        severity=Severity.MEDIUM,
        description="Existing tables will be dropped before restore.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mysql-grant-all",
        regex=r"(?i)GRANT\s+ALL\s+(?:PRIVILEGES\s+)?ON\s+\*\.\*",
        reason="GRANT ALL ON *.* gives unrestricted access to all databases",
        severity=Severity.HIGH,
        description="Full administrative access granted across all databases.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mysql-drop-user",
        regex=r"(?i)\bDROP\s+USER\b",
        reason="DROP USER permanently deletes the user account",
        severity=Severity.MEDIUM,
        description="User deleted. All privileges revoked. Existing connections may break.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mysql-reset-master",
        regex=r"(?i)\bRESET\s+MASTER\b",
        reason="RESET MASTER deletes all binary logs and resets binlog position",
        severity=Severity.CRITICAL,
        description="Breaks replication. All binlog history lost.",
        suggestions=(),
    ),
    # ---- SQLite (sqlite3) ----
    DestructivePattern(
        name="sqlite-drop-table",
        regex=r"(?i)\bDROP\s+TABLE\b",
        reason="DROP TABLE permanently deletes the table and all data",
        severity=Severity.CRITICAL,
        description="Table and all rows deleted from the database file.",
        suggestions=(),
    ),
    DestructivePattern(
        name="sqlite-delete-without-where",
        regex=r"(?i)DELETE\s+FROM\s+[a-zA-Z_][a-zA-Z0-9_]*\s*(?:;|$)",
        reason="DELETE without WHERE clause removes ALL rows",
        severity=Severity.CRITICAL,
        description="SQLite does not support TRUNCATE. DELETE without WHERE removes all rows.",
        suggestions=(),
    ),
    DestructivePattern(
        name="sqlite-vacuum-into",
        regex=r"(?i)VACUUM\s+INTO\s+",
        reason="VACUUM INTO overwrites the target file if it exists",
        severity=Severity.MEDIUM,
        description="Target file is overwritten without warning.",
        suggestions=(),
    ),
    DestructivePattern(
        name="sqlite-sqlite3-file-input",
        regex=r"sqlite3\s+[^\s]+\s+<\s+",
        reason="SQL loaded from file may contain destructive commands",
        severity=Severity.HIGH,
        description="Read SQL from file command — file contents not inspected by guard.",
        suggestions=(),
    ),
    # ---- MongoDB (mongosh) ----
    DestructivePattern(
        name="mongodb-drop-database",
        regex=r"\.dropDatabase\s*\(",
        reason="dropDatabase() permanently deletes the entire database",
        severity=Severity.CRITICAL,
        description="All collections, indexes, and data lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mongodb-drop-collection",
        regex=r"\.drop\s*\(\s*\)|\.dropCollection\s*\(",
        reason="drop()/dropCollection() permanently deletes the collection",
        severity=Severity.HIGH,
        description="All documents and indexes in the collection lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mongodb-delete-all",
        regex=r"\.(?:remove|deleteMany)\s*\(\s*\{\s*\}\s*\)",
        reason="remove({})/deleteMany({}) removes ALL documents",
        severity=Severity.HIGH,
        description="All documents in the collection deleted.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mongodb-mongorestore-drop",
        regex=r"mongorestore\s+.*--drop",
        reason="mongorestore --drop drops existing collections before restoring",
        severity=Severity.HIGH,
        description="Existing collections are dropped before data restoration.",
        suggestions=(),
    ),
    # ---- Redis (redis-cli) ----
    DestructivePattern(
        name="redis-flushall",
        regex=r"(?i)\bFLUSHALL\b",
        reason="FLUSHALL deletes ALL keys in ALL databases",
        severity=Severity.CRITICAL,
        description="Every key in every database is deleted immediately.",
        suggestions=(),
    ),
    DestructivePattern(
        name="redis-flushdb",
        regex=r"(?i)\bFLUSHDB\b",
        reason="FLUSHDB deletes ALL keys in the current database",
        severity=Severity.HIGH,
        description="All keys in the selected database deleted.",
        suggestions=(),
    ),
    DestructivePattern(
        name="redis-mass-delete-pipeline",
        regex=r"(?i)\bredis-cli\b.*\b(?:KEYS\b|--scan\b|SCAN\b).*\|\s*xargs\s+(?:-\S+(?:\s+\S+)?\s+)*redis-cli(?:\s+\S+)*\s+(?:DEL|UNLINK)\b",
        reason="KEYS/SCAN piped through xargs to DEL/UNLINK mass-deletes many keys",
        severity=Severity.HIGH,
        description="Mass key deletion via pipe. Can affect many keys at once.",
        suggestions=(),
    ),
    DestructivePattern(
        name="redis-debug-crash",
        regex=r"(?i)\bDEBUG\s+(?:SEGFAULT|CRASH)\b",
        reason="DEBUG SEGFAULT/CRASH crashes the Redis server",
        severity=Severity.CRITICAL,
        description="Redis server process crashes. Data loss may occur.",
        suggestions=(),
    ),
    DestructivePattern(
        name="redis-debug-sleep",
        regex=r"(?i)\bDEBUG\s+SLEEP\b",
        reason="DEBUG SLEEP blocks the Redis server",
        severity=Severity.HIGH,
        description="Redis blocked for N seconds. All clients time out.",
        suggestions=(),
    ),
    DestructivePattern(
        name="redis-shutdown",
        regex=r"(?i)\bSHUTDOWN\b",
        reason="SHUTDOWN stops the Redis server (SHUTDOWN NOSAVE loses data)",
        severity=Severity.HIGH,
        description="Redis server shut down gracefully (or with NOSAVE, losing data).",
        suggestions=(),
    ),
    DestructivePattern(
        name="redis-config-dangerous",
        regex=r"(?i)\bCONFIG\s+SET\s+(?:dir|dbfilename|slaveof|replicaof)\b",
        reason="CONFIG SET dir/dbfilename/slaveof can enable RCE or data exfiltration",
        severity=Severity.CRITICAL,
        description="Changing dir+dbfilename writes key data outside data dir. "
        "slaveof/replicaof can leak keys to attacker.",
        suggestions=(),
    ),
    DestructivePattern(
        name="redis-config-set-maxmemory",
        regex=r"(?i)\bCONFIG\s+SET\s+maxmemory\b(?:\s|$)",
        reason="CONFIG SET maxmemory can trigger mass key eviction",
        severity=Severity.CRITICAL,
        description="Setting maxmemory too low causes Redis to evict keys aggressively.",
        suggestions=(),
    ),
    DestructivePattern(
        name="redis-config-set-maxmemory-policy",
        regex=r"(?i)\bCONFIG\s+SET\s+maxmemory-policy\b",
        reason="CONFIG SET maxmemory-policy changes eviction policy — risk of data loss",
        severity=Severity.CRITICAL,
        description="Changing to allkeys-lru or volatile-ttl can evict any key.",
        suggestions=(),
    ),
    DestructivePattern(
        name="redis-config-set-save",
        regex=r"(?i)\bCONFIG\s+SET\s+save\b",
        reason="CONFIG SET save can disable RDB persistence entirely",
        severity=Severity.HIGH,
        description="Setting save to empty disables snapshots. Data lost on restart.",
        suggestions=(),
    ),
    DestructivePattern(
        name="redis-config-set-appendonly",
        regex=r"(?i)\bCONFIG\s+SET\s+appendonly\b",
        reason="CONFIG SET appendonly can disable AOF persistence",
        severity=Severity.HIGH,
        description="Disabling AOF removes append-only log. Data may be lost on restart.",
        suggestions=(),
    ),
    DestructivePattern(
        name="redis-config-rewrite",
        regex=r"(?i)\bCONFIG\s+REWRITE\b",
        reason="CONFIG REWRITE saves runtime changes to redis.conf permanently",
        severity=Severity.HIGH,
        description="Runtime CONFIG SET changes are persisted to disk.",
        suggestions=(),
    ),
)

_COMPILED_DATABASE_PATTERNS: list[tuple[re.Pattern, DestructivePattern]] = [
    (re.compile(p.regex, re.IGNORECASE), p) for p in DATABASE_DESTRUCTIVE_PATTERNS
]


# ---------------------------------------------------------------------------
# DCG-ported destructive git patterns (strict-git pack)
# ---------------------------------------------------------------------------

GIT_DESTRUCTIVE_PATTERNS: tuple[DestructivePattern, ...] = (
    DestructivePattern(
        name="git-push-force",
        regex=r"git\b.*?\bpush(?:[^\n;]*\s(?:--force(?:=\S*)?|--force-with-lease(?:=\S*)?|-f)(?=\s|$)|(?:\s+\S+)*\s+(?:\$?[\"']|\\)*\+\S+)",
        reason="Force push rewrites remote history",
        severity=Severity.CRITICAL,
        description="All forms of force push (--force, --force-with-lease, +refspec) "
        "rewrite remote history and may cause data loss for collaborators.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-push-mirror",
        regex=r"git\b.*?\bpush\b[^\n;]*(?:^|\s)--mirror(?:=\S*)?(?=\s|$)",
        reason="git push --mirror force-updates and deletes remote refs",
        severity=Severity.CRITICAL,
        description="Mirror push deletes remote refs absent locally. Extremely destructive.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-push-dynamic-arg",
        regex=r"git\b.*?\bpush\b[^\n;]*(?:\\|\$|`|\*|\?|\{|\}|\[)",
        reason="Shell-expanded push argument cannot be verified as non-forcing",
        severity=Severity.HIGH,
        description="Variables, globs, or escaped args in git push may expand to destructive refspecs.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-rebase",
        regex=r"git\b.*?\brebase\b",
        reason="git rebase rewrites commit history",
        severity=Severity.HIGH,
        description="Rebase rewrites commits. Force push needed afterward. Conflicts may lose changes.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-commit-amend",
        regex=r"git\b.*?\bcommit\s+.*--amend",
        reason="git commit --amend rewrites the last commit",
        severity=Severity.HIGH,
        description="Amending a pushed commit rewrites history. Previous commit is lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-filter-branch",
        regex=r"git\b.*?\bfilter-branch\b",
        reason="git filter-branch rewrites entire repository history",
        severity=Severity.CRITICAL,
        description="Rewrites ALL commits. Extremely dangerous. Use filter-repo instead.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-filter-repo",
        regex=r"git\b.*?\bfilter-repo\b",
        reason="git filter-repo rewrites repository history",
        severity=Severity.CRITICAL,
        description="Modern replacement for filter-branch. Still rewrites all history.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-cherry-pick",
        regex=r"git\b.*?\bcherry-pick\b",
        reason="git cherry-pick can introduce duplicate commits",
        severity=Severity.MEDIUM,
        description="Cherry-pick creates duplicate commits. Can cause merge conflicts later.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-reflog-expire",
        regex=r"git\b.*?\breflog\s+expire",
        reason="git reflog expire removes recovery entries",
        severity=Severity.HIGH,
        description="Reflog is the last resort for recovery. Expiring entries may lose data permanently.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-gc-aggressive",
        regex=r"git\b.*?\bgc\s+.*--(?:aggressive|prune)",
        reason="git gc with aggressive/prune removes recoverable objects",
        severity=Severity.HIGH,
        description="Prunes loose objects. Reflog entries and stashed changes may be lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-worktree-remove",
        regex=r"git\b.*?\bworktree\s+remove",
        reason="git worktree remove deletes a linked working tree",
        severity=Severity.HIGH,
        description="Uncommitted changes in the worktree are lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-submodule-deinit",
        regex=r"git\b.*?\bsubmodule\s+deinit",
        reason="git submodule deinit removes submodule configuration",
        severity=Severity.MEDIUM,
        description="Submodule working tree is removed. Clone again to restore.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-add-all-dot",
        regex=r"git\b.*?\badd\s+['\"]?\.['\"]?(?:\s|$)",
        reason="git add . stages everything including secrets, .env, build artifacts",
        severity=Severity.MEDIUM,
        description="All changes in the repo are staged. Secrets or build artifacts may be committed.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-add-all-flag",
        regex=r"git\b.*?\badd\s+(?:-A|--all)\b",
        reason="git add -A/--all stages all changes including secrets",
        severity=Severity.MEDIUM,
        description="Tracks new and modified files. Secrets may be unintentionally staged.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-push-to-master",
        regex=r"git\s+(?:\S+\s+)*push\s+(?:.*[\s:/])?\+?master(?:\s|$)",
        reason="Direct push to master/main branch is blocked",
        severity=Severity.MEDIUM,
        description="Push to default branch may bypass review. Use a feature branch and PR.",
        suggestions=(),
    ),
    DestructivePattern(
        name="git-push-to-main",
        regex=r"git\s+(?:\S+\s+)*push\s+(?:.*[\s:/])?\+?main(?:\s|$)",
        reason="Direct push to main branch is blocked",
        severity=Severity.MEDIUM,
        description="Push to default branch may bypass review. Use a feature branch and PR.",
        suggestions=(),
    ),
)

_COMPILED_GIT_PATTERNS: list[tuple[re.Pattern, DestructivePattern]] = [
    (re.compile(p.regex, re.IGNORECASE), p) for p in GIT_DESTRUCTIVE_PATTERNS
]


# ---------------------------------------------------------------------------
# DCG-style firewall destructive patterns (iptables, ufw, nftables)
# ---------------------------------------------------------------------------

FIREWALL_DESTRUCTIVE_PATTERNS: tuple[DestructivePattern, ...] = (
    DestructivePattern(
        name="iptables-flush",
        regex=r"\biptables\b.*(?:\s-F\b|\s+--flush\b)",
        reason="iptables --flush removes all firewall rules",
        severity=Severity.CRITICAL,
        description="Flushes all iptables rules. Network access will be lost immediately.",
        suggestions=(),
    ),
    DestructivePattern(
        name="iptables-policy-drop",
        regex=r"\biptables\b.*\s-P\s+(?:INPUT|FORWARD|OUTPUT)\s+DROP\b",
        reason="Setting default policy to DROP disconnects all traffic",
        severity=Severity.CRITICAL,
        description="Changes the default policy to DROP. SSH session will be terminated.",
        suggestions=(),
    ),
    DestructivePattern(
        name="iptables-delete-chains",
        regex=r"\biptables\b.*\s-X\b",
        reason="Deleting custom iptables chains removes security rules",
        severity=Severity.HIGH,
        description="Removes all user-defined chains. Security rules and jump rules are lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="iptables-restore",
        regex=r"\biptables-restore\b",
        reason="iptables-restore replaces the entire ruleset at once",
        severity=Severity.HIGH,
        description="Replaces all iptables rules. A malformed ruleset disconnects the server.",
        suggestions=(),
    ),
    DestructivePattern(
        name="ip6tables-flush",
        regex=r"\bip6tables\b.*(?:\s-F\b|\s+--flush\b)",
        reason="ip6tables --flush removes all IPv6 firewall rules",
        severity=Severity.CRITICAL,
        description="Flushes all ip6tables rules. IPv6 network access will be lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="iptables-insert-reject",
        regex=r"\biptables\b.*\s-[IA]\s+\S+(?:\s+\d+)?\s+-j\s+(?:DROP|REJECT)\b",
        reason="Inserting a DROP/REJECT rule locks out matching traffic",
        severity=Severity.HIGH,
        description="A DROP/REJECT rule at the top of INPUT chain blocks SSH if rule matches.",
        suggestions=(),
    ),
    DestructivePattern(
        name="ufw-disable",
        regex=r"\bufw\b.*\bdisable\b",
        reason="Disabling ufw removes all firewall protection",
        severity=Severity.CRITICAL,
        description="ufw is disabled. No firewall rules are enforced.",
        suggestions=(),
    ),
    DestructivePattern(
        name="ufw-reset",
        regex=r"\bufw\b.*\breset\b",
        reason="Resetting ufw deletes all custom rules",
        severity=Severity.HIGH,
        description="All ufw rules are deleted and firewall is disabled.",
        suggestions=(),
    ),
    DestructivePattern(
        name="ufw-default-deny",
        regex=r"\bufw\b.*\bdefault\s+deny\b",
        reason="Setting ufw default to deny blocks all incoming traffic",
        severity=Severity.HIGH,
        description="Changes the default ufw policy to deny. SSH access may be lost.",
        suggestions=(),
    ),
    DestructivePattern(
        name="ufw-delete",
        regex=r"\bufw\b.*\bdelete\b",
        reason="Deleting ufw rules may remove SSH access rules",
        severity=Severity.MEDIUM,
        description="Deletes a ufw rule. Removing the wrong rule may lock out SSH.",
        suggestions=(),
    ),
    DestructivePattern(
        name="nft-flush-ruleset",
        regex=r"\bnft\s+flush\s+ruleset\b",
        reason="nft flush ruleset removes all nftables rules",
        severity=Severity.CRITICAL,
        description="Flushes the entire nftables ruleset. All firewall rules are removed at once.",
        suggestions=(),
    ),
    DestructivePattern(
        name="nft-delete-table",
        regex=r"\bnft\s+delete\s+table\b",
        reason="Deleting an nftables table removes all chains and rules",
        severity=Severity.HIGH,
        description="Deletes an entire nftables table. All chains, rules, and sets are removed.",
        suggestions=(),
    ),
    DestructivePattern(
        name="nft-load-stdin",
        regex=r"\bnft\b\s+-[fF]\s+(?:/dev/stdin|-)(?:\s|$)",
        reason="Loading nftables rules from stdin can inject destructive rules",
        severity=Severity.HIGH,
        description="Loading rules from stdin or a pipe is risky. A malformed ruleset disconnects the server.",
        suggestions=(),
    ),
)

_COMPILED_FIREWALL_PATTERNS: list[tuple[re.Pattern, DestructivePattern]] = [
    (re.compile(p.regex, re.IGNORECASE), p) for p in FIREWALL_DESTRUCTIVE_PATTERNS
]


# ---------------------------------------------------------------------------
# DCG-ported loadbalancer destructive patterns (nginx, haproxy, traefik, ELB)
# ---------------------------------------------------------------------------

LOADBALANCER_DESTRUCTIVE_PATTERNS: tuple[DestructivePattern, ...] = (
    # nginx
    DestructivePattern(
        name="nginx-stop",
        regex=r"nginx\s+-s\s+stop\b",
        reason="nginx -s stop shuts down nginx and stops the load balancer.",
        severity=Severity.HIGH,
        description="Sending the stop signal terminates nginx immediately. All in-flight requests are dropped.",
        suggestions=(),
    ),
    DestructivePattern(
        name="nginx-quit",
        regex=r"nginx\s+-s\s+quit\b",
        reason="nginx -s quit gracefully stops nginx and halts traffic handling.",
        severity=Severity.HIGH,
        description="The quit signal stops accepting new connections. No new traffic is routed.",
        suggestions=(),
    ),
    DestructivePattern(
        name="systemctl-stop-nginx",
        regex=r"systemctl\b.*?\s+stop\s+nginx(?:\.service)?\b",
        reason="systemctl stop nginx stops the nginx service and disrupts traffic.",
        severity=Severity.HIGH,
        description="Stopping nginx via systemctl shuts down all worker processes.",
        suggestions=(),
    ),
    DestructivePattern(
        name="service-stop-nginx",
        regex=r"service\s+nginx\s+stop\b",
        reason="service nginx stop stops the nginx service and disrupts traffic.",
        severity=Severity.HIGH,
        description="Stopping nginx via the service command terminates all worker processes.",
        suggestions=(),
    ),
    DestructivePattern(
        name="nginx-config-delete",
        regex=r"\brm\b.*\s+['\"]?/etc/nginx(?:/|\b)",
        reason="Removing files from /etc/nginx deletes nginx configuration.",
        severity=Severity.CRITICAL,
        description="Deleting nginx config removes site definitions, upstream blocks, and SSL references.",
        suggestions=(),
    ),
    # haproxy
    DestructivePattern(
        name="haproxy-soft-stop",
        regex=r"\bhaproxy\s+.*-sf\b",
        reason="haproxy -sf sends a soft stop signal to the load balancer.",
        severity=Severity.HIGH,
        description="Soft-stop gracefully finishes current connections before shutting down.",
        suggestions=(),
    ),
    DestructivePattern(
        name="haproxy-hard-stop",
        regex=r"\bhaproxy\s+.*-st\b",
        reason="haproxy -st sends a hard stop signal, immediately terminating the load balancer.",
        severity=Severity.HIGH,
        description="Hard-stop kills HAProxy immediately. Active connections are dropped.",
        suggestions=(),
    ),
    DestructivePattern(
        name="haproxy-systemctl-stop",
        regex=r"systemctl\b.*?\s+stop\s+haproxy(?:\.service)?\b",
        reason="systemctl stop haproxy stops the HAProxy service.",
        severity=Severity.HIGH,
        description="Stopping HAProxy via systemctl terminates all proxy processes.",
        suggestions=(),
    ),
    DestructivePattern(
        name="haproxy-service-stop",
        regex=r"service\s+haproxy\s+stop\b",
        reason="service haproxy stop stops the HAProxy service.",
        severity=Severity.HIGH,
        description="Stopping HAProxy via service command terminates all proxy processes.",
        suggestions=(),
    ),
    DestructivePattern(
        name="haproxy-socat-disable-server",
        regex=r"(?:echo|printf)\s+['\"]?disable\s+server\b.*\|\s*socat\b",
        reason="Disabling a server via HAProxy runtime API removes it from the pool.",
        severity=Severity.HIGH,
        description="Disabling a server via socat removes it from the load balancer pool immediately.",
        suggestions=(),
    ),
    DestructivePattern(
        name="haproxy-socat-shutdown-sessions",
        regex=r"(?:echo|printf)\s+['\"]?shutdown\s+sessions\b.*\|\s*socat\b",
        reason="Shutting down sessions via HAProxy runtime API terminates active connections.",
        severity=Severity.HIGH,
        description="Shutting down sessions terminates all active connections to the backend.",
        suggestions=(),
    ),
    DestructivePattern(
        name="haproxy-socat-disable-frontend",
        regex=r"(?:echo|printf)\s+['\"]?disable\s+frontend\b.*\|\s*socat\b",
        reason="Disabling a frontend via HAProxy runtime API stops accepting new connections.",
        severity=Severity.HIGH,
        description="Disabling a frontend immediately stops accepting new connections.",
        suggestions=(),
    ),
    DestructivePattern(
        name="haproxy-socat-shutdown-frontend",
        regex=r"(?:echo|printf)\s+['\"]?shutdown\s+frontend\b.*\|\s*socat\b",
        reason="Shutting down a frontend via HAProxy runtime API terminates it immediately.",
        severity=Severity.HIGH,
        description="Shutting down a frontend terminates the frontend and all its connections.",
        suggestions=(),
    ),
    DestructivePattern(
        name="haproxy-config-delete",
        regex=r"\brm\b.*\s+['\"]?/etc/haproxy(?:/|\b)",
        reason="Removing files from /etc/haproxy deletes HAProxy configuration.",
        severity=Severity.HIGH,
        description="Deleting HAProxy config removes backend definitions and frontend configurations.",
        suggestions=(),
    ),
    # traefik
    DestructivePattern(
        name="traefik-docker-stop",
        regex=r"docker\b.*?\s+(?:stop|kill)\s+.*\btraefik\b",
        reason="Stopping the Traefik container halts all traffic routing.",
        severity=Severity.CRITICAL,
        description="Stopping or killing the Traefik container immediately halts all traffic routing.",
        suggestions=(),
    ),
    DestructivePattern(
        name="traefik-docker-rm",
        regex=r"docker\b.*?\s+rm\s+.*\btraefik\b",
        reason="Removing the Traefik container destroys the load balancer.",
        severity=Severity.CRITICAL,
        description="Removing the Traefik container deletes it entirely, including runtime state.",
        suggestions=(),
    ),
    DestructivePattern(
        name="traefik-compose-down",
        regex=r"docker[\s-]compose\s+.*\bdown\b.*\btraefik\b",
        reason="docker-compose down on Traefik stops and removes the load balancer.",
        severity=Severity.CRITICAL,
        description="docker-compose down stops and removes Traefik containers and networks.",
        suggestions=(),
    ),
    DestructivePattern(
        name="traefik-kubectl-delete-pod",
        regex=r"kubectl\b.*?\s+delete\s+(?:pod|deployment|daemonset)\s+.*\btraefik\b",
        reason="Deleting Traefik pods/deployments disrupts traffic routing.",
        severity=Severity.CRITICAL,
        description="Deleting Traefik pods or deployments removes the load balancer from the cluster.",
        suggestions=(),
    ),
    DestructivePattern(
        name="traefik-kubectl-delete-ingressroute",
        regex=r"kubectl\b.*?\s+delete\s+ingressroute\b",
        reason="Deleting IngressRoute CRDs removes Traefik routing rules.",
        severity=Severity.HIGH,
        description="Deleting IngressRoute CRDs removes routing rules, making services unreachable.",
        suggestions=(),
    ),
    DestructivePattern(
        name="traefik-config-delete",
        regex=r"\brm\b.*\btraefik\b.*\.(?:ya?ml|toml)\b",
        reason="Removing Traefik config files disrupts load balancer configuration.",
        severity=Severity.CRITICAL,
        description="Deleting Traefik config removes entrypoints, middleware, and provider settings.",
        suggestions=(),
    ),
    DestructivePattern(
        name="traefik-api-delete",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))DELETE\b)(?=.*\btraefik\b.*\b/api/).*",
        reason="DELETE operations against Traefik API can remove routing configuration.",
        severity=Severity.HIGH,
        description="Sending DELETE to Traefik API removes routers, services, or middleware.",
        suggestions=(),
    ),
    DestructivePattern(
        name="traefik-systemctl-stop",
        regex=r"systemctl\b.*?\s+stop\s+traefik(?:\.service)?\b",
        reason="systemctl stop traefik stops the Traefik service.",
        severity=Severity.HIGH,
        description="Stopping Traefik via systemctl shuts down the load balancer process.",
        suggestions=(),
    ),
    DestructivePattern(
        name="traefik-service-stop",
        regex=r"service\s+traefik\s+stop\b",
        reason="service traefik stop stops the Traefik service.",
        severity=Severity.HIGH,
        description="Stopping Traefik via service command terminates the load balancer.",
        suggestions=(),
    ),
    # AWS ELB
    DestructivePattern(
        name="elbv2-delete-load-balancer",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elbv2\s+delete-load-balancer\b",
        reason="aws elbv2 delete-load-balancer permanently deletes the load balancer.",
        severity=Severity.HIGH,
        description="Deletes an ALB or NLB. All traffic to that load balancer stops immediately.",
        suggestions=(),
    ),
    DestructivePattern(
        name="elbv2-delete-target-group",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elbv2\s+delete-target-group\b",
        reason="aws elbv2 delete-target-group permanently deletes the target group.",
        severity=Severity.HIGH,
        description="Deletes an ELBv2 target group. Instances in the group become unreachable.",
        suggestions=(),
    ),
    DestructivePattern(
        name="elbv2-deregister-targets",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elbv2\s+deregister-targets\b",
        reason="aws elbv2 deregister-targets removes targets from the load balancer.",
        severity=Severity.HIGH,
        description="Deregisters targets from an ALB/NLB target group. Live traffic is disrupted.",
        suggestions=(),
    ),
    DestructivePattern(
        name="elbv2-delete-listener",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elbv2\s+delete-listener\b",
        reason="aws elbv2 delete-listener deletes a listener, potentially breaking traffic routing.",
        severity=Severity.HIGH,
        description="Deletes a listener. All rules in the listener are removed.",
        suggestions=(),
    ),
    DestructivePattern(
        name="elbv2-delete-rule",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elbv2\s+delete-rule\b",
        reason="aws elbv2 delete-rule deletes a listener rule, potentially breaking routing.",
        severity=Severity.HIGH,
        description="Deletes a listener rule. Associated routing logic is removed.",
        suggestions=(),
    ),
    DestructivePattern(
        name="elb-delete-load-balancer",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elb\s+delete-load-balancer\b",
        reason="aws elb delete-load-balancer permanently deletes the classic load balancer.",
        severity=Severity.HIGH,
        description="Deletes a Classic ELB. All traffic stops immediately.",
        suggestions=(),
    ),
    DestructivePattern(
        name="elb-deregister-instances",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elb\s+deregister-instances-from-load-balancer\b",
        reason="aws elb deregister-instances-from-load-balancer removes instances from the load balancer.",
        severity=Severity.HIGH,
        description="Deregisters EC2 instances from a Classic ELB. Live traffic is disrupted.",
        suggestions=(),
    ),
)

_COMPILED_LOADBALANCER_PATTERNS: list[tuple[re.Pattern, DestructivePattern]] = [
    (re.compile(p.regex, re.IGNORECASE), p) for p in LOADBALANCER_DESTRUCTIVE_PATTERNS
]


# ---------------------------------------------------------------------------
# DCG-ported system destructive patterns — disk (39), permissions (7),
# services (8). Total: 54 patterns.
# ---------------------------------------------------------------------------

SYSTEM_DESTRUCTIVE_PATTERNS: tuple[DestructivePattern, ...] = (
    # ---- disk: dd ----
    DestructivePattern(
        name="dd-device",
        regex=r"dd\s+.*of=['\"]?/dev/",
        reason="dd to a block device will OVERWRITE all data on that device.",
        severity=Severity.HIGH,
        description="Dangerous! dd to /dev/* block device overwrites data.",
        suggestions=(),
    ),
    DestructivePattern(
        name="dd-wipe",
        regex=r"dd\s+.*if=['\"]?/dev/(?:zero|urandom|random).*of=['\"]?/dev/",
        reason="dd from /dev/zero or /dev/urandom to a device will WIPE all data!",
        severity=Severity.HIGH,
        description="dd wipe from /dev/zero|urandom|random to block device.",
        suggestions=(),
    ),
    # ---- disk: partition tools ----
    DestructivePattern(
        name="fdisk-edit",
        regex=r"fdisk\s+['\"]?/dev/(?!.*-l)",
        reason="fdisk can modify partition tables and cause data loss.",
        severity=Severity.HIGH,
        description="fdisk edits partition tables on /dev/*.",
        suggestions=(),
    ),
    DestructivePattern(
        name="parted-modify",
        regex=r"parted\b[^\n;&|]*?['\"]?/dev/\S+['\"]?(?:\s+--)?\s+(?:(?!\s*(?:align-check|help|h|print|p|quit|q|select|unit|u)\b)|[^\n;&|]*\b(?:print|p)\b\s+(?:(?:devices|free|list|all|\d+)\s+\S+|(?!devices\b|free\b|list\b|all\b|\d+\b)\S+)|[^\n;&|]*\b(?:disk_set|disk_toggle|mklabel|mktable|mkpart|name|rescue|resizepart|rm|set|toggle|type)\b)",
        reason="parted can modify partition tables and cause data loss.",
        severity=Severity.HIGH,
        description="parted modifies partition tables.",
        suggestions=(),
    ),
    # ---- disk: filesystem creation/destruction ----
    DestructivePattern(
        name="mkfs",
        regex=r"mkfs(?:\.[a-z0-9]+)?\s+",
        reason="mkfs formats a partition/device and ERASES all existing data.",
        severity=Severity.HIGH,
        description="mkfs creates a filesystem, erasing existing data.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mkswap",
        regex=r"mkswap\s+",
        reason="mkswap formats a partition as swap, ERASING any existing data.",
        severity=Severity.HIGH,
        description="mkswap creates swap area, overwriting existing data.",
        suggestions=(),
    ),
    DestructivePattern(
        name="wipefs",
        regex=r"wipefs\s+",
        reason="wipefs removes filesystem signatures.",
        severity=Severity.HIGH,
        description="wipefs erases filesystem signatures from a device.",
        suggestions=(),
    ),
    # ---- disk: mount/umount ----
    DestructivePattern(
        name="mount-bind-root",
        regex=r"mount\s+.*--bind\s+.*\s+['\"]?/(?:$|[^a-z])",
        reason="mount --bind to root directory can have system-wide effects.",
        severity=Severity.HIGH,
        description="Mount --bind to / may cause system issues.",
        suggestions=(),
    ),
    DestructivePattern(
        name="umount-force",
        regex=r"umount\s+.*-[a-z]*f",
        reason="umount -f may cause data loss if device is in use.",
        severity=Severity.HIGH,
        description="Force unmount can cause data loss.",
        suggestions=(),
    ),
    DestructivePattern(
        name="losetup-device",
        regex=r"losetup\s+['\"]?/dev/loop",
        reason="losetup modifies loop device associations.",
        severity=Severity.HIGH,
        description="losetup on /dev/loop changes device mapping.",
        suggestions=(),
    ),
    # ---- disk: mdadm RAID ----
    DestructivePattern(
        name="mdadm-stop",
        regex=r"mdadm\s+(?:.*\s+)?(?:--stop|-S)\b",
        reason="mdadm --stop shuts down a RAID array.",
        severity=Severity.HIGH,
        description="Stops a RAID array. Data may become inaccessible.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mdadm-remove",
        regex=r"mdadm\s+(?:.*\s+)?--remove\b",
        reason="mdadm --remove removes a drive from a RAID array.",
        severity=Severity.HIGH,
        description="Removes device from RAID. Data loss if no redundancy.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mdadm-fail",
        regex=r"mdadm\s+(?:.*\s+)?(?:--fail|-f)\b",
        reason="mdadm --fail marks a device as failed.",
        severity=Severity.HIGH,
        description="Marks RAID device as failed.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mdadm-zero-superblock",
        regex=r"mdadm\s+(?:.*\s+)?--zero-superblock\b",
        reason="mdadm --zero-superblock erases RAID metadata.",
        severity=Severity.HIGH,
        description="Erases RAID superblock. Array cannot be reassembled.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mdadm-create",
        regex=r"mdadm\s+(?:.*\s+)?(?:--create|-C)\b",
        reason="mdadm --create creates a RAID array, erasing data on member devices.",
        severity=Severity.HIGH,
        description="Creates new RAID, erasing existing data on members.",
        suggestions=(),
    ),
    DestructivePattern(
        name="mdadm-grow",
        regex=r"mdadm\s+(?:.*\s+)?--grow\b",
        reason="mdadm --grow reshapes a RAID array. Interruption causes data loss.",
        severity=Severity.HIGH,
        description="Grows/reshapes RAID. Backup first.",
        suggestions=(),
    ),
    # ---- disk: btrfs ----
    DestructivePattern(
        name="btrfs-subvolume-delete",
        regex=r"btrfs\b.*?\s+subvolume\s+delete\b",
        reason="btrfs subvolume delete REMOVES a subvolume and its data.",
        severity=Severity.HIGH,
        description="Deletes a btrfs subvolume permanently.",
        suggestions=(),
    ),
    DestructivePattern(
        name="btrfs-device-remove",
        regex=r"btrfs\b.*?\s+device\s+(?:remove|delete)\b",
        reason="btrfs device remove redistributes data off a device.",
        severity=Severity.HIGH,
        description="Removes device from btrfs filesystem.",
        suggestions=(),
    ),
    DestructivePattern(
        name="btrfs-device-add",
        regex=r"btrfs\b.*?\s+device\s+add\b",
        reason="btrfs device add incorporates a device. Verify correctness.",
        severity=Severity.HIGH,
        description="Adds device to btrfs filesystem. Verify target.",
        suggestions=(),
    ),
    DestructivePattern(
        name="btrfs-balance",
        regex=r"btrfs\b.*?\s+balance\s+start\b",
        reason="btrfs balance redistributes data. Can be slow and disruptive.",
        severity=Severity.HIGH,
        description="Starts btrfs balance operation.",
        suggestions=(),
    ),
    DestructivePattern(
        name="btrfs-check-repair",
        regex=r"btrfs\b.*?\s+check\s+(?:.*\s+)?--repair\b",
        reason="btrfs check --repair is DANGEROUS. Can cause data loss.",
        severity=Severity.HIGH,
        description="btrfs check --repair modifies filesystem. Backup first!",
        suggestions=(),
    ),
    DestructivePattern(
        name="btrfs-rescue",
        regex=r"btrfs\b.*?\s+rescue\b",
        reason="btrfs rescue modifies filesystem metadata. Last resort only.",
        severity=Severity.HIGH,
        description="btrfs rescue operations modify metadata.",
        suggestions=(),
    ),
    DestructivePattern(
        name="btrfs-filesystem-resize",
        regex=r"btrfs\b.*?\s+filesystem\s+resize\b",
        reason="btrfs filesystem resize can shrink FS. Data loss if too small.",
        severity=Severity.HIGH,
        description="Resizes btrfs filesystem. Can cause data loss if shrinking.",
        suggestions=(),
    ),
    # ---- disk: dmsetup ----
    DestructivePattern(
        name="dmsetup-remove",
        regex=r"dmsetup\b.*?\s+remove\b",
        reason="dmsetup remove detaches a device-mapper device.",
        severity=Severity.HIGH,
        description="Removes a device-mapper device.",
        suggestions=(),
    ),
    DestructivePattern(
        name="dmsetup-remove-all",
        regex=r"dmsetup\b.*?\s+remove_all\b",
        reason="dmsetup remove_all REMOVES ALL device-mapper devices.",
        severity=Severity.HIGH,
        description="Removes ALL device-mapper devices. Extremely dangerous!",
        suggestions=(),
    ),
    DestructivePattern(
        name="dmsetup-wipe-table",
        regex=r"dmsetup\b.*?\s+wipe_table\b",
        reason="dmsetup wipe_table replaces table with error target.",
        severity=Severity.HIGH,
        description="Wipes device-mapper table. All I/O will fail.",
        suggestions=(),
    ),
    DestructivePattern(
        name="dmsetup-clear",
        regex=r"dmsetup\b.*?\s+clear\b",
        reason="dmsetup clear removes the mapping table from a device.",
        severity=Severity.HIGH,
        description="Clears device-mapper mapping table.",
        suggestions=(),
    ),
    DestructivePattern(
        name="dmsetup-load",
        regex=r"dmsetup\b.*?\s+load\b",
        reason="dmsetup load changes device mapping.",
        severity=Severity.HIGH,
        description="Loads new device-mapper table. Verify correctness.",
        suggestions=(),
    ),
    DestructivePattern(
        name="dmsetup-create",
        regex=r"dmsetup\b.*?\s+create\b",
        reason="dmsetup create sets up a new device-mapper device.",
        severity=Severity.HIGH,
        description="Creates a device-mapper device. Verify parameters.",
        suggestions=(),
    ),
    # ---- disk: nbd-client ----
    DestructivePattern(
        name="nbd-client-disconnect",
        regex=r"nbd-client\s+(?:.*\s+)?-d\b",
        reason="nbd-client -d disconnects a network block device.",
        severity=Severity.HIGH,
        description="Disconnects NBD device. Data loss if not unmounted.",
        suggestions=(),
    ),
    DestructivePattern(
        name="nbd-client-connect",
        regex=r"nbd-client\s+\S+\s+\d+\s+['\"]?/dev/nbd",
        reason="nbd-client connects a network block device.",
        severity=Severity.HIGH,
        description="Connects NBD device. Verify server and device target.",
        suggestions=(),
    ),
    # ---- disk: LVM ----
    DestructivePattern(
        name="pvremove",
        regex=r"\bpvremove\b",
        reason="pvremove ERASES LVM metadata from a physical volume.",
        severity=Severity.HIGH,
        description="Removes LVM PV. Data becomes inaccessible.",
        suggestions=(),
    ),
    DestructivePattern(
        name="vgremove",
        regex=r"\bvgremove\b",
        reason="vgremove DELETES a volume group and all LVs within it.",
        severity=Severity.HIGH,
        description="Removes LVM VG and all logical volumes.",
        suggestions=(),
    ),
    DestructivePattern(
        name="lvremove",
        regex=r"\blvremove\b",
        reason="lvremove PERMANENTLY deletes a logical volume and its data.",
        severity=Severity.HIGH,
        description="Deletes LVM LV and ALL data on it.",
        suggestions=(),
    ),
    DestructivePattern(
        name="vgreduce",
        regex=r"\bvgreduce\b",
        reason="vgreduce removes a PV from a VG. Data may be lost.",
        severity=Severity.HIGH,
        description="Reduces VG by removing a PV.",
        suggestions=(),
    ),
    DestructivePattern(
        name="lvreduce",
        regex=r"\blvreduce\b",
        reason="lvreduce SHRINKS a logical volume. Data loss possible!",
        severity=Severity.HIGH,
        description="Shrinks LV. Data loss if FS not resized first!",
        suggestions=(),
    ),
    DestructivePattern(
        name="lvresize-shrink",
        regex=r"lvresize\s+(?:.*\s+)?(?:-L\s*-|-l\s*-|--size\s+\S*-)",
        reason="lvresize with negative size SHRINKS the volume.",
        severity=Severity.HIGH,
        description="Shrinks LV via negative size. Resize FS first!",
        suggestions=(),
    ),
    DestructivePattern(
        name="pvmove",
        regex=r"\bpvmove\b",
        reason="pvmove migrates data between PVs. Do NOT interrupt!",
        severity=Severity.HIGH,
        description="Moves data between physical volumes. Interruption causes loss.",
        suggestions=(),
    ),
    DestructivePattern(
        name="lvconvert-merge",
        regex=r"lvconvert\s+(?:.*\s+)?--merge\b",
        reason="lvconvert --merge reverts LV to snapshot state.",
        severity=Severity.HIGH,
        description="Merges LV snapshot, discarding changes since snapshot.",
        suggestions=(),
    ),
    # ---- permissions ----
    DestructivePattern(
        name="chmod-777",
        regex=r"chmod\s+(?:.*\s+)?[\\\"'=]?0*777(?:[\\\s\"']|$)",
        reason="chmod 777 makes files world-writable.",
        severity=Severity.HIGH,
        description="chmod 777 grants read/write/execute to everyone.",
        suggestions=(),
    ),
    DestructivePattern(
        name="chmod-recursive-root",
        regex=r"chmod\s+(?:.*(?:-[rR]|--recursive)).*\s+['\"]?/(?:$|bin|boot|dev|etc|lib|lib64|opt|proc|root|run|sbin|srv|sys|usr|var)\b",
        reason="chmod -R on system directories can break system permissions.",
        severity=Severity.CRITICAL,
        description="Recursive chmod on system dirs can break the system.",
        suggestions=(),
    ),
    DestructivePattern(
        name="chown-recursive-root",
        regex=r"chown\s+(?:.*(?:-[rR]|--recursive)).*\s+['\"]?/(?:$|bin|boot|dev|etc|lib|lib64|opt|proc|root|run|sbin|srv|sys|usr|var)\b",
        reason="chown -R on system directories can break system ownership.",
        severity=Severity.HIGH,
        description="Recursive chown on system dirs can break services.",
        suggestions=(),
    ),
    DestructivePattern(
        name="chmod-setuid",
        regex=r"chmod\s+.*u\+s|chmod\s+[4-7]\d{3}",
        reason="Setting setuid bit (chmod u+s) is security-sensitive.",
        severity=Severity.HIGH,
        description="setuid allows running with owner privileges.",
        suggestions=(),
    ),
    DestructivePattern(
        name="chmod-setgid",
        regex=r"chmod\s+.*g\+s|chmod\s+[2367]\d{3}",
        reason="Setting setgid bit (chmod g+s) is security-sensitive.",
        severity=Severity.HIGH,
        description="setgid affects group privileges.",
        suggestions=(),
    ),
    DestructivePattern(
        name="chown-to-root",
        regex=r"chown\s+.*root[:\s]",
        reason="Changing ownership to root should be done carefully.",
        severity=Severity.HIGH,
        description="chown to root makes files inaccessible to normal users.",
        suggestions=(),
    ),
    DestructivePattern(
        name="setfacl-all",
        regex=r"setfacl\s+.*-[rR].*\s+['\"]?/(?:$|bin|boot|dev|etc|lib|lib64|opt|proc|root|run|sbin|srv|sys|usr|var)\b",
        reason="setfacl -R on system directories can modify access control across FS.",
        severity=Severity.CRITICAL,
        description="Recursive setfacl on system dirs breaks security boundaries.",
        suggestions=(),
    ),
    # ---- services ----
    DestructivePattern(
        name="systemctl-stop-critical",
        regex=r"systemctl\b.*?\s+(?:stop|disable|mask)\s+(?:ssh|sshd|network|networking|firewalld|ufw|docker|containerd)\b",
        reason="Stopping/disabling critical services can cause access loss or outage.",
        severity=Severity.HIGH,
        description="Stop critical service: ssh, network, firewall, container runtime.",
        suggestions=(),
    ),
    DestructivePattern(
        name="systemctl-stop",
        regex=r"systemctl\b.*?\s+(?:stop|disable|mask)\b",
        reason="systemctl stop/disable/mask affects service availability.",
        severity=Severity.HIGH,
        description="Stops, disables, or masks a systemd service.",
        suggestions=(),
    ),
    DestructivePattern(
        name="service-stop-critical",
        regex=r"service\s+(?:ssh|sshd|network|networking|docker)\s+stop",
        reason="Stopping critical services via service command can cause access loss.",
        severity=Severity.HIGH,
        description="Stops a critical service via SysV init.",
        suggestions=(),
    ),
    DestructivePattern(
        name="systemctl-isolate",
        regex=r"systemctl\b.*?\s+isolate\b",
        reason="systemctl isolate changes the system state significantly.",
        severity=Severity.HIGH,
        description="Isolates to a different systemd target.",
        suggestions=(),
    ),
    DestructivePattern(
        name="systemctl-power",
        regex=r"systemctl\b.*?\s+(?:poweroff|reboot|halt|suspend|hibernate)\b",
        reason="systemctl poweroff/reboot/halt shuts down or restarts the system.",
        severity=Severity.CRITICAL,
        description="System power state change: poweroff, reboot, halt, suspend.",
        suggestions=(),
    ),
    DestructivePattern(
        name="shutdown",
        regex=r"\bshutdown\b",
        reason="shutdown will power off or restart the system.",
        severity=Severity.CRITICAL,
        description="Shuts down or restarts the system.",
        suggestions=(),
    ),
    DestructivePattern(
        name="reboot",
        regex=r"\breboot\b",
        reason="reboot will restart the system.",
        severity=Severity.CRITICAL,
        description="Restarts the system immediately.",
        suggestions=(),
    ),
    DestructivePattern(
        name="init-level",
        regex=r"\binit\s+[06]\b",
        reason="init 0 shuts down, init 6 reboots the system.",
        severity=Severity.CRITICAL,
        description="Init runlevel change to 0 (halt) or 6 (reboot).",
        suggestions=(),
    ),
)

_COMPILED_SYSTEM_PATTERNS: list[tuple[re.Pattern, DestructivePattern]] = [
    (re.compile(p.regex, re.IGNORECASE), p) for p in SYSTEM_DESTRUCTIVE_PATTERNS
]


def _check_docker_destructive(command: str) -> DestructiveMatch | None:
    """Check a docker command against destructive patterns (ported from DCG)."""
    for compiled, pattern in _COMPILED_DOCKER_PATTERNS:
        if compiled.search(command):
            suggestion = pattern.suggestions[0].command if pattern.suggestions else None
            return DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=suggestion,
            )
    return None


def _check_compose_destructive(command: str) -> DestructiveMatch | None:
    """Check a docker-compose command against destructive patterns (ported from DCG)."""
    for compiled, pattern in _COMPILED_COMPOSE_PATTERNS:
        if compiled.search(command):
            suggestion = pattern.suggestions[0].command if pattern.suggestions else None
            return DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=suggestion,
            )
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

    Unlike ``_check_docker_destructive`` / ``_check_compose_destructive``
    which return only the FIRST match, this function iterates every pattern
    and collects every match — used by the scan tool.
    """
    findings: list[DestructiveMatch] = []

    for compiled, pattern in _COMPILED_DOCKER_PATTERNS:
        if compiled.search(command):
            findings.append(DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=pattern.description,
            ))

    for compiled, pattern in _COMPILED_COMPOSE_PATTERNS:
        if compiled.search(command):
            findings.append(DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=pattern.description,
            ))

    for compiled, pattern in _COMPILED_FILESYSTEM_PATTERNS:
        if compiled.search(command):
            findings.append(DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=pattern.description,
            ))

    for compiled, pattern in _COMPILED_KUBERNETES_PATTERNS:
        if compiled.search(command):
            findings.append(DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=pattern.description,
            ))

    for compiled, pattern in _COMPILED_CLOUD_PATTERNS:
        if compiled.search(command):
            findings.append(DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=pattern.description,
            ))

    for compiled, pattern in _COMPILED_DATABASE_PATTERNS:
        if compiled.search(command):
            findings.append(DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=pattern.description,
            ))

    for compiled, pattern in _COMPILED_GIT_PATTERNS:
        if compiled.search(command):
            findings.append(DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=pattern.description,
            ))

    for compiled, pattern in _COMPILED_FIREWALL_PATTERNS:
        if compiled.search(command):
            findings.append(DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=pattern.description,
            ))

    for compiled, pattern in _COMPILED_LOADBALANCER_PATTERNS:
        if compiled.search(command):
            findings.append(DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=pattern.description,
            ))

    for compiled, pattern in _COMPILED_SYSTEM_PATTERNS:
        if compiled.search(command):
            findings.append(DestructiveMatch(
                pattern_name=pattern.name,
                reason=pattern.reason,
                severity=pattern.severity,
                suggestion=pattern.description,
            ))

    return findings


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
    cmd = " ".join(effective)
    match = _check_docker_destructive(cmd)
    if match:
        msg = f"Destructive docker operation blocked: {match.reason}"
        if match.suggestion:
            msg += f" (safer: {match.suggestion})"
        return False, msg

    match = _check_compose_destructive(cmd)
    if match:
        msg = f"Destructive docker-compose operation blocked: {match.reason}"
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
