"""Tests for the heredoc/inline script scanner."""

from __future__ import annotations

from app.heredoc_scanner import (
    check_nested_commands,
    extract_all,
    extract_command_substitutions,
    extract_eval_exec,
    extract_heredocs,
    extract_herestrings,
    extract_inline_scripts,
    has_heredoc_triggers,
)

# ── Tier 1: Trigger detection ───────────────────────────────────────────────

class TestHasHeredocTriggers:
    def test_python_inline(self):
        assert has_heredoc_triggers('python3 -c "print(1)"')

    def test_bash_inline(self):
        assert has_heredoc_triggers('bash -c "rm -rf /"')

    def test_heredoc_operator(self):
        assert has_heredoc_triggers("cat << EOF")
        assert has_heredoc_triggers("cat <<EOF")
        assert has_heredoc_triggers("cat <<-EOF")

    def test_eval_trigger(self):
        assert has_heredoc_triggers('eval "something"')

    def test_exec_trigger(self):
        assert has_heredoc_triggers('exec "something"')

    def test_herestring_trigger(self):
        assert has_heredoc_triggers("<<<")

    def test_dollar_paren_trigger(self):
        assert has_heredoc_triggers("$(echo hi)")

    def test_backtick_trigger(self):
        assert has_heredoc_triggers("`echo hi`")

    def test_no_false_positive(self):
        assert not has_heredoc_triggers("ls -la")
        assert not has_heredoc_triggers("git status")
        assert not has_heredoc_triggers("cd /tmp && echo hello")
        assert not has_heredoc_triggers("docker ps")
        assert not has_heredoc_triggers("cat /etc/passwd")


# ── Tier 2: Inline scripts ──────────────────────────────────────────────────

class TestExtractInlineScripts:
    def test_python_c(self):
        res = extract_inline_scripts('python3 -c "import os; os.remove(\'/tmp/x\')"')
        assert len(res) == 1
        assert "os.remove" in res[0].script
        assert res[0].language == "python"

    def test_bash_c(self):
        res = extract_inline_scripts('bash -c "rm -rf /data"')
        assert len(res) == 1
        assert "rm -rf" in res[0].script
        assert res[0].language == "bash"

    def test_ruby_e(self):
        res = extract_inline_scripts('ruby -e "puts 42"')
        assert len(res) == 1
        assert res[0].language == "ruby"

    def test_perl_e(self):
        res = extract_inline_scripts('perl -e "print 42"')
        assert len(res) == 1
        assert res[0].language == "perl"

    def test_node_e(self):
        res = extract_inline_scripts('node -e "console.log(42)"')
        assert len(res) == 1
        assert res[0].language == "javascript"

    def test_php_r(self):
        res = extract_inline_scripts('php -r "echo 42;"')
        assert len(res) == 1
        assert res[0].language == "php"

    def test_sh_c(self):
        res = extract_inline_scripts('sh -c "echo hi"')
        assert len(res) == 1
        assert res[0].language == "bash"

    def test_zsh_c(self):
        res = extract_inline_scripts('zsh -c "echo hi"')
        assert len(res) == 1
        assert res[0].language == "bash"

    def test_no_false_positive(self):
        res = extract_inline_scripts("ls -la")
        assert len(res) == 0

    def test_single_quotes(self):
        res = extract_inline_scripts("python3 -c 'import os; os.remove(\"/tmp/x\")'")
        assert len(res) == 1
        assert "os.remove" in res[0].script

    def test_no_close_quote_takes_rest(self):
        res = extract_inline_scripts('python3 -c "import os; print(1)')
        assert len(res) == 1
        assert "import os" in res[0].script

    def test_multiple_interpreters(self):
        res = extract_inline_scripts('python3 -c "a=1" && bash -c "b=2"')
        assert len(res) == 2


# ── Tier 2: eval/exec ──────────────────────────────────────────────────────

class TestExtractEvalExec:
    def test_eval(self):
        res = extract_eval_exec('eval "rm -rf /"')
        assert len(res) == 1
        assert "rm -rf" in res[0].script

    def test_exec(self):
        res = extract_eval_exec('exec "dangerous_thing"')
        assert len(res) == 1

    def test_no_false_positive(self):
        res = extract_eval_exec("ls -la")
        assert len(res) == 0


# ── Tier 2: Heredocs ───────────────────────────────────────────────────────

class TestExtractHeredocs:
    def test_basic_heredoc(self):
        cmd = "cat << EOF\nrm -rf /data\nEOF"
        res = extract_heredocs(cmd)
        assert len(res) == 1
        assert "rm -rf" in res[0].script

    def test_no_space_after_ll(self):
        cmd = "cat <<EOF\nrm -rf /data\nEOF"
        res = extract_heredocs(cmd)
        assert len(res) == 1

    def test_tab_dash(self):
        cmd = "cat <<-EOF\nrm -rf /data\nEOF"
        res = extract_heredocs(cmd)
        assert len(res) == 1

    def test_quoted_delimiter(self):
        cmd = "cat <<'EOF'\nrm -rf /data\nEOF"
        res = extract_heredocs(cmd)
        assert len(res) == 1

    def test_double_quoted_delimiter(self):
        cmd = 'cat <<"EOF"\nrm -rf /data\nEOF'
        res = extract_heredocs(cmd)
        assert len(res) == 1

    def test_no_false_positive(self):
        cmd = "ls -la"
        res = extract_heredocs(cmd)
        assert len(res) == 0

    def test_multiple_heredocs(self):
        cmd = "cat << EOF\nrm -rf /data\nEOF\necho hi\ncat << EOS\nrm -rf /tmp\nEOS"
        res = extract_heredocs(cmd)
        assert len(res) >= 1  # at least one extraction


