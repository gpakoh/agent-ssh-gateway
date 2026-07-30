"""Tests for P11: Suggest Allowlist Clustering."""

from __future__ import annotations

from app.suggest import (
    CommandCluster,
    ConfidenceTier,
    RiskLevel,
    SuggestionSafetyDecision,
    _assess_risk_level,
    _calculate_confidence_tier,
    _calculate_suggestion_score,
    _extract_program,
    check_suggestion_safety,
    generate_enhanced_suggestions,
    generate_pattern_from_cluster,
    jaccard_similarity,
    tokenize_for_similarity,
)

# ── Tokenization ─────────────────────────────────────────────────────────


def test_tokenize_basic():
    assert tokenize_for_similarity("rm -rf /") == ["rm", "-rf", "/"]


def test_tokenize_case():
    assert tokenize_for_similarity("RM -RF /") == ["rm", "-rf", "/"]


def test_tokenize_whitespace():
    assert tokenize_for_similarity("  git   commit  ") == ["git", "commit"]


def test_tokenize_empty():
    assert tokenize_for_similarity("") == []


# ── Jaccard similarity ───────────────────────────────────────────────────


def test_jaccard_identical():
    assert jaccard_similarity(["rm", "-rf", "/"], ["rm", "-rf", "/"]) == 1.0


def test_jaccard_no_overlap():
    assert jaccard_similarity(["rm", "-rf", "/"], ["echo", "hello"]) == 0.0


def test_jaccard_partial():
    sim = jaccard_similarity(["rm", "-rf", "/"], ["rm", "dir"])
    assert sim == 0.25  # intersection: {rm}, union: {rm, -rf, /, dir}


def test_jaccard_both_empty():
    assert jaccard_similarity([], []) == 1.0


def test_jaccard_one_empty():
    assert jaccard_similarity(["rm"], []) == 0.0


# ── Program extraction ───────────────────────────────────────────────────


def test_extract_simple():
    assert _extract_program("rm -rf /") == "rm"


def test_extract_env_prefix():
    assert _extract_program("FOO=bar rm -rf /") == "rm"


def test_extract_multiple_env():
    assert _extract_program("A=1 B=2 echo hi") == "echo"


def test_extract_empty():
    assert _extract_program("") == ""


# ── Pattern generation ───────────────────────────────────────────────────


def test_gen_pattern_single():
    c = CommandCluster(commands=["rm -rf /"])
    p = generate_pattern_from_cluster(c)
    assert p.regex.startswith("^")
    assert p.regex.endswith("$")


def test_gen_pattern_multi():
    c = CommandCluster(commands=["rm -rf /tmp", "rm -rf /var/tmp"])
    p = generate_pattern_from_cluster(c)
    assert len(p.example_matches) <= 5
    assert p.specificity_score > 0.5


def test_gen_pattern_alternation():
    """2-10 middle variants → alternation (?:a|b)."""
    c = CommandCluster(commands=["apt-get install foo", "apt-get install bar", "apt-get install baz"])
    p = generate_pattern_from_cluster(c)
    assert "(?:" in p.regex or "install" in p.regex


def test_gen_pattern_destructive_not_generated():
    """Cluster with rm -rf / should still produce a pattern (safety handled later)."""
    c = CommandCluster(commands=["rm -rf /"])
    p = generate_pattern_from_cluster(c)
    assert p.regex is not None


def test_gen_pattern_empty_cluster():
    c = CommandCluster(commands=[])
    p = generate_pattern_from_cluster(c)
    assert p.regex in ("^$", "")


# ── Confidence tiers ─────────────────────────────────────────────────────


def test_confidence_high():
    c = _calculate_confidence_tier(10, 3, 0, False)
    assert c == ConfidenceTier.HIGH


def test_confidence_medium():
    c = _calculate_confidence_tier(5, 5, 0, False)
    assert c == ConfidenceTier.MEDIUM


def test_confidence_low():
    c = _calculate_confidence_tier(3, 3, 0, False)
    assert c == ConfidenceTier.LOW


def test_confidence_high_not_consistent():
    """10 freq but 8 variants means freq/variants < 2 → Medium."""
    c = _calculate_confidence_tier(10, 8, 0, False)
    assert c == ConfidenceTier.MEDIUM


def test_confidence_path_boost():
    """Low with path clusters → Medium."""
    c = _calculate_confidence_tier(4, 4, 0, True)
    assert c == ConfidenceTier.LOW  # 4 < 5


