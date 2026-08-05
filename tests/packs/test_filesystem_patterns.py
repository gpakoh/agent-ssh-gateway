"""Regression tests for filesystem pack rm-rf false-positive bug (app/packs/filesystem.py).

rm-rf (and its siblings rm-rf-root/rm-rf-sensitive/rm-recursive) detected
the invoked command via a bare `\\brm\\b` word search over the raw command
string. Since that has no notion of "this word is the invoked program"
vs "this word is just an argument somewhere in the string", it matched:
  - the "rm" subcommand argument of an unrelated command, e.g.
    "docker rm --force x" (docker's own rm, not the filesystem rm binary);
  - "rm -rf" appearing inside a quoted string, e.g. echo 'rm -rf /'
    (never actually invoked).

It also never detected the recursive+force flags when given as two
separate arguments (rm -r -f, as opposed to the combined rm -rf).

Fixed by anchoring rm to an actual command-start position (start of
string, or right after &&/;/|, with optional sudo/absolute-path prefix)
and detecting -r/-f as independent lookaheads so they're caught whether
combined or given separately.
"""

from __future__ import annotations

from app.packs.registry import build_registry


class TestRmRfFalsePositives:
    def test_docker_rm_force_is_not_filesystem_rm_rf(self):
        r = build_registry()
        matches = r.evaluate_pack("filesystem", "docker rm --force x")
        assert matches == []

    def test_docker_rm_short_force_is_not_filesystem_match(self):
        r = build_registry()
        matches = r.evaluate_pack("filesystem", "docker rm -f x")
        assert matches == []

    def test_rm_rf_inside_quoted_string_is_not_a_real_invocation(self):
        r = build_registry()
        matches = r.evaluate_pack("filesystem", "echo 'rm -rf /'")
        assert matches == []


class TestRmRfTruePositives:
    def test_plain_rm_rf_detected(self):
        r = build_registry()
        names = [m.pattern_name for m in r.evaluate_pack("filesystem", "rm -rf ./dir")]
        assert "rm-rf" in names

    def test_sudo_rm_fr_detected(self):
        r = build_registry()
        names = [m.pattern_name for m in r.evaluate_pack("filesystem", "sudo rm -fr ./dir")]
        assert "rm-rf" in names

    def test_rm_after_compound_command_detected(self):
        """A real rm -r -f after && must still be caught even though it's
        not at the start of the string, and even with separated flags."""
        r = build_registry()
        names = [
            m.pattern_name
            for m in r.evaluate_pack("filesystem", "docker ps && rm -r -f ./dir")
        ]
        assert "rm-rf" in names

    def test_absolute_path_rm_detected(self):
        r = build_registry()
        names = [
            m.pattern_name for m in r.evaluate_pack("filesystem", "/usr/bin/rm -rf ./dir")
        ]
        assert "rm-rf" in names

    def test_rm_rf_root_still_detected(self):
        r = build_registry()
        names = [m.pattern_name for m in r.evaluate_pack("filesystem", "rm -rf /")]
        assert "rm-rf-root" in names

    def test_rm_rf_sensitive_still_detected(self):
        r = build_registry()
        names = [m.pattern_name for m in r.evaluate_pack("filesystem", "rm -rf /etc")]
        assert "rm-rf-sensitive" in names
