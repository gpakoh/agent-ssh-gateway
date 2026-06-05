"""Shared helper for conditional command output redaction."""

from app.config import settings


def should_redact_command_output(redact_output: bool | None) -> bool:
    """Return effective redaction toggle — request/query override or settings default."""
    if redact_output is not None:
        return redact_output
    return settings.command_output_redaction_enabled