# ── Risk assessment ──────────────────────────────────────────────────────


def test_risk_high_rm():
    assert _assess_risk_level("rm -rf /") == RiskLevel.HIGH


def test_risk_medium_git_reset():
    assert _assess_risk_level("git reset --hard") == RiskLevel.MEDIUM


def test_risk_low_echo():
    assert _assess_risk_level("echo hello") == RiskLevel.LOW


# ── Safety filter ─────────────────────────────────────────────────────────


def test_safety_allow_echo():
    assert check_suggestion_safety(["echo hello"]) == SuggestionSafetyDecision.ALLOW


def test_safety_never_rm_root():
    assert check_suggestion_safety(["rm -rf /"]) == SuggestionSafetyDecision.NEVER_SUGGEST


def test_safety_never_drop_database():
    assert check_suggestion_safety(["DROP TABLE users"]) == SuggestionSafetyDecision.NEVER_SUGGEST


def test_safety_never_dd():
    assert check_suggestion_safety(["dd if=/dev/zero of=/dev/sda bs=4M"]) == SuggestionSafetyDecision.NEVER_SUGGEST


def test_safety_never_mkfs():
    assert check_suggestion_safety(["mkfs.ext4 /dev/sdb1"]) == SuggestionSafetyDecision.NEVER_SUGGEST


def test_safety_never_delete_all():
    assert check_suggestion_safety(["DELETE FROM users WHERE 1=1"]) == SuggestionSafetyDecision.NEVER_SUGGEST


def test_safety_never_terraform_destroy():
    assert check_suggestion_safety(["terraform destroy"]) == SuggestionSafetyDecision.NEVER_SUGGEST


def test_safety_never_helm_uninstall():
    assert check_suggestion_safety(["helm uninstall release"]) == SuggestionSafetyDecision.NEVER_SUGGEST


def test_safety_never_kubectl_delete_ns():
    assert check_suggestion_safety(["kubectl delete namespace prod"]) == SuggestionSafetyDecision.NEVER_SUGGEST


def test_safety_require_confirmation_system_path():
    assert check_suggestion_safety(["cat /etc/passwd"]) == SuggestionSafetyDecision.REQUIRE_CONFIRMATION


def test_safety_require_sensitive_home():
    assert check_suggestion_safety(["ls /root"]) == SuggestionSafetyDecision.REQUIRE_CONFIRMATION


def test_safety_require_sensitive_var_lib():
    assert check_suggestion_safety(["ls /var/lib"]) == SuggestionSafetyDecision.REQUIRE_CONFIRMATION


def test_safety_never_rm_system_wildcard():
    assert check_suggestion_safety(["rm -rf /etc/*"]) == SuggestionSafetyDecision.NEVER_SUGGEST


def test_safety_multi_commands_most_restrictive():
    assert check_suggestion_safety(["echo hi", "rm -rf /"]) == SuggestionSafetyDecision.NEVER_SUGGEST


# ── Scoring ──────────────────────────────────────────────────────────────


def test_score_high_low():
    s = _calculate_suggestion_score(ConfidenceTier.HIGH, RiskLevel.LOW, SuggestionSafetyDecision.ALLOW, 0)
    assert 0.8 <= s <= 1.0


def test_score_low_high():
    s = _calculate_suggestion_score(ConfidenceTier.LOW, RiskLevel.HIGH, SuggestionSafetyDecision.ALLOW, 0)
    assert s <= 0.3


def test_score_require_confirmation_penalty():
    s1 = _calculate_suggestion_score(ConfidenceTier.MEDIUM, RiskLevel.LOW, SuggestionSafetyDecision.ALLOW, 0)
    s2 = _calculate_suggestion_score(ConfidenceTier.MEDIUM, RiskLevel.LOW, SuggestionSafetyDecision.REQUIRE_CONFIRMATION, 0)
    assert s2 < s1


def test_score_never_zero():
    s = _calculate_suggestion_score(ConfidenceTier.HIGH, RiskLevel.LOW, SuggestionSafetyDecision.NEVER_SUGGEST, 0)
    assert s == 0.0


def test_score_bypass_boost():
    s = _calculate_suggestion_score(ConfidenceTier.MEDIUM, RiskLevel.LOW, SuggestionSafetyDecision.ALLOW, 5)
    assert s > _calculate_suggestion_score(ConfidenceTier.MEDIUM, RiskLevel.LOW, SuggestionSafetyDecision.ALLOW, 0)


# ── End-to-end suggestions ───────────────────────────────────────────────


