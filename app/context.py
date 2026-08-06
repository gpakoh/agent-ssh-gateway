"""Shell command span classification — context-aware pattern matching.

Reduces false positives by classifying command parts as executed code vs
data (strings, comments). Only executed spans are checked for destructive
patterns.

Span kinds:
    EXECUTED    — shell-executed code (checked for destructive patterns)
    DATA        — single-quoted string (no interpolation, safe to skip)
    COMMENT     — shell comment (# ...), safe to skip
    ARGUMENT    — argument to a known-safe wrapper (low-risk)
    INLINE_CODE — -c/-e flag content (bash -c, python -c) — checked
    HEREDOC_BODY — heredoc body — checked (recursive via heredoc scanner)
    UNKNOWN     — ambiguous, treated as EXECUTED
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SpanKind(StrEnum):
    PENDING = "pending"
    EXECUTED = "executed"
    DATA = "data"
    COMMENT = "comment"
    ARGUMENT = "argument"
    INLINE_CODE = "inline_code"
    HEREDOC_BODY = "heredoc_body"
    UNKNOWN = "unknown"

    def should_check(self) -> bool:
        return self in (SpanKind.EXECUTED, SpanKind.INLINE_CODE, SpanKind.HEREDOC_BODY, SpanKind.UNKNOWN)


@dataclass(frozen=True)
class Span:
    kind: SpanKind
    text: str
    start: int

    @property
    def end(self) -> int:
        return self.start + len(self.text)


# Known-safe wrappers — commands whose arguments are data, not code.
# Map: command → set of flag prefixes that take data arguments.
# () = all positional args are data; ("-m", "-n") = only after these flags.
_SAFE_WRAPPER_FLAGS: dict[str, tuple[str, ...] | None] = {
    "echo": (),
    "printf": (),
    "grep": ("-e",),
    "rg": (),
    "ag": (),
    "ack": (),
    "cat": (),
    "head": (),
    "tail": (),
    "less": (),
    "more": (),
    "jq": (),
    "yq": (),
    "sed": (),
    "awk": (),
}

# Safe subcommands of `git` whose arguments are data.
_SAFE_GIT_SUBCOMMANDS: dict[str, tuple[str, ...] | None] = {
    "commit": ("-m",),
    "log": (),
    "diff": (),
    "show": (),
    "status": (),
    "blame": (),
    "branch": (),
    "describe": (),
    "shortlog": (),
}

# Commands where -c/-e flag carries inline executable code.
_INLINE_CODE_FLAGS: dict[str, tuple[str, ...]] = {
    "bash": ("-c",),
    "sh": ("-c",),
    "python": ("-c",),
    "python3": ("-c",),
    "node": ("-e", "-p"),
    "ruby": ("-e",),
    "perl": ("-e",),
}

# Safe base commands (not subcommand-aware) — all positional args are data.
_SAFE_COMMANDS: set[str] = set(_SAFE_WRAPPER_FLAGS.keys()) - {"git"}


def _tokenize(command: str) -> list[tuple[str, int, SpanKind]]:
    """Tokenize a command line into (text, start, kind) triples.

    This is a lightweight shell-aware tokenizer that handles single quotes,
    double quotes, comments, backticks, and heredocs.
    """
    tokens: list[tuple[str, int, SpanKind]] = []
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]

        # Whitespace — skip (don't emit tokens)
        if ch in " \t\n":
            i += 1
            continue

        # Shell comment — rest of line
        if ch == "#" and (i == 0 or command[i - 1] in " \t\n"):
            tokens.append((command[i:], i, SpanKind.COMMENT))
            break

        # Single quote — DATA
        if ch == "'":
            start = i
            i += 1
            while i < n and command[i] != "'":
                i += 1
            if i < n:
                i += 1  # closing quote
            tokens.append((command[start:i], start, SpanKind.DATA))
            continue

        # Double quote — ARGUMENT (could be interpolation but often safe)
        if ch == '"':
            start = i
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\":
                    i += 1  # skip escaped char
                i += 1
            if i < n:
                i += 1  # closing quote
            tokens.append((command[start:i], start, SpanKind.ARGUMENT))
            continue

        # Backtick — EXECUTED (command substitution)
        if ch == "`":
            start = i
            i += 1
            while i < n and command[i] != "`":
                if command[i] == "\\":
                    i += 1
                i += 1
            if i < n:
                i += 1
            tokens.append((command[start:i], start, SpanKind.EXECUTED))
            continue

        # Heredoc start — find the delimiter, then capture body
        if ch == "<" and i + 1 < n and command[i + 1] == "<":
            start = i
            i += 2
            strip_tabs = False
            if i < n and command[i] == "-":
                strip_tabs = True
                i += 1
            # Skip optional whitespace, handle quoted delimiter
            while i < n and command[i] in " \t":
                i += 1
            # Quoted delimiter: <<'EOF' or <<"EOF"
            quoted = False
            if i < n and command[i] in ("'", '"'):
                quote_char = command[i]
                quoted = True
                i += 1
            # Read delimiter
            delim_start = i
            while i < n and command[i] not in (" \t\n;|&" if not quoted else (quote_char,)):
                i += 1
            delim = command[delim_start:i]
            if quoted and i < n and command[i] == quote_char:
                i += 1  # skip closing quote
            tokens.append((command[start:i], start, SpanKind.UNKNOWN))

            if not delim or i >= n:
                continue

            # <<< is a here-string (single-line data), not a heredoc
            if delim == "<" and command[delim_start - 1 : delim_start + 1] == "<<":
                tokens.append((command[i:], i, SpanKind.DATA))
                break

            body_start = i
            while i < n:
                line_start = i
                # Skip to next newline or end
                while i < n and command[i] != "\n":
                    i += 1
                line_end = i
                # Check for delimiter at line start (after optional tab for <<-)
                check_pos = line_start
                if strip_tabs and check_pos < n and command[check_pos] == "\t":
                    check_pos += 1
                if command[check_pos:].startswith(delim) and check_pos < n:
                    body_end = line_start
                    if body_end > body_start:
                        tokens.append((command[body_start:body_end], body_start, SpanKind.HEREDOC_BODY))
                    tokens.append((command[line_start:line_end], line_start, SpanKind.UNKNOWN))
                    i = line_end + 1 if line_end < n else line_end
                    break
                # Skip newline
                if line_end < n:
                    i = line_end + 1
                else:
                    i = line_end
            continue

        # Normal word or whitespace, or shell metacharacter (|, ;, &)
        start = i
        while i < n and command[i] not in " \t\n'\"`#<|;&":
            i += 1
        if i > start:
            tokens.append((command[start:i], start, SpanKind.PENDING))
            continue

        # Shell metacharacter — emit as UNKNOWN (reset context in classifier)
        tokens.append((command[i], i, SpanKind.PENDING))
        i += 1

    return tokens


def _classify_tokens(
    tokens: list[tuple[str, int, SpanKind]],
) -> list[Span]:
    """Classify pending tokens based on command structure.

    Context awareness: first word is the command; following words
    may be arguments to known-safe wrappers.
    """
    spans: list[Span] = []
    cmd_word: str | None = None
    subcmd_word: str | None = None
    pending_flags: list[str] = []
    # None = not a safe wrapper, () = safe, all positional args are data
    # ("-m",) = safe, only args after these flags are data
    safe_flags: tuple[str, ...] | None = ()
    inline_flags: tuple[str, ...] = ()
    # True right after an inline-code flag (-c/-e/-p) — the NEXT token is
    # the code payload regardless of its own tokenizer-assigned kind (a
    # quoted payload like 'rm -rf /' or "..." already arrives as
    # DATA/ARGUMENT straight from the tokenizer, bypassing the kind-check
    # below entirely, so this must be checked before it).
    pending_inline = False
    i = 0

    while i < len(tokens):
        text, start, kind = tokens[i]

        if pending_inline:
            pending_inline = False
            spans.append(Span(SpanKind.INLINE_CODE, text, start))
            i += 1
            continue

        if kind != SpanKind.PENDING:
            spans.append(Span(kind, text, start))
            i += 1
            continue

        word = text.lstrip(" \t\n").split()[0] if text.strip() else ""

        # Shell metacharacters (|, ;, &) — reset command context
        if word in ("|", ";", "&", "&&", "||", "|&"):
            spans.append(Span(SpanKind.EXECUTED, text, start))
            cmd_word = None
            subcmd_word = None
            pending_flags = []
            safe_flags = None
            inline_flags = ()
            i += 1
            continue

        if cmd_word is None:
            # First word — the command itself
            cmd_word = word
            spans.append(Span(SpanKind.EXECUTED, text, start))
            subcmd_word = None

            if cmd_word == "git":
                safe_flags = None  # will be set by subcommand
            elif cmd_word in _SAFE_COMMANDS:
                safe_flags = _SAFE_WRAPPER_FLAGS.get(cmd_word)  # () or tuple
            else:
                safe_flags = None  # not a safe wrapper

            inline_flags = _INLINE_CODE_FLAGS.get(cmd_word, ())
        elif (
            cmd_word == "git"
            and subcmd_word is None
            and word in _SAFE_GIT_SUBCOMMANDS
        ):
            subcmd_word = word
            git_flags = _SAFE_GIT_SUBCOMMANDS.get(word)
            safe_flags = git_flags if git_flags is not None else ()
            spans.append(Span(SpanKind.UNKNOWN, text, start))
        elif pending_flags:
            # This is a data argument for a previous safe flag
            pending_flags.pop()
            spans.append(Span(SpanKind.ARGUMENT, text, start))
        elif word.startswith("-") and not word.startswith("--"):
            flag = word[:2].rstrip()
            if flag in inline_flags:
                # Next token (regardless of tokenizer kind) is INLINE_CODE
                inline_flags = ()
                pending_inline = True
                spans.append(Span(SpanKind.EXECUTED, text, start))
            elif safe_flags is not None and flag in safe_flags:
                # Next arg is safe data
                pending_flags.append(flag)
                spans.append(Span(SpanKind.EXECUTED, text, start))
            elif safe_flags is not None:
                # Safe wrapper but unknown flag → positional args still safe
                spans.append(Span(SpanKind.EXECUTED, text, start))
            else:
                # Not a safe wrapper → just executed
                spans.append(Span(SpanKind.EXECUTED, text, start))
        elif safe_flags is not None:
            # Positional arg to a known-safe wrapper → data
            if word == "--":
                spans.append(Span(SpanKind.EXECUTED, text, start))
            else:
                spans.append(Span(SpanKind.ARGUMENT, text, start))
        else:
            # Not a safe wrapper → executed code
            spans.append(Span(SpanKind.EXECUTED, text, start))

        i += 1

    # Merge adjacent spans of the same kind where possible
    return _merge_spans(spans)


def _merge_spans(spans: list[Span]) -> list[Span]:
    if not spans:
        return spans
    merged: list[Span] = [spans[0]]
    for s in spans[1:]:
        last = merged[-1]
        if s.kind == last.kind and s.start == last.end:
            merged[-1] = Span(last.kind, last.text + s.text, last.start)
        else:
            merged.append(s)
    return merged


def classify_command(command: str) -> list[Span]:
    """Parse a shell command into classified spans.

    Returns a list of Spans, each with a SpanKind indicating whether
    the span is executed code (should be checked) or data (safe to skip).
    """
    tokens = _tokenize(command)
    return _classify_tokens(tokens)


def check_context_filter(command: str) -> bool:
    """Quick check: should this command be scanned at all?

    Returns True if the command has any span that requires pattern checking.
    A command whose entire content is DATA or COMMENT can be skipped entirely.
    """
    spans = classify_command(command)
    for s in spans:
        if s.kind.should_check():
            return True
    return False


def compute_span_confidence(command: str, match_start: int, match_end: int) -> float:
    """Adjust confidence based on which span a match lands in.

    Delegates to ``app.confidence.compute_span_confidence`` for the
    full signal-based implementation.

    Returns a multiplier (0.0-1.0) to apply to pattern confidence.
    """
    from app.confidence import compute_span_confidence as _compute

    multiplier, _signals = _compute(command, match_start, match_end)
    return multiplier
