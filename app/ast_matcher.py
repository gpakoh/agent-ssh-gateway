"""AST-based pattern matching for embedded code (P8).

Uses Python stdlib ``ast`` for Python scripts — structural detection of
dangerous function calls with zero regex false positives.

For other script languages (bash, node, ruby), falls back to regex patterns
since we don't have ast-grep-core.

Integration: called by ``app/heredoc_scanner.py`` for extracted script bodies.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from enum import StrEnum


class MatchSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class AstMatch:
    rule_id: str
    reason: str
    severity: MatchSeverity
    lineno: int
    col_offset: int
    matched_text: str
    suggestion: str | None = None


@dataclass(frozen=True)
class _PatternDef:
    rule_id: str
    reason: str
    severity: MatchSeverity
    suggestion: str | None = None


# Python patterns: (module, function) → pattern
# module=None means "any module" (for bare function names after from-import)
_PYTHON_MODULE_FUNC: dict[tuple[str | None, str], _PatternDef] = {
    # File deletion — critical
    (None, "rmtree"): _PatternDef("ast.python.shutil_rmtree", "shutil.rmtree() recursively deletes directories", MatchSeverity.CRITICAL, "Use explicit path validation"),
    # File deletion — high
    ("os", "remove"): _PatternDef("ast.python.os_remove", "os.remove() deletes files", MatchSeverity.HIGH),
    (None, "remove"): _PatternDef("ast.python.os_remove", "os.remove() deletes files", MatchSeverity.HIGH),
    ("os", "unlink"): _PatternDef("ast.python.os_unlink", "os.unlink() deletes files", MatchSeverity.HIGH),
    (None, "unlink"): _PatternDef("ast.python.os_unlink", "os.unlink() deletes files", MatchSeverity.HIGH),
    ("os", "rmdir"): _PatternDef("ast.python.os_rmdir", "os.rmdir() deletes directories", MatchSeverity.HIGH),
    (None, "rmdir"): _PatternDef("ast.python.os_rmdir", "os.rmdir() deletes directories", MatchSeverity.HIGH),
    # shutil directory ops
    ("shutil", "rmtree"): _PatternDef("ast.python.shutil_rmtree", "shutil.rmtree() recursively deletes directories", MatchSeverity.CRITICAL, "Use explicit path validation"),
    # Shell execution
    ("os", "system"): _PatternDef("ast.python.os_system", "os.system() executes shell commands", MatchSeverity.MEDIUM, "Use subprocess with explicit args"),
    (None, "system"): _PatternDef("ast.python.os_system", "os.system() executes shell commands", MatchSeverity.MEDIUM, "Use subprocess with explicit args"),
    ("os", "popen"): _PatternDef("ast.python.os_popen", "os.popen() executes shell commands", MatchSeverity.MEDIUM, "Use subprocess instead"),
    (None, "popen"): _PatternDef("ast.python.os_popen", "os.popen() executes shell commands", MatchSeverity.MEDIUM, "Use subprocess instead"),
    # Subprocess
    ("subprocess", "run"): _PatternDef("ast.python.subprocess_run", "subprocess.run() executes shell commands", MatchSeverity.MEDIUM),
    (None, "run"): _PatternDef("ast.python.subprocess_run", "subprocess.run() executes shell commands", MatchSeverity.MEDIUM),
    ("subprocess", "call"): _PatternDef("ast.python.subprocess_call", "subprocess.call() executes shell commands", MatchSeverity.MEDIUM),
    (None, "call"): _PatternDef("ast.python.subprocess_call", "subprocess.call() executes shell commands", MatchSeverity.MEDIUM),
    ("subprocess", "Popen"): _PatternDef("ast.python.subprocess_popen", "subprocess.Popen() spawns processes", MatchSeverity.MEDIUM),
    (None, "Popen"): _PatternDef("ast.python.subprocess_popen", "subprocess.Popen() spawns processes", MatchSeverity.MEDIUM),
}

# Regex-based patterns for non-Python scripts, grouped by language.
_SCRIPT_PATTERNS: dict[str, list[tuple[re.Pattern, _PatternDef]]] = {}

# Shell interpreters whose "-c" payload is a nested shell command.
_SHELL_INTERPRETERS = {"sh", "bash", "dash", "zsh", "/bin/sh", "/bin/bash", "/bin/dash", "/bin/zsh"}

# Binaries whose argv is scanned as a nested command when passed as a list
# to subprocess.run/call/Popen (e.g. ["rm", "-rf", "/"]).
_NESTED_SCAN_BINARIES = {
    "rm", "mv", "dd", "mkfs", "wipefs", "fdisk", "sfdisk", "parted",
    "truncate", "pkill", "killall", "git", "docker",
}


def _compile_script_patterns():
    if _SCRIPT_PATTERNS:
        return

    _SCRIPT_PATTERNS["bash"] = [
        (re.compile(r"\brm\s+-[rR]f\b"), _PatternDef("ast.bash.rm_rf", "rm -rf recursively deletes files", MatchSeverity.CRITICAL)),
        (re.compile(r"\bdrop\s+(database|table|schema)\s", re.IGNORECASE), _PatternDef("ast.bash.psql_drop", "SQL DROP statement destroys data", MatchSeverity.HIGH)),
        (re.compile(r"\bdd\s+if="), _PatternDef("ast.bash.dd_destructive", "dd with if= can overwrite data", MatchSeverity.HIGH)),
        (re.compile(r"\bmkfs\."), _PatternDef("ast.bash.mkfs", "mkfs formats filesystems", MatchSeverity.CRITICAL)),
    ]

    _SCRIPT_PATTERNS["javascript"] = [
        (re.compile(r"\bfs\.rmSync\s*\("), _PatternDef("ast.javascript.fs_rmsync", "fs.rmSync() deletes files", MatchSeverity.MEDIUM)),
        (re.compile(r"\bfs\.rmdirSync\s*\("), _PatternDef("ast.javascript.fs_rmdirsync", "fs.rmdirSync() deletes directories", MatchSeverity.MEDIUM)),
        (re.compile(r"\bchild_process\.exec(?:Sync)?\s*\("), _PatternDef("ast.javascript.execsync", "child_process.exec() executes shell commands", MatchSeverity.MEDIUM)),
        (re.compile(r"\bchild_process\.spawnSync\s*\("), _PatternDef("ast.javascript.spawnsync", "child_process.spawnSync() executes shell commands", MatchSeverity.MEDIUM)),
    ]

    _SCRIPT_PATTERNS["ruby"] = [
        (re.compile(r"\bFileUtils\.rm_rf\s*\("), _PatternDef("ast.ruby.fileutils_rm_rf", "FileUtils.rm_rf() deletes directories", MatchSeverity.MEDIUM)),
        (re.compile(r"\bFile\.delete\s*\("), _PatternDef("ast.ruby.file_delete", "File.delete() deletes files", MatchSeverity.MEDIUM)),
        (re.compile(r"\bFileUtils\.remove_dir\s*\("), _PatternDef("ast.ruby.fileutils_remove_dir", "FileUtils.remove_dir() deletes directories", MatchSeverity.MEDIUM)),
    ]

    _SCRIPT_PATTERNS["typescript"] = [
        (re.compile(r"\bfs\.rmSync\s*\("), _PatternDef("ast.typescript.fs_rmsync", "fs.rmSync() deletes files", MatchSeverity.MEDIUM)),
        (re.compile(r"\bDeno\.remove\s*\("), _PatternDef("ast.typescript.deno_remove", "Deno.remove() deletes files", MatchSeverity.MEDIUM)),
        (re.compile(r"\bchild_process\.exec(?:Sync)?\s*\("), _PatternDef("ast.typescript.execsync", "child_process.exec() executes shell commands", MatchSeverity.MEDIUM)),
    ]


def _check_ast_python(code: str) -> list[AstMatch]:
    """Check Python code using stdlib ``ast``.

    Returns a list of matches for dangerous function calls.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []

    # Track imports: name → (module_path, original_name)
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                imports[name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                for alias in node.names:
                    name = alias.asname or alias.name
                    imports[name] = f"{node.module}.{alias.name}"

    matches: list[AstMatch] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func

        # Nested-command analysis for subprocess.run/call/Popen: a list argv
        # or shell=True string is invisible to the regex packs above, so
        # ``subprocess.run(['rm','-rf','/'])`` would only be MEDIUM.
        if isinstance(func, (ast.Attribute, ast.Name)):
            func_name = func.attr if isinstance(func, ast.Attribute) else func.id
            if func_name in ("run", "call", "Popen"):
                matches.extend(_subprocess_nested_matches(node, code))

        # Direct attribute call: os.remove(x) → func=Attribute(value=Name(id='os'), attr='remove')
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module_name = func.value.id
            func_name = func.attr

            # Check (module, function) pattern
            key = (module_name, func_name)
            if key in _PYTHON_MODULE_FUNC:
                pattern = _PYTHON_MODULE_FUNC[key]
                matches.append(_make_ast_match(node, pattern, code))
                continue

            # Check (None, function) — module doesn't matter for imported names
            # But for attribute calls like os.remove, the module IS specified
            # so only match if the module is one we know

        # Bare function call: remove(x) after "from os import remove"
        if isinstance(func, ast.Name):
            func_name = func.id
            key = (None, func_name)
            if key in _PYTHON_MODULE_FUNC:
                pattern = _PYTHON_MODULE_FUNC[key]
                matches.append(_make_ast_match(node, pattern, code))
                continue

            # Check imports for aliased functions
            if func_name in imports:
                imported_path = imports[func_name]
                # imported_path is like "os.remove" or "shutil.rmtree"
                parts = imported_path.rsplit(".", 1)
                if len(parts) == 2:
                    mod, name = parts
                    key = (mod, name)
                    if key in _PYTHON_MODULE_FUNC:
                        pattern = _PYTHON_MODULE_FUNC[key]
                        matches.append(_make_ast_match(node, pattern, code))
                        continue
                    # Also try (None, name)
                    key2 = (None, name)
                    if key2 in _PYTHON_MODULE_FUNC:
                        pattern = _PYTHON_MODULE_FUNC[key2]
                        matches.append(_make_ast_match(node, pattern, code))
                        continue

        # Method chain: Path(x).unlink() → Call(func=Attribute(value=Call(...), attr='unlink'))
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call):
            method_name = func.attr
            key = (None, method_name)
            if key in _PYTHON_MODULE_FUNC:
                pattern = _PYTHON_MODULE_FUNC[key]
                matches.append(_make_ast_match(node, pattern, code))
                continue

    return matches


