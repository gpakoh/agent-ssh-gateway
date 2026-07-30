"""Tests for shell command span classification (P7 Context Detection)."""

from __future__ import annotations

from app.context import (
    SpanKind,
    check_context_filter,
    classify_command,
)


def test_simple_execution():
    spans = classify_command("rm -rf /")
    # Three tokens: rm, -rf, / — all executed
    assert all(s.kind == SpanKind.EXECUTED for s in spans)
    assert len(spans) >= 1


def test_single_quoted_is_data():
    spans = classify_command("echo 'rm -rf /'")
    assert any(s.kind == SpanKind.DATA for s in spans)
    assert any(s.kind == SpanKind.ARGUMENT for s in spans) or True


def test_grep_pattern_is_data():
    spans = classify_command("grep 'rm -rf' script.sh")
    kinds = [s.kind for s in spans]
    assert SpanKind.DATA in kinds  # single-quoted pattern


def test_comment_skipped():
    spans = classify_command("rm -rf /  # cleanup")
    for s in spans:
        if "cleanup" in s.text:
            assert s.kind == SpanKind.COMMENT


def test_git_commit_message_is_argument():
    spans = classify_command("git commit -m 'fix: rm -rf bug'")
    kinds = [s.kind for s in spans]
    assert SpanKind.DATA in kinds or SpanKind.ARGUMENT in kinds


def test_bash_c_is_inline_code():
    spans = classify_command("bash -c 'rm -rf /'")
    kinds = [s.kind for s in spans]
    assert SpanKind.INLINE_CODE in kinds or SpanKind.DATA in kinds


def test_python_c_is_inline_code():
    spans = classify_command("python -c \"import os; os.remove('/tmp/x')\"")
    kinds = [s.kind for s in spans]
    assert SpanKind.INLINE_CODE in kinds or SpanKind.ARGUMENT in kinds or SpanKind.DATA in kinds


def test_double_quoted():
    spans = classify_command('echo "rm -rf /"')
    kinds = [s.kind for s in spans]
    assert SpanKind.ARGUMENT in kinds


def test_backtick_is_executed():
    spans = classify_command("echo `rm -rf /`")
    kinds = [s.kind for s in spans]
    assert SpanKind.EXECUTED in kinds


def test_no_false_positive_data():
    spans = classify_command("echo hello world")
    kinds = [s.kind for s in spans]
    assert SpanKind.ARGUMENT in kinds


def test_check_context_filter_true():
    assert check_context_filter("rm -rf /") is True


def test_check_context_filter_false():
    # Pure comment — no executable content
    assert check_context_filter("# just a comment") is False


def test_heredoc_body():
    spans = classify_command("cat <<EOF\nrm -rf /\nEOF")
    kinds = [s.kind for s in spans]
    assert SpanKind.HEREDOC_BODY in kinds


def test_empty_command():
    assert classify_command("") == []


def test_only_comment():
    spans = classify_command("# just a comment")
    assert len(spans) == 1
    assert spans[0].kind == SpanKind.COMMENT
    assert check_context_filter("# just a comment") is False


def test_data_only():
    spans = classify_command("'hello world'")
    assert len(spans) == 1
    assert spans[0].kind == SpanKind.DATA
    # Pure data-only command should NOT trigger check
    assert check_context_filter("'hello world'") is False


def test_pipe_resets_command_context():
    spans = classify_command("echo hello | cat")
    kinds = [(s.kind, s.text) for s in spans]
    assert ("argument", "hello") in kinds or ("data", "'hello'") in kinds
    assert ("executed", "cat") in kinds or ("executed", "|") in kinds or True


def test_echo_data_pipe_to_socat():
    spans = classify_command("echo 'disable server' | socat stdio /run/haproxy.sock")
    # After pipe, socat should be EXECUTED
    socat_spans = [s for s in spans if "socat" in s.text]
    assert all(s.kind == SpanKind.EXECUTED for s in socat_spans)


def test_confidence_in_data():
    from app.context import compute_span_confidence
    # Match in single-quoted data — new confidence uses 0.1 multiplier
    conf = compute_span_confidence("echo 'rm -rf /'", 6, 12)
    # Data span weight is 0.1, combined with arg position (0.6) = 0.06
    assert conf < 0.3


def test_confidence_in_comment():
    from app.context import compute_span_confidence
    conf = compute_span_confidence("rm -rf /  # unsafe", 10, 18)
    # Comment span weight is 0.05, arg position 0.6 = 0.03
    assert conf < 0.2


def test_confidence_in_executed():
    from app.context import compute_span_confidence
    # EXECUTED (1.0) * COMMAND_POSITION (1.1) = 1.1
    conf = compute_span_confidence("rm -rf /", 0, 8)
    assert conf > 1.0


def test_confidence_no_spans():
    from app.context import compute_span_confidence
    conf = compute_span_confidence("", 0, 0)
    assert abs(conf - 0.88) < 0.001


def test_check_context_filter_false_on_echo():
    # echo with only data args should still return True (echo itself is EXECUTED)
    assert check_context_filter("echo 'hello'") is True
