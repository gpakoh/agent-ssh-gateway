"""Tests for AST-based pattern matching (P8)."""

from __future__ import annotations

from app.ast_matcher import (
    MatchSeverity,
    check_ast,
)


def test_os_remove():
    matches = check_ast("import os\nos.remove('/tmp/x')")
    assert len(matches) == 1
    assert matches[0].rule_id == "ast.python.os_remove"
    assert matches[0].severity == MatchSeverity.HIGH
    assert matches[0].lineno == 2


def test_shutil_rmtree():
    matches = check_ast("import shutil\nshutil.rmtree('/tmp/x')")
    assert len(matches) == 1
    assert matches[0].rule_id == "ast.python.shutil_rmtree"
    assert matches[0].severity == MatchSeverity.CRITICAL


def test_os_system():
    matches = check_ast("import os\nos.system('rm -rf /')")
    assert len(matches) >= 1
    assert matches[0].rule_id == "ast.python.os_system"


def test_subprocess_run():
    matches = check_ast("import subprocess\nsubprocess.run(['rm', '-rf', '/'])")
    assert len(matches) >= 1
    sub = [m for m in matches if m.rule_id == "ast.python.subprocess_run"]
    assert len(sub) == 1
    assert sub[0].severity == MatchSeverity.MEDIUM


def test_subprocess_run_nested_rm_rf_detected():
    matches = check_ast("import subprocess\nsubprocess.run(['rm', '-rf', '/'])")
    nested = [m for m in matches if m.rule_id == "ast.python.nested.ast.bash.rm_rf"]
    assert len(nested) == 1
    assert nested[0].severity == MatchSeverity.CRITICAL


def test_subprocess_run_sh_c_payload_scanned():
    matches = check_ast("import subprocess\nsubprocess.run(['sh', '-c', 'rm -rf /'])")
    nested = [m for m in matches if m.rule_id == "ast.python.nested.ast.bash.rm_rf"]
    assert len(nested) == 1
    assert nested[0].severity == MatchSeverity.CRITICAL


def test_subprocess_run_shell_true_string_scanned():
    matches = check_ast("import subprocess\nsubprocess.run('rm -rf /', shell=True)")
    nested = [m for m in matches if m.rule_id == "ast.python.nested.ast.bash.rm_rf"]
    assert len(nested) == 1
    assert nested[0].severity == MatchSeverity.CRITICAL


def test_subprocess_run_benign_argv_not_scanned():
    matches = check_ast("import subprocess\nsubprocess.run(['echo', 'rm -rf /'])")
    nested = [m for m in matches if m.rule_id.startswith("ast.python.nested.")]
    assert nested == []


def test_from_import():
    matches = check_ast("from shutil import rmtree\nrmtree('/tmp')")
    assert len(matches) == 1
    assert matches[0].rule_id == "ast.python.shutil_rmtree"


def test_os_remove_from_import():
    matches = check_ast("from os import remove\nremove('/tmp/x')")
    assert len(matches) == 1
    assert matches[0].rule_id == "ast.python.os_remove"


def test_pathlib_unlink():
    matches = check_ast("from pathlib import Path\nPath('/tmp/x').unlink()")
    assert len(matches) >= 1
    # Should find pathlib_unlink rule
    found = any("unlink" in m.rule_id for m in matches)
    assert found, f"Expected unlink rule, got: {[m.rule_id for m in matches]}"


def test_syntax_error():
    """Syntax errors should not crash and return empty matches."""
    matches = check_ast("import os\nos.remove( unfinishe")
    assert len(matches) == 0


def test_no_patterns():
    """Safe code should return no matches."""
    matches = check_ast("print('hello')\nx = 1 + 2")
    assert len(matches) == 0


def test_bash_rm_rf():
    matches = check_ast("rm -rf /var/log", language="bash")
    assert len(matches) >= 1
    assert "rm_rf" in matches[0].rule_id


def test_bash_dd():
    matches = check_ast("dd if=/dev/zero of=/dev/sda bs=1M", language="bash")
    assert len(matches) >= 1
    assert "dd_destructive" in matches[0].rule_id


def test_javascript_fs_rmsync():
    matches = check_ast("fs.rmSync('/data', {recursive: true})", language="javascript")
    assert len(matches) >= 1
    assert "fs_rmsync" in matches[0].rule_id


def test_javascript_exec():
    matches = check_ast("child_process.execSync('rm -rf /')", language="javascript")
    assert len(matches) >= 1
    assert "execsync" in matches[0].rule_id


def test_ruby_fileutils():
    matches = check_ast("FileUtils.rm_rf('/data')", language="ruby")
    assert len(matches) >= 1
    assert "fileutils_rm_rf" in matches[0].rule_id


def test_empty_code():
    matches = check_ast("")
    assert len(matches) == 0


def test_multiple_matches():
    matches = check_ast("import os\nos.remove('/a')\nos.system('rm -rf /')\nos.unlink('/b')")
    assert len(matches) >= 3
    rule_ids = {m.rule_id for m in matches}
    assert "ast.python.os_remove" in rule_ids
    assert "ast.python.os_system" in rule_ids
    assert "ast.python.os_unlink" in rule_ids


def test_popen():
    matches = check_ast("import subprocess\nsubprocess.Popen(['ls'])", language="python")
    assert len(matches) >= 1
    assert matches[0].rule_id == "ast.python.subprocess_popen"
