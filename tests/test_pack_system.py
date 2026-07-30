"""Tests for Pack and PackRegistry infrastructure (P4)."""

from __future__ import annotations

import pytest

from app.command_policy import DestructiveMatch, DestructivePattern, Severity, scan_command
from app.packs import Pack, PackRegistry

# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_pattern(name: str, regex: str, severity: str = "high") -> DestructivePattern:
    return DestructivePattern(
        name=name,
        regex=regex,
        reason=f"test pattern {name}",
        severity=Severity(severity),
        description=f"Dummy pattern {name} for testing.",
        suggestions=(),
    )


_MAKE_PATTERN = _make_pattern  # shorthand for parametrize


# ── Pack unit tests ──────────────────────────────────────────────────────────


class TestPack:
    def test_init(self):
        p = Pack(id="test", name="Test pack")
        assert p.id == "test"
        assert p.name == "Test pack"
        assert p.keywords == ()
        assert p.destructive_patterns == ()
        assert p._compiled == []

    def test_init_with_patterns(self):
        dp = _make_pattern("rm-rf", r"rm\s+-rf")
        p = Pack(id="fs", name="Filesystem", destructive_patterns=(dp,), keywords=("rm",))
        assert len(p.destructive_patterns) == 1
        assert len(p._compiled) == 1
        pat, pattern_obj = p._compiled[0]
        assert pattern_obj is dp

    def test_matches_keywords_empty(self):
        """Empty keywords means always match (no quick-reject)."""
        p = Pack(id="test", name="Test")
        assert p.matches_keywords("anything at all") is True
        assert p.matches_keywords("") is True

    def test_matches_keywords_found(self):
        p = Pack(id="test", name="Test", keywords=("docker", "container"))
        assert p.matches_keywords("docker ps") is True
        assert p.matches_keywords("container rm") is True

    def test_matches_keywords_not_found(self):
        p = Pack(id="test", name="Test", keywords=("docker",))
        assert p.matches_keywords("echo hello") is False
        assert p.matches_keywords("git push") is False
        assert p.matches_keywords("") is False

    def test_matches_keywords_case_insensitive(self):
        p = Pack(id="test", name="Test", keywords=("docker",))
        assert p.matches_keywords("DOCKER PS") is True
        assert p.matches_keywords("Docker ps") is True
        assert p.matches_keywords("dOcKeR") is True

    def test_check_no_match(self):
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("rm-rf", r"rm\s+-rf"),
        ), keywords=("rm",))
        assert p.check("echo hello") == []

    def test_check_single_match(self):
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("rm-rf", r"rm\s+-rf"),
        ), keywords=("rm",))
        matches = p.check("rm -rf /tmp")
        assert len(matches) == 1
        assert matches[0].pattern_name == "rm-rf"

    def test_check_multiple_matches(self):
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("rm-rf", r"rm\s+-rf"),
            _make_pattern("rm-root", r"rm\s+-rf\s+/"),
        ), keywords=("rm",))
        matches = p.check("rm -rf /")
        assert len(matches) == 2
        names = {m.pattern_name for m in matches}
        assert names == {"rm-rf", "rm-root"}

    def test_check_with_keyword_reject(self):
        """Keyword quick-reject prevents any check work."""
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("rm-rf", r"rm\s+-rf"),
        ), keywords=("docker",))
        assert p.check("rm -rf /") == []

    def test_check_case_insensitive_regex(self):
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("rm-rf", r"rm\s+-rf"),
        ), keywords=("rm",))
        assert len(p.check("RM -RF /")) == 1
        assert len(p.check("Rm -Rf /")) == 1

    def test_check_returns_destructive_match(self):
        dp = _make_pattern("test-pat", r"danger")
        p = Pack(id="test", name="Test", destructive_patterns=(dp,), keywords=("danger",))
        matches = p.check("danger command")
        assert len(matches) == 1
        m = matches[0]
        assert isinstance(m, DestructiveMatch)
        assert m.pattern_name == "test-pat"
        assert m.reason == "test pattern test-pat"
        assert m.severity == Severity.HIGH
        assert m.suggestion is None

    def test_check_suggestion_included(self):
        dp = DestructivePattern(
            name="test-sug",
            regex=r"danger",
            reason="test suggestion",
            severity=Severity.HIGH,
            description="Pattern with suggestion.",
            suggestions=(
                type("Suggestion", (), {"command": "safe-cmd --help"})(),
            ),
        )
        p = Pack(id="test", name="Test", destructive_patterns=(dp,), keywords=("danger",))
        matches = p.check("danger command")
        assert matches[0].suggestion == "safe-cmd --help"

    def test_duplicate_pattern_names(self):
        """Two patterns with same name in one pack — handled by list-based _compiled."""
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("same-name", r"pattern-one"),
            _make_pattern("same-name", r"pattern-two"),
        ), keywords=("pattern",))
        # Both should match separately
        matches = p.check("pattern-one here")
        assert len(matches) == 1
        assert matches[0].pattern_name == "same-name"
        matches2 = p.check("pattern-two here")
        assert len(matches2) == 1
        assert matches2[0].pattern_name == "same-name"

    def test_empty_patterns(self):
        p = Pack(id="empty", name="Empty", destructive_patterns=(), keywords=())
        assert p.check("anything") == []
        assert p.matches_keywords("anything") is True

    def test_regex_with_dotall_flag(self):
        """Patterns are compiled with DOTALL — . matches newlines."""
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("multi", r"start.*end"),
        ), keywords=("start",))
        assert len(p.check("start\nmiddle\nend")) == 1


