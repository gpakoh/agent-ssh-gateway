"""Confidence scoring v2 — span-aware pattern match confidence (P9).

Extends the basic pattern-level confidence with span context from P7:
- Where does the match fall in the command (executed code vs data/comment)?
- Are there execution operators near the match?
- Is the match at command position?

Design follows DCG's ``compute_match_confidence()`` approach:
start with pattern_confidence, then apply span-aware multipliers.
"""

from __future__ import annotations

from enum import StrEnum


class ConfidenceSignal(StrEnum):
    """Signal that contributed to a confidence adjustment."""

    EXECUTED_SPAN = "executed_span"
    INLINE_CODE_SPAN = "inline_code_span"
    DATA_SPAN = "data_span"
    ARGUMENT_SPAN = "argument_span"
    COMMENT_SPAN = "comment_span"
    HEREDOC_BODY_SPAN = "heredoc_body_span"
    UNKNOWN_SPAN = "unknown_span"
    SANITIZED_REGION = "sanitized_region"
    EXECUTION_OPERATORS_NEARBY = "execution_operators_nearby"
    COMMAND_POSITION = "command_position"
    ARGUMENT_POSITION = "argument_position"


_SIGNAL_WEIGHTS: dict[ConfidenceSignal, float] = {
    # High confidence — executed code
    ConfidenceSignal.EXECUTED_SPAN: 1.0,
    ConfidenceSignal.INLINE_CODE_SPAN: 1.0,
    # Boosters — slight confidence increase
    ConfidenceSignal.COMMAND_POSITION: 1.1,
    ConfidenceSignal.EXECUTION_OPERATORS_NEARBY: 1.1,
    # Low confidence — data context
    ConfidenceSignal.DATA_SPAN: 0.1,
    ConfidenceSignal.COMMENT_SPAN: 0.05,
    ConfidenceSignal.ARGUMENT_SPAN: 0.3,
    ConfidenceSignal.SANITIZED_REGION: 0.2,
    ConfidenceSignal.ARGUMENT_POSITION: 0.6,
    # Moderate confidence — ambiguous
    ConfidenceSignal.HEREDOC_BODY_SPAN: 0.7,
    ConfidenceSignal.UNKNOWN_SPAN: 0.8,
}


_SIGNAL_DESCRIPTIONS: dict[ConfidenceSignal, str] = {
    ConfidenceSignal.EXECUTED_SPAN: "match is in executed code",
    ConfidenceSignal.INLINE_CODE_SPAN: "match is in inline code (bash -c, python -c, etc.)",
    ConfidenceSignal.DATA_SPAN: "match is in a data string (single-quoted)",
    ConfidenceSignal.ARGUMENT_SPAN: "match is in a string argument to a safe command",
    ConfidenceSignal.COMMENT_SPAN: "match is in a comment",
    ConfidenceSignal.HEREDOC_BODY_SPAN: "match is in a heredoc body",
    ConfidenceSignal.UNKNOWN_SPAN: "match context is ambiguous",
    ConfidenceSignal.SANITIZED_REGION: "match was in a region masked by sanitization",
    ConfidenceSignal.EXECUTION_OPERATORS_NEARBY: "execution operators (|, ;, &&) found nearby",
    ConfidenceSignal.COMMAND_POSITION: "match is at command position",
    ConfidenceSignal.ARGUMENT_POSITION: "match is in argument position",
}


def signal_weight(signal: ConfidenceSignal) -> float:
    return _SIGNAL_WEIGHTS.get(signal, 0.8)


def signal_description(signal: ConfidenceSignal) -> str:
    return _SIGNAL_DESCRIPTIONS.get(signal, "unknown signal")


