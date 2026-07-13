"""Read-only command policy for the experimental MCP gateway server."""

from __future__ import annotations

ALLOWED_COMMAND_PREFIXES: tuple[str, ...] = (
    "git rev-parse",
    "git status",
    "git log",
    "git diff",
    "git show",
    "git tag",
    "pytest -q",
    "ruff check",
    "mypy",
    "uv ",
    "command -v uv",
    "find ",
    "grep ",
    "sed -n",
    "cat ",
    "ls ",
    "pwd",
    "python -m compileall",
)

DENIED_COMMAND_PARTS: tuple[str, ...] = (
    " rm ",
    "rm -",
    "mv ",
    "chmod ",
    "chown ",
    "git push",
    "git reset",
    "git clean",
    "docker system prune",
    "docker compose down",
    "pip install",
    "apt install",
    "curl ",
    "wget ",
    ">",
    ">>",
    "|",
    ";",
    "&&",
)


class CommandPolicyError(ValueError):
    """Raised when a command is denied by the MCP example policy."""


def validate_readonly_command(command: str) -> str:
    """Validate a read-only command for the MCP gateway example."""
    stripped = command.strip()
    lowered = f" {stripped.lower()} "

    if not stripped:
        raise CommandPolicyError("Command must not be empty")

    for denied in DENIED_COMMAND_PARTS:
        if denied in lowered:
            raise CommandPolicyError(f"Command denied by policy: {denied.strip()}")

    if not stripped.startswith(ALLOWED_COMMAND_PREFIXES):
        raise CommandPolicyError("Command is not allowed by the MCP example allowlist")

    return stripped
