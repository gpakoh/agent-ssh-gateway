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


def _find_closing_quote(text: str, quote: str, start: int = 1) -> int:
    """Find unescaped closing quote, respecting backslash escapes."""
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i
        i += 1
    return -1


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
            # Extract quoted string with escape-aware closing-quote scan
            quote = after_flag[0]
            if quote not in ('"', "'"):
                continue
            end = _find_closing_quote(after_flag, quote)
            if end == -1:
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
        pattern = rf"\b{cmd_name}\s+['\"]"
        for m in re.finditer(pattern, command):
            quote_pos = m.end() - 1
            quote = command[quote_pos]
            rest = command[quote_pos + 1:]
            end = _find_closing_quote(rest, quote)
            if end == -1:
                code = rest
            else:
                code = rest[:end]
            code = code.strip()
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
                suggestions=m.suggestions,
            ))

    return findings


# ---------------------------------------------------------------------------
# Obfuscation normalization (variable resolution + scan candidates)
# ---------------------------------------------------------------------------

_VAR_ASSIGN_RE = re.compile(
    r"(?:^|[\s;|&()])([A-Za-z_][A-Za-z0-9_]*)=(?:([\"'])(.*?)\2|(\S+?))(?=[\s;|&()]|$)"
)
_VAR_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)\b")


def resolve_shell_variables(command: str) -> str:
    """Heuristically resolve ``VAR=value`` assignments against variable
    references (``$VAR``/``${VAR}``) in a command string.

    String-based approximation of shell expansion: standalone ``VAR=value``
    assignments (word or quoted values) are collected, then every matching
    reference is substituted. Resolution runs to a small fixed point so
    chained indirection (``x=rm; y=$x; $y -rf /``) is also expanded.

    This is deliberately conservative — it never invokes a shell and never
    rejects input. It exists so the regex-based scan_command can see through
    variable obfuscation like ``x=rm; $x -rf /``; the policy enforcement
    gates (metachar / interpreter shape / heredoc) remain the real control.
    """
    assignments: dict[str, str] = {}
    for m in _VAR_ASSIGN_RE.finditer(command):
        name = m.group(1)
        value = m.group(3) if m.group(3) is not None else m.group(4)
        if value is not None:
            assignments.setdefault(name, value)
    if not assignments:
        return command

    resolved = command
    for _ in range(3):
        prev = resolved
        for name, value in assignments.items():
            def _replace(match: re.Match[str], n: str = name, v: str = value) -> str:
                return v if match.group(1) == n or match.group(2) == n else match.group(0)
            resolved = _VAR_REF_RE.sub(_replace, resolved)
        if resolved == prev:
            break
    return resolved


def normalize_scan_candidates(command: str) -> list[str]:
    """Return deduplicated command variants that should be pattern-scanned.

    Variants: the raw command, its variable-resolved form, and nested inline
    scripts (``bash -c '...'``, heredocs, ``$(...)``, backticks, ``eval``)
    with their resolved forms. Used by ``scan_command`` so the informational
    scanner sees through shell obfuscation the same way the enforcement gates
    do.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(text: str) -> None:
        text = text.strip()
        if text and text not in seen:
            seen.add(text)
            candidates.append(text)

    _add(command)
    _add(resolve_shell_variables(command))
    for base in list(candidates):
        for ec in extract_all(base):
            _add(ec.script)
            _add(resolve_shell_variables(ec.script))
    return candidates
