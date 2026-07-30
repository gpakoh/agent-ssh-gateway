"""Tests for span-aware confidence scoring (P9)."""

from __future__ import annotations

from app.confidence import (
    ConfidenceSignal,
    compute_match_confidence,
    compute_span_confidence,
    has_execution_operators_nearby,
    is_command_position,
    signal_description,
    signal_weight,
)


def test_signal_weights():
    assert signal_weight(ConfidenceSignal.EXECUTED_SPAN) >= 1.0
    assert signal_weight(ConfidenceSignal.DATA_SPAN) < 0.5
    assert signal_weight(ConfidenceSignal.COMMENT_SPAN) < 0.1
    assert signal_weight(ConfidenceSignal.COMMAND_POSITION) > 1.0
    assert signal_weight(ConfidenceSignal.EXECUTION_OPERATORS_NEARBY) > 1.0


def test_signal_descriptions():
    desc = signal_description(ConfidenceSignal.EXECUTED_SPAN)
    assert isinstance(desc, str)
    assert len(desc) > 5


def test_unknown_signal():
    assert signal_description(ConfidenceSignal.UNKNOWN_SPAN) == "match context is ambiguous"
    assert signal_weight(ConfidenceSignal.UNKNOWN_SPAN) == 0.8


def test_is_command_position_start():
    assert is_command_position("rm -rf /", 0) is True


def test_is_command_position_after_pipe():
    assert is_command_position("echo foo | rm -rf /", 11) is True


def test_is_command_position_after_and():
    assert is_command_position("false && rm -rf /", 9) is True


def test_is_command_position_in_message():
    assert is_command_position("git commit -m 'rm'", 15) is False


def test_has_execution_operators_nearby_pipe():
    assert has_execution_operators_nearby("echo foo | rm -rf /", 11, 19) is True


def test_has_execution_operators_nearby_semicolon():
    assert has_execution_operators_nearby("false ; rm -rf /", 8, 16) is True


def test_has_execution_operators_nearby_none():
    assert has_execution_operators_nearby("echo 'rm -rf /'", 6, 14) is False


def test_has_execution_operators_nearby_backtick():
    assert has_execution_operators_nearby("echo `rm -rf /`", 6, 15) is True


def test_compute_span_confidence_executed():
    mult, signals = compute_span_confidence("rm -rf /", 0, 8)
    assert mult > 1.0  # EXECUTED + COMMAND_POSITION boost > 1.0
    assert ConfidenceSignal.EXECUTED_SPAN in signals


def test_compute_span_confidence_pipe_boost():
    mult, signals = compute_span_confidence("echo foo | rm -rf /", 11, 19)
    assert mult > 1.0  # EXECUTED + OPERATORS + CMD_POSITION boost


def test_compute_match_confidence_executed():
    final, signals = compute_match_confidence(0.7, "rm -rf /", 0, 8)
    # 0.7 * 1.0 (executed) * 1.1 (command pos) = 0.77
    assert final > 0.7
    assert final <= 1.0


def test_compute_match_confidence_data():
    final, signals = compute_match_confidence(0.7, "echo 'rm -rf /'", 6, 14)
    # 0.7 * 0.1 (data) * 0.6 (arg position) = 0.042
    assert final < 0.3


def test_compute_match_confidence_comment():
    final, signals = compute_match_confidence(0.7, "rm -rf /  # unsafe", 10, 18)
    # 0.7 * 0.05 (comment) * 0.6 (arg position) = 0.021
    assert final < 0.2


def test_compute_match_confidence_empty():
    final, signals = compute_match_confidence(0.5, "", 0, 0)
    assert 0.0 <= final <= 1.0


def test_has_operators_unicode():
    """Should not panic with multi-byte UTF-8 chars."""
    result = has_execution_operators_nearby("echo café | rm -rf /", 14, 22)
    assert result is True


def test_is_command_position_empty_prefix():
    assert is_command_position("rm -rf /", 0) is True


def test_compute_span_confidence_empty():
    mult, signals = compute_span_confidence("", 0, 0)
    assert abs(mult - 0.88) < 0.001  # UNKNOWN_SPAN (0.8) * COMMAND_POSITION (1.1)
    assert ConfidenceSignal.UNKNOWN_SPAN in signals
