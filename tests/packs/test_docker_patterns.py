"""Regression tests for docker pack flag-detection bugs (app/packs/docker.py).

rm-force / rmi-force used to detect the "force" flag via a bare regex
substring search (`-[a-zA-Z0-9]*f|--force`) over the raw command string,
with no shell-argument-boundary awareness. Because the search has no
notion of where one argument ends and the next begins, any long option
whose name happens to contain "-f" as a substring — e.g. --format,
--filter — matched the same alternative as a real -f short flag.

Fixed by requiring proper token boundaries: a negative lookbehind before
the dash (so a match can't start mid-token, e.g. at the second dash of
"--format") and a trailing \\b (so the match can't extend into more word
characters, e.g. "...format" continuing past "-f").
"""

from __future__ import annotations

from app.packs.registry import build_registry


class TestDockerRmForceFlagBoundary:
    def test_format_flag_is_not_force(self):
        r = build_registry()
        matches = r.evaluate_pack("docker", "docker rm --format '{{.ID}}' x")
        assert "rm-force" not in [m.pattern_name for m in matches]

    def test_filter_flag_is_not_force(self):
        r = build_registry()
        matches = r.evaluate_pack("docker", "docker rm --filter status=exited x")
        assert "rm-force" not in [m.pattern_name for m in matches]

    def test_short_force_flag_is_force(self):
        r = build_registry()
        matches = r.evaluate_pack("docker", "docker rm -f x")
        assert "rm-force" in [m.pattern_name for m in matches]

    def test_long_force_flag_is_force(self):
        r = build_registry()
        matches = r.evaluate_pack("docker", "docker rm --force x")
        assert "rm-force" in [m.pattern_name for m in matches]

    def test_combined_short_flags_with_force_still_detected(self):
        """f can appear anywhere within a combined short-flag cluster."""
        r = build_registry()
        for cmd in ("docker rm -rf x", "docker rm -fr x", "docker rm -vf x"):
            matches = r.evaluate_pack("docker", cmd)
            assert "rm-force" in [m.pattern_name for m in matches], cmd

    def test_rmi_format_flag_is_not_force(self):
        r = build_registry()
        matches = r.evaluate_pack("docker", "docker rmi --format '{{.ID}}' x")
        assert "rmi-force" not in [m.pattern_name for m in matches]

    def test_rmi_short_force_flag_is_force(self):
        r = build_registry()
        matches = r.evaluate_pack("docker", "docker rmi -f x")
        assert "rmi-force" in [m.pattern_name for m in matches]