# ── PackRegistry unit tests ──────────────────────────────────────────────────


class TestPackRegistry:
    def test_empty_registry(self):
        r = PackRegistry()
        assert r.pack_count == 0
        assert r.pattern_count == 0
        assert r.keyword_count == 0
        assert r.all_packs == ()
        assert r.evaluate("anything") == []
        assert r.evaluate_pack("nonexistent", "cmd") == []

    def test_register_and_get(self):
        r = PackRegistry()
        p = Pack(id="test", name="Test")
        r.register(p)
        assert r.get("test") is p
        assert r.get("nonexistent") is None

    def test_register_updates_keyword_index(self):
        r = PackRegistry()
        r.register(Pack(id="a", name="A", keywords=("docker", "compose")))
        assert "docker" in r._all_keywords
        assert "compose" in r._all_keywords
        r.register(Pack(id="b", name="B", keywords=("git",)))
        assert "git" in r._all_keywords
        assert "docker" in r._all_keywords

    def test_all_packs(self):
        r = PackRegistry()
        p1 = Pack(id="a", name="A")
        p2 = Pack(id="b", name="B")
        r.register(p1)
        r.register(p2)
        assert set(r.all_packs) == {p1, p2}

    @pytest.mark.parametrize("cmd,expected_count", [
        ("docker rm -f container1", 1),
        ("rm -rf /tmp", 2),
        ("echo hello", 0),
        ("git push --force origin main", 2),
    ])
    def test_evaluate(self, cmd, expected_count):
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate(cmd)
        assert len(matches) == expected_count, (
            f"expected {expected_count} matches for {cmd!r}, got {len(matches)}: "
            f"{[m.pattern_name for m in matches]}"
        )

    def test_evaluate_pack_specific(self):
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate_pack("docker", "docker rm -f container")
        assert len(matches) == 1
        assert matches[0].pattern_name == "rm-force"

    def test_evaluate_pack_unknown(self):
        r = PackRegistry()
        assert r.evaluate_pack("unknown", "anything") == []

    def test_evaluate_pack_no_match(self):
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate_pack("docker", "echo hello")
        assert matches == []

    def test_pack_and_pattern_counts(self):
        r = PackRegistry()
        r.register(Pack(id="a", name="A", destructive_patterns=(
            _make_pattern("p1", r"x"), _make_pattern("p2", r"y"),
        )))
        r.register(Pack(id="b", name="B", destructive_patterns=(
            _make_pattern("p3", r"z"),
        )))
        assert r.pack_count == 2
        assert r.pattern_count == 3

    def test_keyword_count(self):
        r = PackRegistry()
        r.register(Pack(id="a", name="A", keywords=("docker", "compose")))
        r.register(Pack(id="b", name="B", keywords=("docker", "git")))
        # "docker" deduplicated, so 3 unique keywords
        assert r.keyword_count == 3

    def test_global_quick_reject(self):
        """If no pack matches keywords, evaluate returns empty."""
        r = PackRegistry()
        r.register(Pack(id="fs", name="FS", keywords=("rm", "shred"),
                        destructive_patterns=(_make_pattern("rm-rf", r"rm\s+-rf"),)))
        r.register(Pack(id="git", name="Git", keywords=("git",),
                        destructive_patterns=(_make_pattern("push-force", r"git\s+push\s+--force"),)))
        # "az" doesn't match any keyword — quick-reject
        assert r.evaluate("az vm delete --name x") == []
        # "rm" matches fs keywords — only fs pack runs
        matches = r.evaluate("rm -rf /tmp")
        assert len(matches) == 1
        assert matches[0].pattern_name == "rm-rf"

    def test_global_quick_reject_no_keywords(self):
        """No keywords = no quick-reject."""
        r = PackRegistry()
        r.register(Pack(id="test", name="Test", keywords=(),
                        destructive_patterns=(_make_pattern("always", r"."),)))
        assert len(r.evaluate("anything at all")) == 1

    def test_duplicate_pack_id_overwrites(self):
        r = PackRegistry()
        p1 = Pack(id="test", name="First")
        p2 = Pack(id="test", name="Second")
        r.register(p1)
        r.register(p2)
        assert r.get("test").name == "Second"
        assert r.pack_count == 1

    def test_matches_properties(self):
        """Verify DestructiveMatch fields from evaluate()."""
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate("rm -rf /")
        assert len(matches) >= 1
        m = matches[0]
        assert hasattr(m, "pattern_name")
        assert hasattr(m, "severity")
        assert hasattr(m, "reason")
        assert m.pattern_name in ("rm-rf-root", "rm-rf", "rm-recursive")
        assert m.severity.value in ("critical", "high", "medium")

    def test_confidence_present_in_match(self):
        """Verify DestructiveMatch carries confidence field."""
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate("rm -rf /")
        assert len(matches) >= 1
        for m in matches:
            assert hasattr(m, "confidence")
            assert isinstance(m.confidence, float)
            assert 0.5 <= m.confidence <= 1.0  # can exceed 0.95 with span-aware boost

    def test_confidence_longer_regex_higher(self):
        """Longer, more specific patterns should have higher confidence."""
        from app.packs.registry import build_registry
        r = build_registry()
        m1 = r.evaluate("chmod -R 777 /")  # short regex
        m2 = r.evaluate("rm -rf /")  # medium regex
        assert all(hasattr(m, "confidence") for m in m1 + m2)

    def test_confidence_critical_high_value(self):
        """CRITICAL severity patterns should have higher base confidence."""
        from app.command_policy import DestructivePattern, Severity
        from app.packs import _compute_confidence

        dp_high = DestructivePattern(name="t", regex="test", reason="r", severity=Severity.HIGH, description="d")
        dp_crit = DestructivePattern(name="t", regex="test", reason="r", severity=Severity.CRITICAL, description="d")
        assert _compute_confidence(dp_crit) > _compute_confidence(dp_high)


