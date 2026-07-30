"""Tests for Simulate Mode (P10) — command replay through policy engine."""

from __future__ import annotations

from app.simulate import (
    InputFormat,
    SimLimits,
    parse_commands,
    simulate,
)


def test_parse_plain_commands():
    cmds, stats = parse_commands("rm -rf /\necho hello\nls -la")
    assert len(cmds) == 3
    assert cmds[0].command == "rm -rf /"
    assert cmds[1].command == "echo hello"
    assert cmds[2].command == "ls -la"
    assert stats.commands_extracted == 3


def test_parse_empty_lines():
    cmds, stats = parse_commands("rm -rf /\n\n\necho hello\n")
    assert len(cmds) == 2
    assert stats.empty == 2  # two blank lines


def test_parse_comments_are_commands():
    """Comments are treated as plain commands in simulate mode."""
    cmds, stats = parse_commands("# just a comment\nrm -rf /")
    assert len(cmds) == 2


def test_parse_hook_json():
    text = '{"tool_name": "bash", "tool_input": {"command": "rm -rf /"}}'
    cmds, stats = parse_commands(text)
    assert len(cmds) == 1
    assert cmds[0].command == "rm -rf /"
    assert cmds[0].format == InputFormat.HOOK_JSON


def test_parse_hook_json_camelcase():
    text = '{"toolName": "bash", "toolInput": {"command": "echo hi"}}'
    cmds, stats = parse_commands(text)
    assert len(cmds) == 1
    assert cmds[0].command == "echo hi"


def test_parse_not_hook_json_falls_to_plain():
    text = '{"not": "hook"}'
    cmds, stats = parse_commands(text)
    assert len(cmds) == 1
    assert cmds[0].format == InputFormat.PLAIN


def test_parse_decision_log():
    import base64
    cmd_b64 = base64.b64encode(b"rm -rf /").decode()
    text = f"DCG_LOG_V1|1234567890|deny|{cmd_b64}"
    cmds, stats = parse_commands(text)
    assert len(cmds) == 1
    assert cmds[0].command == "rm -rf /"
    assert cmds[0].format == InputFormat.DECISION_LOG


def test_parse_decision_log_bad_base64():
    text = "DCG_LOG_V1|ts|deny|not-valid-base64!!!"
    cmds, stats = parse_commands(text)
    assert len(cmds) == 0
    assert stats.malformed >= 0


def test_max_lines_limit():
    text = "a\nb\nc\nd\ne"
    cmds, stats = parse_commands(text, SimLimits(max_lines=3))
    assert len(cmds) == 3
    assert stats.stopped_at_limit is True


def test_max_command_bytes():
    text = "rm"
    cmds, stats = parse_commands(text, SimLimits(max_command_bytes=1))
    assert len(cmds) == 0


def test_simulate_audit_mode():
    result = simulate("rm -rf /\necho hello", mode="audit", profile="default")
    assert "results" in result
    assert "stats" in result
    assert len(result["results"]) == 2
    # Audit mode — both commands allowed
    assert result["results"][0]["allowed"] is True
    assert "AUDIT_ONLY" in result["results"][0]["reason"]


def test_simulate_enforce_mode():
    result = simulate("rm -rf /", mode="enforce", profile="readonly")
    assert result["results"][0]["allowed"] is False


def test_simulate_stats():
    result = simulate("rm -rf /\n\n# comment", mode="audit")
    stats = result["stats"]
    assert stats["commands_extracted"] >= 1
    assert stats["total_lines"] == 3


def test_simulate_findings():
    result = simulate("rm -rf /", mode="audit")
    findings = result["results"][0]["findings"]
    assert len(findings) >= 1


def test_simulate_empty():
    result = simulate("", mode="audit")
    assert len(result["results"]) == 0
    assert result["stats"]["total_lines"] == 0