def _classify_match_span(
    command: str,
    match_start: int,
    match_end: int,
) -> ConfidenceSignal:
    """Determine which span kind the match falls in."""
    from app.context import SpanKind, classify_command

    spans = classify_command(command)
    if not spans:
        return ConfidenceSignal.UNKNOWN_SPAN

    for s in spans:
        if s.start <= match_start and match_end <= s.end:
            match s.kind:
                case SpanKind.EXECUTED:
                    return ConfidenceSignal.EXECUTED_SPAN
                case SpanKind.INLINE_CODE:
                    return ConfidenceSignal.INLINE_CODE_SPAN
                case SpanKind.DATA:
                    return ConfidenceSignal.DATA_SPAN
                case SpanKind.ARGUMENT:
                    return ConfidenceSignal.ARGUMENT_SPAN
                case SpanKind.COMMENT:
                    return ConfidenceSignal.COMMENT_SPAN
                case SpanKind.HEREDOC_BODY:
                    return ConfidenceSignal.HEREDOC_BODY_SPAN
                case _:
                    return ConfidenceSignal.UNKNOWN_SPAN

    # Match may span multiple spans (e.g. "rm -rf /" with whitespace gaps).
    # Check overlap with the most relevant span.
    best_overlap: float = 0
    best_signal: ConfidenceSignal = ConfidenceSignal.UNKNOWN_SPAN
    for s in spans:
        overlap_start = max(s.start, match_start)
        overlap_end = min(s.end, match_end)
        if overlap_start < overlap_end:
            overlap = (overlap_end - overlap_start) / (match_end - match_start)
            if overlap > best_overlap:
                best_overlap = overlap
                match s.kind:
                    case SpanKind.EXECUTED:
                        best_signal = ConfidenceSignal.EXECUTED_SPAN
                    case SpanKind.INLINE_CODE:
                        best_signal = ConfidenceSignal.INLINE_CODE_SPAN
                    case SpanKind.DATA:
                        best_signal = ConfidenceSignal.DATA_SPAN
                    case SpanKind.ARGUMENT:
                        best_signal = ConfidenceSignal.ARGUMENT_SPAN
                    case SpanKind.COMMENT:
                        best_signal = ConfidenceSignal.COMMENT_SPAN
                    case SpanKind.HEREDOC_BODY:
                        best_signal = ConfidenceSignal.HEREDOC_BODY_SPAN
                    case _:
                        best_signal = ConfidenceSignal.UNKNOWN_SPAN

    return best_signal


def has_execution_operators_nearby(
    command: str,
    match_start: int,
    match_end: int,
    window: int = 20,
) -> bool:
    """Check for execution operators (|, ;, &&, ||, $(, `) within window bytes."""
    search_start = max(0, match_start - window)
    prefix = command[search_start:match_start]

    search_end = min(len(command), match_end + window)
    suffix = command[match_end:search_end]

    operators = ["|", ";", "&&", "||", "$(", "`"]
    return any(op in prefix or op in suffix for op in operators)


def is_command_position(command: str, match_start: int) -> bool:
    """Check if match is at a command boundary."""
    if match_start == 0:
        return True

    prefix = command[:match_start]
    trimmed = prefix.rstrip()

    if not trimmed:
        return True

    last_char = trimmed[-1]
    # Command position after: |, ;, (, `, &&, ||, $(
    return (
        last_char in ("|", ";", "(", "`")
        or trimmed.endswith("&&")
        or trimmed.endswith("||")
        or trimmed.endswith("$(")
    )


def compute_span_confidence(
    command: str,
    match_start: int,
    match_end: int,
) -> tuple[float, list[ConfidenceSignal]]:
    """Compute span-aware confidence adjustments for a match.

    Returns (multiplier, signals) where multiplier is a value 0.0-1.0+
    that should be multiplied with pattern-level confidence.
    """
    signals: list[ConfidenceSignal] = []

    # Signal 1: Span classification
    span_signal = _classify_match_span(command, match_start, match_end)
    signals.append(span_signal)

    # Signal 2: Execution operators nearby (boost if present)
    if has_execution_operators_nearby(command, match_start, match_end):
        signals.append(ConfidenceSignal.EXECUTION_OPERATORS_NEARBY)

    # Signal 3: Command position vs argument position
    if is_command_position(command, match_start):
        signals.append(ConfidenceSignal.COMMAND_POSITION)
    else:
        signals.append(ConfidenceSignal.ARGUMENT_POSITION)

    # Combine: multiply all weights, clamp lower bound to 0.05
    multiplier = 1.0
    for sig in signals:
        multiplier *= signal_weight(sig)

    multiplier = max(0.05, multiplier)

    return multiplier, signals


def compute_match_confidence(
    pattern_confidence: float,
    command: str,
    match_start: int,
    match_end: int,
) -> tuple[float, list[ConfidenceSignal]]:
    """Full confidence computation: pattern-level × span-aware adjustments.

    Args:
        pattern_confidence: Base confidence from pattern characteristics (0.5-0.95).
        command: The full command string.
        match_start: Byte offset of match start (0-indexed).
        match_end: Byte offset of match end.

    Returns:
        (final_confidence, signals) where final_confidence is clamped to 0.0-1.0.
    """
    multiplier, signals = compute_span_confidence(command, match_start, match_end)

    final = pattern_confidence * multiplier
    final = max(0.0, min(1.0, final))

    return final, signals
