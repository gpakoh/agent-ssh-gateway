"""Heredoc and inline script detection (DCG-style 2-tier).

Tier 1: Trigger detection via fast regex.
Tier 2: Content extraction from inline scripts, heredocs, here-strings,
        and command substitutions.

Extracted inner commands are recursively scanned for destructive patterns.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from app.command_policy import DestructiveMatch, _check_all_destructive

# ---------------------------------------------------------------------------
# Tier 1: Trigger patterns
# ---------------------------------------------------------------------------

_TRIGGER_PATTERNS: list[re.Pattern] = [
    # Here-string operator
    re.compile(r"<<<"),
    # Python inline execution: python -c "...", python3 -c '...'
    re.compile(r"\bpython[0-9.]*(?:\.exe)?\b(?:\s+(?:--\S+|-[A-Za-z]+(?:[:.=]\S*)?)(?:\s+(?:[0-9]\S*|\S*[:/\\]\S*|[A-Za-z][A-Za-z0-9_]*))?)*\s+-[A-Za-z]*[ce][A-Za-z]*(?:\s|['\"]|$)"),
    # Ruby inline execution: ruby -e "..."
    re.compile(r"\bruby[0-9.]*(?:\.exe)?\b(?:\s+(?:--\S+|-[A-Za-z]+(?:[:.=]\S*)?)(?:\s+(?:[0-9]\S*|\S*[:/\\]\S*|[A-Za-z][A-Za-z0-9_]*))?)*\s+-[A-Za-z]*e[A-Za-z]*(?:\s|['\"]|$)"),
    # Perl inline execution: perl -e "..."
    re.compile(r"\bperl[0-9.]*(?:\.exe)?\b(?:\s+(?:--\S+|-[A-Za-z]+(?:[:.=]\S*)?)(?:\s+(?:[0-9]\S*|\S*[:/\\]\S*|[A-Za-z][A-Za-z0-9_]*))?)*\s+-[A-Za-z]*[eE][A-Za-z]*(?:\s|['\"]|$)"),
    # Node inline execution: node -e "..."
    re.compile(r"\bnode(?:js)?[0-9.]*(?:\.exe)?\b(?:\s+(?:--\S+|-[A-Za-z]+(?:[:.=]\S*)?)(?:\s+(?:[0-9]\S*|\S*[:/\\]\S*|[A-Za-z][A-Za-z0-9_]*))?)*\s+-[A-Za-z]*[ep][A-Za-z]*(?:\s|['\"]|$)"),
    # PHP inline execution: php -r "..."
    re.compile(r"\bphp[0-9.]*(?:\.exe)?\b(?:\s+(?:--\S+|-[A-Za-z]+(?:[:.=]\S*)?)(?:\s+(?:[0-9]\S*|\S*[:/\\]\S*|[A-Za-z][A-Za-z0-9_]*))?)*\s+-[A-Za-z]*r[A-Za-z]*(?:\s|['\"]|$)"),
    # Shell inline execution: sh -c "...", bash -c "..."
    re.compile(r"\b(?:sh|bash|zsh|fish)(?:\.exe)?\b(?:\s+(?:--\S+|-[A-Za-z]+(?:[:.=]\S*)?)(?:\s+(?:[0-9]\S*|\S*[:/\\]\S*|[A-Za-z][A-Za-z0-9_]*))?)*\s+-[A-Za-z]*c[A-Za-z]*(?:\s|['\"]|$)"),
    # PowerShell inline: powershell -Command "..."
    re.compile(r"(?i)\b(?:powershell|pwsh)(?:\.exe)?['\"]?(?:\s+-\S+(?:\s+(?:[0-9]\S*|\S*[:/\\]\S*|[A-Za-z][A-Za-z0-9_]*))?)*\s+-c[a-z]*\s*['\"]"),
    # PowerShell -EncodedCommand
    re.compile(r"(?i)\b(?:powershell|pwsh)(?:\.exe)?['\"]?(?:\s+-\S+(?:\s+(?:[0-9]\S*|\S*[:/\\]\S*|[A-Za-z][A-Za-z0-9_]*))?)*\s+-e(?:n(?:c(?:o(?:d(?:e(?:d(?:c(?:o(?:m(?:m(?:a(?:n(?:d)?)?)?)?)?)?)?)?)?)?)?)?)?\s+[A-Za-z0-9+/=]"),
    # cmd.exe /c or /k
    re.compile(r"(?i)\bcmd(?:\.exe)?\b(?:\s+/[A-Za-z]+)*\s+/[ck]\b"),
    # Invoke-Expression / iex
    re.compile(r"(?i)(?:^|[\s;|&({])(?:iex|invoke-expression)\b"),
    # Piped to interpreters
    re.compile(r"\|\s*(?:python[0-9.]*|ruby[0-9.]*|perl[0-9.]*|node(?:js)?[0-9.]*|php[0-9.]*|lua[0-9.]*|sh|bash)(?:\.exe)?\b"),
    # Piped to xargs
    re.compile(r"\|\s*xargs\s"),
    # eval/exec with quoted string
    re.compile(r"\beval\s+['\"]"),
    re.compile(r"\bexec\s+['\"]"),
    # Command substitution $(...) — also catches bare $(
    re.compile(r"\$\(.*?\)"),
    # Backtick substitution
    re.compile(r"`[^`]+`"),
]

# Manually also check for heredoc operator `<<`
_HEREDOC_OP_RE = re.compile(r"<<[-~]?(?:\s*[A-Za-z_][A-Za-z0-9_]*)?")


def has_heredoc_triggers(command: str) -> bool:
    """Tier 1: fast check for any heredoc/inline script trigger."""
    for p in _TRIGGER_PATTERNS:
        if p.search(command):
            return True
    if _HEREDOC_OP_RE.search(command):
        return True
    return False


# ---------------------------------------------------------------------------
# Tier 2: Content extraction
# ---------------------------------------------------------------------------

class ExtractedCommand(NamedTuple):
    script: str
    extractor: str
    language: str


def extract_inline_scripts(command: str) -> list[ExtractedCommand]:
    """Extract code from `python -c "..."`, `bash -c '...'`, etc."""
    results: list[ExtractedCommand] = []

    # Match interpreter flags -c/-e/-r followed by quoted string
    patterns = [
        (r"\bpython[0-9.]*\b", r"-[A-Za-z]*[ce][A-Za-z]*", "python"),
        (r"\bruby[0-9.]*\b", r"-[A-Za-z]*e[A-Za-z]*", "ruby"),
        (r"\bperl[0-9.]*\b", r"-[A-Za-z]*[eE][A-Za-z]*", "perl"),
        (r"\bnode(?:js)?[0-9.]*\b", r"-[A-Za-z]*[ep][A-Za-z]*", "javascript"),
        (r"\bphp[0-9.]*\b", r"-[A-Za-z]*r[A-Za-z]*", "php"),
        (r"\b(?:sh|bash|zsh|fish)\b", r"-[A-Za-z]*c[A-Za-z]*", "bash"),
    ]

    for interp_re, flag_re, lang in patterns:
        for match in re.finditer(interp_re, command):
            # Find the flag after interpreter
            rest = command[match.end():]
            flag_m = re.search(flag_re, rest)
            if not flag_m:
                continue
            after_flag = rest[flag_m.end():].lstrip()
            if not after_flag:
                continue
            # Extract quoted string
            quote = after_flag[0]
            if quote not in ('"', "'"):
                continue
            # Find closing quote
            end = after_flag.find(quote, 1)
            if end == -1:
                # No closing quote - take rest of string
                code = after_flag[1:]
            else:
                code = after_flag[1:end]
            if code.strip():
                results.append(ExtractedCommand(code.strip(), "inline_script", lang))

    return results


def extract_eval_exec(command: str) -> list[ExtractedCommand]:
    """Extract code from `eval "..."` or `exec "..."`."""
    results: list[ExtractedCommand] = []
    for cmd_name in ("eval", "exec"):
        pattern = rf"\b{cmd_name}\s+(['\"])(.*?)(\1)"
        for m in re.finditer(pattern, command, re.DOTALL):
            code = m.group(2).strip()
            if code:
                results.append(ExtractedCommand(code, f"{cmd_name}_call", "shell"))
    return results


def extract_heredocs(command: str) -> list[ExtractedCommand]:
    """Extract heredoc bodies (<<EOF ... EOF)."""
    results: list[ExtractedCommand] = []
    # Match heredoc bodies: <<EOF ... EOF (optional space after <<)
    # Quoted delimiter: <<'EOF' or <<"EOF"
    patterns = [
        (r"<<[-~]?\s*'([A-Za-z_][A-Za-z0-9_]*)'\s*\n(.*?)\n\1", False),
        (r'<<[-~]?\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\n(.*?)\n\1', False),
        (r"<<[-~]?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\n(.*?)\n\1", False),
    ]
    for pattern, _ in patterns:
        for m in re.finditer(pattern, command, re.DOTALL):
            body = m.group(2).strip()
            if body:
                results.append(ExtractedCommand(body, "heredoc", "shell"))
    return results


def extract_herestrings(command: str) -> list[ExtractedCommand]:
    """Extract here-string content (<<< "string")."""
    results: list[ExtractedCommand] = []
    pattern = r"<<<\s+['\"](.*?)['\"]"
    for m in re.finditer(pattern, command):
        content = m.group(1).strip()
        if content:
            results.append(ExtractedCommand(content, "herestring", "shell"))
    return results


def extract_command_substitutions(command: str) -> list[ExtractedCommand]:
    """Extract content from $(...) and backtick substitutions."""
    results: list[ExtractedCommand] = []
    # $(...) - track nesting depth
    i = 0
    while i < len(command):
        if command[i:i+2] == "$(":
            depth = 1
            start = i + 2
            j = start
            while j < len(command) and depth > 0:
                if command[j:j+2] == "$(":
                    depth += 1
                    j += 2
                elif command[j] == ")":
                    depth -= 1
                    j += 1
                else:
                    j += 1
            if depth == 0:
                inner = command[start:j-1].strip()
                if inner:
                    results.append(ExtractedCommand(inner, "dollar_paren", "shell"))
            i = j
        elif command[i] == "`":
            end = command.find("`", i + 1)
            if end != -1:
                inner = command[i+1:end].strip()
                if inner:
                    results.append(ExtractedCommand(inner, "backtick", "shell"))
                i = end + 1
            else:
                i += 1
        else:
            i += 1
    return results


def extract_all(command: str) -> list[ExtractedCommand]:
    """Run all extractors and return deduplicated results."""
    results: list[ExtractedCommand] = []
    results.extend(extract_inline_scripts(command))
    results.extend(extract_eval_exec(command))
    results.extend(extract_heredocs(command))
    results.extend(extract_herestrings(command))
    results.extend(extract_command_substitutions(command))
    # Deduplicate by script content
    seen: set[str] = set()
    deduped: list[ExtractedCommand] = []
    for ec in results:
        if ec.script not in seen:
            seen.add(ec.script)
            deduped.append(ec)
    return deduped


# ---------------------------------------------------------------------------
# Recursive scanning
# ---------------------------------------------------------------------------

def check_nested_commands(command: str) -> list[DestructiveMatch]:
    """Extract nested commands from heredoc/inline scripts and scan them.

    Returns list of destructive matches found in nested content.
    """
    findings: list[DestructiveMatch] = []
    if not has_heredoc_triggers(command):
        return findings

    extracted = extract_all(command)
    for ec in extracted:
        nested = _check_all_destructive(ec.script)
        for m in nested:
            findings.append(DestructiveMatch(
                pattern_name=f"nested:{m.pattern_name}",
                reason=f"[{ec.extractor} → {ec.language}] {m.reason}",
                severity=m.severity,
                suggestion=m.suggestion,
            ))

    return findings