# ── Tier 2: Here-strings ──────────────────────────────────────────────────

class TestExtractHereStrings:
    def test_basic(self):
        res = extract_herestrings('<<< "hello world"')
        assert len(res) == 1
        assert "hello world" in res[0].script

    def test_single_quotes(self):
        res = extract_herestrings("<<< 'hello world'")
        assert len(res) == 1

    def test_no_false_positive(self):
        res = extract_herestrings("ls -la")
        assert len(res) == 0


# ── Tier 2: Command substitutions ─────────────────────────────────────────

class TestExtractCommandSubstitutions:
    def test_dollar_paren(self):
        res = extract_command_substitutions("$(echo hello)")
        assert len(res) == 1
        assert "echo hello" in res[0].script

    def test_backtick(self):
        res = extract_command_substitutions("`echo hello`")
        assert len(res) == 1
        assert "echo hello" in res[0].script

    def test_nested_dollar_paren(self):
        res = extract_command_substitutions("$(echo $(git rev-parse HEAD))")
        assert len(res) == 1
        # The inner content is the full expression
        assert "git rev-parse" in res[0].script

    def test_no_false_positive(self):
        res = extract_command_substitutions("ls -la")
        assert len(res) == 0


# ── Extract all ────────────────────────────────────────────────────────────

class TestExtractAll:
    def test_deduplication(self):
        res = extract_all("python3 -c 'print(1)'")
        assert len(res) == 1  # no duplicates from multiple extractors

    def test_multiple_sources(self):
        res = extract_all('python3 -c "import os" && bash -c "rm -rf /"')
        assert len(res) >= 2

    def test_empty(self):
        res = extract_all("ls -la")
        assert len(res) == 0


# ── Recursive scanning ────────────────────────────────────────────────────

class TestCheckNestedCommands:
    def test_bash_rm_rf(self):
        findings = check_nested_commands('bash -c "rm -rf /data"')
        assert len(findings) > 0
        assert any("rm-rf" in f.pattern_name for f in findings)

    def test_eval_rm_rf_root(self):
        findings = check_nested_commands('eval "rm -rf /"')
        assert len(findings) > 0
        # rm-rf-root is critical (targeting root)
        assert any("rm-rf-root" in f.pattern_name for f in findings)

    def test_heredoc_rm_rf(self):
        findings = check_nested_commands("cat << EOF\nrm -rf /data\nEOF")
        assert len(findings) > 0
        # origin info should be in reason
        assert any("heredoc" in f.reason for f in findings)

    def test_harmless(self):
        findings = check_nested_commands("ls -la")
        assert len(findings) == 0

    def test_harmless_python_print(self):
        findings = check_nested_commands('python3 -c "print(42)"')
        assert len(findings) == 0  # no destructive patterns in print(42)

    def test_all_origins_tagged(self):
        """Each finding should have its extraction origin in the reason."""
        findings = check_nested_commands('eval "rm -rf /tmp"')
        assert all("eval_call" in f.reason for f in findings)

    def test_no_triggers_no_work(self):
        """If no triggers detected, check_nested_commands returns fast."""
        findings = check_nested_commands("git commit -m 'fix bug'")
        assert len(findings) == 0


# ── Policy integration ────────────────────────────────────────────────────

class TestPolicyIntegration:
    def test_heredoc_destructive_blocked(self):
        from app.command_policy import evaluate_command_policy

        result = evaluate_command_policy(
            "cat << EOF\nrm -rf /data\nEOF",
            mode="enforce",
            profile="default",
        )
        assert not result.allowed
        assert "heredoc" in result.reason.lower() or "destructive" in result.reason.lower()

    def test_eval_destructive_blocked(self):
        from app.command_policy import evaluate_command_policy

        result = evaluate_command_policy(
            'eval "rm -rf /"',
            mode="enforce",
            profile="default",
        )
        assert not result.allowed

    def test_harmless_not_blocked(self):
        from app.command_policy import evaluate_command_policy

        result = evaluate_command_policy("ls -la", mode="enforce", profile="default")
        assert result.allowed

    def test_audit_mode_logs_block(self):
        """Audit mode should not block but report what would happen."""
        from app.command_policy import evaluate_command_policy

        result = evaluate_command_policy(
            "cat << EOF\nrm -rf /data\nEOF",
            mode="audit",
            profile="default",
        )
        assert result.allowed  # audit mode never blocks
        assert "AUDIT_ONLY" in result.reason
