"""Tests for opencode_runner_wrapper.build_opencode_args — pure argv logic.

Deliberately kept out of test_opencode_runner_wrapper.py: that file's
module-level pytestmark skips everything when no real opencode binary is
present, even tests (like this one) that never touch the binary at all —
which is exactly how this bug went unnoticed.
"""

from __future__ import annotations

from scripts.opencode_runner_wrapper import build_opencode_args


class TestBuildOpencodeArgs:
    """Regression: this used `resolved_cmd.split()[1:]`, plain whitespace
    splitting with no concept of quoting. The wrapper's own default,
    auto-generated command embeds a quoted, multi-word prompt argument —
    exactly the string every real (non---command-override) invocation
    produces — and naive .split() shreds it into dozens of disjoint argv
    tokens instead of passing it through as one argument to the opencode
    binary.
    """

    def test_quoted_multiword_argument_stays_one_token(self):
        resolved_cmd = (
            'opencode run --never-ask "Read .ai-bridge/tasks/x/current-plan.md '
            'and execute the plan. Save diff to '
            '.ai-bridge/tasks/x/implementation-diff.patch."'
        )
        args = build_opencode_args(resolved_cmd)
        assert args[0] == "run"
        assert args[1] == "--never-ask"
        assert len(args) == 3, f"expected 3 args, got {len(args)}: {args}"
        assert args[2] == (
            "Read .ai-bridge/tasks/x/current-plan.md and execute the plan. "
            "Save diff to .ai-bridge/tasks/x/implementation-diff.patch."
        )

    def test_simple_command_drops_program_name(self):
        assert build_opencode_args("opencode run tests") == ["run", "tests"]