def _subprocess_nested_matches(call_node: ast.Call, code: str) -> list[AstMatch]:
    """Rebuild the nested command of a subprocess.run/call/Popen call.

    Supports both forms:
    * list argv: ``subprocess.run(['rm', '-rf', '/'])`` — rebuilt argv is
      scanned when the binary is a shell interpreter (``sh -c ...``) or a
      known destructive binary;
    * string form: ``subprocess.run('rm -rf /', shell=True)``.

    Returns additional matches (rule_id ``ast.python.nested.*``) so the
    destructive intent is not lost behind the generic MEDIUM subprocess hit.
    """
    if not call_node.args:
        return []
    first = call_node.args[0]

    nested: list[str] = []
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        nested.append(first.value)
    elif isinstance(first, ast.List):
        argv: list[str] = []
        for elt in first.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                argv.append(elt.value)
            else:
                return []
        if not argv:
            return []
        if argv[0] in _SHELL_INTERPRETERS and len(argv) >= 3 and argv[1] == "-c":
            nested.append(" ".join(argv[2:]))
        elif argv[0].rsplit("/", 1)[-1] in _NESTED_SCAN_BINARIES:
            nested.append(" ".join(argv))
    else:
        return []

    _compile_script_patterns()
    lineno = getattr(call_node, "lineno", 1)
    col_offset = getattr(call_node, "col_offset", 0)
    matches: list[AstMatch] = []
    for cmd in nested:
        for compiled_re, pattern in _SCRIPT_PATTERNS["bash"]:
            for m in compiled_re.finditer(cmd):
                matches.append(
                    AstMatch(
                        rule_id=f"ast.python.nested.{pattern.rule_id}",
                        reason=f"{pattern.reason} (via subprocess argv)",
                        severity=pattern.severity,
                        lineno=lineno,
                        col_offset=col_offset,
                        matched_text=m.group().strip(),
                        suggestion=pattern.suggestion,
                    )
                )
    return matches