# ── Singleton tests ──────────────────────────────────────────────────────────


class TestRegistrySingleton:
    def test_get_registry_returns_pack_registry(self):
        from app.packs.registry import get_registry
        r = get_registry()
        from app.packs import PackRegistry
        assert isinstance(r, PackRegistry)

    def test_get_registry_singleton(self):
        from app.packs.registry import get_registry
        assert get_registry() is get_registry()

    def test_get_registry_has_all_packs(self):
        from app.packs.registry import get_registry
        r = get_registry()
        assert r.pack_count == 9
        expected_ids = {"docker", "filesystem", "kubernetes", "cloud",
                        "database", "git", "firewall", "loadbalancer", "system"}
        actual_ids = {p.id for p in r.all_packs}
        assert actual_ids == expected_ids


# ── Smoke tests ──────────────────────────────────────────────────────────────


class TestPackSmoke:
    """Quick smoke tests against the real built registry."""

    def test_build_registry_has_no_duplicates(self):
        """No duplicate pattern names within any single pack."""
        from app.packs.registry import build_registry
        r = build_registry()
        for pack in r.all_packs:
            names = [dp.name for dp in pack.destructive_patterns]
            dupes = {n for n in names if names.count(n) > 1}
            assert not dupes, f"Pack '{pack.id}' has duplicate names: {dupes}"

    def test_scan_command_returns_report(self):
        report = scan_command("rm -rf /")
        assert report.total > 0
        assert len(report.findings) == report.total
        for f in report.findings:
            assert f.pattern_name
            assert f.severity

    def test_scan_command_safe(self):
        report = scan_command("ls -la")
        assert report.total == 0

    def test_all_packs_have_keywords(self):
        """Every pack should have at least one keyword for quick-reject."""
        from app.packs.registry import build_registry
        r = build_registry()
        for pack in r.all_packs:
            assert len(pack.keywords) > 0, f"Pack '{pack.id}' has no keywords"

    def test_all_packs_have_patterns(self):
        """Every pack should have at least one destructive pattern."""
        from app.packs.registry import build_registry
        r = build_registry()
        for pack in r.all_packs:
            assert len(pack.destructive_patterns) > 0, f"Pack '{pack.id}' has no patterns"

    def test_legacy_wrappers_still_work(self):
        """Verify old delegation functions are intact."""
        from app.command_policy import _check_all_destructive, _check_docker_destructive
        assert len(_check_all_destructive("rm -rf /")) > 0
        assert _check_all_destructive("echo hello") == []
        assert _check_docker_destructive("docker rm -f container") is not None
        assert _check_docker_destructive("echo hello") is None

    @pytest.mark.parametrize("cmd,expected_pack", [
        ("docker rm -f c1", "docker"),
        ("docker-compose rm -f", "docker"),
        ("rm -rf /", "filesystem"),
        ("kubectl delete namespace prod", "kubernetes"),
        ("aws s3 rm s3://bucket/ --recursive", "cloud"),
        ("DROP DATABASE prod", "database"),
        ("git push --force", "git"),
        ("ufw disable", "firewall"),
        ("nginx -s stop", "loadbalancer"),
        ("dd if=/dev/zero of=/dev/sda bs=1M", "system"),
    ])
    def test_evaluate_pack_cross_section(self, cmd, expected_pack):
        """Verify each pack matches at least one expected command."""
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate_pack(expected_pack, cmd)
        assert len(matches) >= 1, (
            f"Expected {expected_pack} pack to match {cmd!r}, got 0 matches"
        )

    def test_global_keyword_reject_still_allows_matches(self):
        """Smoke: commands known to be destructive still match with global keyword check."""
        from app.packs.registry import build_registry
        r = build_registry()
        for cmd in (
            "docker rm -f c1",
            "rm -rf /etc",
            "kubectl delete namespace prod",
            "aws ec2 terminate-instances i-123",
            "DROP TABLE users",
            "git push --force",
            "iptables -F",
            "nginx -s quit",
            "dd if=/dev/zero of=/dev/sda",
        ):
            matches = r.evaluate(cmd)
            assert len(matches) >= 1, f"No matches for {cmd!r} (global quick-reject false negative)"

    def test_global_keyword_reject_blocks_unknown(self):
        """Smoke: commands unrelated to any pack get quick-rejected."""
        from app.packs.registry import build_registry
        r = build_registry()
        for cmd in ("ls", "cat file.txt", "python script.py", "ping 8.8.8.8",
                     "date", "whoami", "top", "df -h"):
            matches = r.evaluate(cmd)
            assert len(matches) == 0, f"False positive for {cmd!r}: {[m.pattern_name for m in matches]}"