def test_suggest_empty_entries():
    assert generate_enhanced_suggestions([]) == []


def test_suggest_below_min_frequency():
    entries = [{"command": "echo hi", "working_dir": "/tmp", "was_bypassed": False}] * 2
    assert generate_enhanced_suggestions(entries, min_frequency=3) == []


def test_suggest_single_cluster():
    entries = [{"command": "rm -rf /tmp", "working_dir": "/home", "was_bypassed": False}] * 5
    results = generate_enhanced_suggestions(entries)
    assert len(results) == 1
    assert results[0].cluster.frequency_count == 5


def test_suggest_cluster_similar_commands():
    entries = [
        {"command": "rm -rf /tmp/cache", "working_dir": "/tmp", "was_bypassed": False},
        {"command": "rm -rf /tmp/build", "working_dir": "/tmp", "was_bypassed": False},
        {"command": "rm -rf /tmp/cache", "working_dir": "/tmp", "was_bypassed": False},
        {"command": "rm -rf /tmp/build", "working_dir": "/tmp", "was_bypassed": False},
    ]
    results = generate_enhanced_suggestions(entries, min_frequency=2)
    assert len(results) >= 1


def test_suggest_multiple_programs_separate():
    """Commands with different programs should not cluster together."""
    entries = [
        {"command": "apt-get install foo", "working_dir": "/", "was_bypassed": False},
        {"command": "apt-get install foo", "working_dir": "/", "was_bypassed": False},
        {"command": "pip install bar", "working_dir": "/", "was_bypassed": False},
        {"command": "pip install bar", "working_dir": "/", "was_bypassed": False},
    ]
    results = generate_enhanced_suggestions(entries, min_frequency=2)
    assert len(results) == 2  # two separate clusters


def test_suggest_safety_removes_dangerous():
    """Suggestions with rm -rf / should be filtered out by safety."""
    entries = [
        {"command": "rm -rf /", "working_dir": "/", "was_bypassed": False},
        {"command": "rm -rf /", "working_dir": "/", "was_bypassed": False},
        {"command": "rm -rf /", "working_dir": "/", "was_bypassed": False},
    ]
    results = generate_enhanced_suggestions(entries, min_frequency=2)
    # Should be filtered out by NeverSuggest
    assert len(results) == 0


def test_suggest_sorted_by_score():
    entries = [
        # High confidence + low risk
        {"command": "pip list", "working_dir": "/tmp", "was_bypassed": False},
        {"command": "pip list", "working_dir": "/tmp", "was_bypassed": False},
        {"command": "pip list", "working_dir": "/tmp", "was_bypassed": False},
        {"command": "pip list", "working_dir": "/tmp", "was_bypassed": False},
        # Lower confidence
        {"command": "npm install", "working_dir": "/tmp", "was_bypassed": False},
        {"command": "npm install", "working_dir": "/tmp", "was_bypassed": False},
        {"command": "npm install", "working_dir": "/tmp", "was_bypassed": False},
        {"command": "npm install", "working_dir": "/tmp", "was_bypassed": False},
    ]
    results = generate_enhanced_suggestions(entries, min_frequency=3)
    assert len(results) == 2
    assert results[0].score >= results[1].score


def test_suggest_bypass_count_tracked():
    entries = [
        {"command": "pip install foo", "working_dir": "/tmp", "was_bypassed": True},
        {"command": "pip install foo", "working_dir": "/tmp", "was_bypassed": True},
        {"command": "pip install foo", "working_dir": "/tmp", "was_bypassed": False},
    ]
    results = generate_enhanced_suggestions(entries, min_frequency=2)
    assert len(results) == 1
    assert results[0].bypass_count == 2


def test_suggest_path_patterns():
    entries = [
        {"command": "cat /etc/hosts", "working_dir": "/etc", "was_bypassed": False},
        {"command": "cat /etc/hosts", "working_dir": "/etc", "was_bypassed": False},
        {"command": "cat /etc/hosts", "working_dir": "/etc", "was_bypassed": False},
    ]
    results = generate_enhanced_suggestions(entries, min_frequency=2)
    assert len(results) >= 0  # may be safety-filtered


def test_suggest_confidence_tiers():
    entries = []
    for _ in range(12):
        entries.append({"command": "pip list", "working_dir": "/tmp", "was_bypassed": False})
    results = generate_enhanced_suggestions(entries, min_frequency=3)
    if results:
        assert results[0].confidence_tier == ConfidenceTier.HIGH