def _make_ast_match(call_node: ast.Call, pattern: _PatternDef, code: str) -> AstMatch:
    """Create an AstMatch from an AST Call node and pattern."""
    start_lineno = getattr(call_node, "lineno", 1)
    start_col = getattr(call_node, "col_offset", 0)

    # Extract matched text
    lines = code.splitlines()
    if start_lineno - 1 < len(lines):
        matched = lines[start_lineno - 1].strip()
    else:
        matched = code[max(0, start_col):start_col + 60]

    return AstMatch(
        rule_id=pattern.rule_id,
        reason=pattern.reason,
        severity=pattern.severity,
        lineno=start_lineno,
        col_offset=start_col,
        matched_text=matched,
        suggestion=pattern.suggestion,
    )


def check_ast(code: str, language: str = "python") -> list[AstMatch]:
    """Check code for dangerous patterns using AST (Python) or regex (other).

    Args:
        code: The script body to analyze.
        language: One of ``python``, ``bash``, ``javascript``, ``typescript``, ``ruby``.

    Returns:
        List of matches; empty list means no dangerous patterns found.
    """
    if language == "python":
        return _check_ast_python(code)

    _compile_script_patterns()
    lang_patterns = _SCRIPT_PATTERNS.get(language, [])
    matches: list[AstMatch] = []

    for compiled_re, pattern in lang_patterns:
        for m in compiled_re.finditer(code):
            lineno = code[: m.start()].count("\n") + 1
            matches.append(
                AstMatch(
                    rule_id=pattern.rule_id,
                    reason=pattern.reason,
                    severity=pattern.severity,
                    lineno=lineno,
                    col_offset=m.start(),
                    matched_text=m.group().strip(),
                    suggestion=pattern.suggestion,
                )
            )

    return matches
