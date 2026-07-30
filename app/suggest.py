"""P11: Suggest Allowlist Clustering — cluster similar denied commands and generate allowlist suggestions.

Port of DCG src/suggest.rs (2406 lines). Key simplifications:
- No serde/serialization (dataclass-based)
- No CommandEntryInfo struct (flat dict input)
- No normalize module dependency (simple tokenization)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ── Enums ────────────────────────────────────────────────────────────────


class ConfidenceTier(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RiskLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SuggestionSafetyDecision(StrEnum):
    ALLOW = "allow"
    REQUIRE_CONFIRMATION = "require_confirmation"
    NEVER_SUGGEST = "never_suggest"


class SuggestionReason(StrEnum):
    HIGH_FREQUENCY = "high_frequency"
    PATH_CLUSTERED = "path_clustered"
    MANUALLY_BYPASSED = "manually_bypassed"
    SAFE_PATTERN_MATCH = "safe_pattern_match"


# ── Data structures ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PathPattern:
    pattern: str
    occurrence_count: int
    is_project_dir: bool


@dataclass
class CommandCluster:
    commands: list[str] = field(default_factory=list)
    normalized: list[str] = field(default_factory=list)
    proposed_pattern: str = ""
    frequency_count: int = 0
    unique_count: int = 0
    bypass_count: int = 0


@dataclass
class GeneratedPattern:
    regex: str
    specificity_score: float = 0.0
    matches_all: bool = False
    example_matches: list[str] = field(default_factory=list)


@dataclass
class AllowlistSuggestion:
    cluster: CommandCluster
    confidence_tier: ConfidenceTier = ConfidenceTier.LOW
    risk_level: RiskLevel = RiskLevel.LOW
    reason: SuggestionReason = SuggestionReason.HIGH_FREQUENCY
    path_patterns: list[PathPattern] = field(default_factory=list)
    safety_decision: SuggestionSafetyDecision = SuggestionSafetyDecision.ALLOW
    score: float = 0.0
    recommendation: str = ""
    bypass_count: int = 0


# ── Internal clustering types ────────────────────────────────────────────


@dataclass
class _Record:
    original: str
    normalized: str
    tokens: list[str]


@dataclass
class _TempCluster:
    records: list[_Record] = field(default_factory=list)
    rep_tokens: set[str] = field(default_factory=set)


# ── Tokenization and similarity ──────────────────────────────────────────


def tokenize_for_similarity(command: str) -> list[str]:
    """Split by whitespace and lowercase; no punctuation stripping."""
    return command.strip().lower().split()


def jaccard_similarity(a: list[str], b: list[str]) -> float:
    """Jaccard similarity over token sets.

    Both empty → 1.0. One empty → 0.0.
    """
    if not a and not b:
        return 1.0
    set_a = set(a)
    set_b = set(b)
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return len(set_a & set_b) / union


# ── Program extraction ───────────────────────────────────────────────────


def _extract_program(command: str) -> str:
    """Extract the first token (program name), stripping leading env vars."""
    stripped = command.strip()
    if not stripped:
        return ""
    first = stripped.split()[0]
    # Strip leading env VAR=val prefixes (e.g. FOO=bar rm -rf / → rm)
    while "=" in first and not first.startswith("--"):
        rest = stripped[len(first):].strip()
        if not rest:
            break
        first = rest.split()[0]
        stripped = rest
    return first


# ── Pattern generation ──────────────────────────────────────────────────


def _escape_regex(text: str) -> str:
    """Escape a plain text segment for use inside a regex character class or literal."""
    return re.escape(text)


def generate_pattern_from_cluster(cluster: CommandCluster) -> GeneratedPattern:
    """Generate a regex pattern from a cluster of similar commands.

    Strategy:
    - Find common prefix tokens (first N identical).
    - Find common suffix tokens (last M identical, after prefix).
    - Middle segment: single variant → exact; 2-10 → alternation; >10 → token wildcard.
    """
    commands = list(dict.fromkeys(cluster.commands))  # deduplicate preserving order
    tokenized = [tokenize_for_similarity(cmd) for cmd in commands]
    max_len = max(len(t) for t in tokenized) if tokenized else 0

    # Token index helper
    def _token_seq(idx_slice: slice) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for tokens in tokenized:
            part = tokens[idx_slice]
            key = " ".join(part)
            if key not in seen:
                seen.add(key)
                result.append(key)
        return result

    # Find common prefix length
    prefix_len = 0
    if tokenized:
        while prefix_len < min(len(t) for t in tokenized):
            first_token = tokenized[0][prefix_len]
            if all(t[prefix_len] == first_token for t in tokenized):
                prefix_len += 1
            else:
                break

    # Find common suffix length after prefix
    suffix_len = 0
    if tokenized:
        while suffix_len < min(len(t) - prefix_len for t in tokenized):
            suffix_idx = -(suffix_len + 1)
            last_token = tokenized[0][suffix_idx]
            if all(t[suffix_idx] == last_token for t in tokenized):
                suffix_len += 1
            else:
                break

    # Middle segment
    middle: list[str] = []
    if prefix_len + suffix_len < max_len:
        for tokens in tokenized:
            segment = tokens[prefix_len:max_len - suffix_len]
            if segment:
                joined = " ".join(segment)
                if joined not in middle:
                    middle.append(joined)

    # Build regex
    parts: list[str] = []
    # Prefix
    prefix_tokens = [t for t in (tokenized[0][:prefix_len] if tokenized else [])]
    if prefix_tokens:
        parts.append(r"\s+".join(_escape_regex(t) for t in prefix_tokens))
    elif tokenized:
        parts.append(_escape_regex(tokenized[0][0]) if tokenized else "")

    # Middle
    if len(middle) == 1:
        seg = middle[0]
        parts.append(r"\s+" + _escape_regex(seg) if " " in seg else r"\s+" + _escape_regex(seg))
    elif 2 <= len(middle) <= 10:
        parts.append(r"\s+(?:" + "|".join(_escape_regex(m) for m in middle) + ")")
    elif middle:
        parts.append(r"\s+[^\s]+")  # conservative token wildcard

    # Suffix
    suffix_tokens = [t for t in (tokenized[0][max_len - suffix_len:] if tokenized else [])]
    if suffix_tokens:
        parts.append(r"\s+" + r"\s+".join(_escape_regex(t) for t in suffix_tokens))

    regex = r"^" + "".join(parts) + r"$"

    # Specificity: 1.0 for no wildcards, less for alternation/wildcard
    specificity = 1.0
    if middle and len(middle) > 10:
        specificity -= 0.4
    elif middle and len(middle) > 1:
        specificity -= 0.2
    if suffix_len == 0 and len(parts) > 1:
        specificity -= 0.1

    return GeneratedPattern(
        regex=regex,
        specificity_score=max(0.0, specificity),
        matches_all=len(middle) >= len(commands) if commands else False,
        example_matches=commands[:5],
    )


# ── Confidence tiers ─────────────────────────────────────────────────────


def _calculate_confidence_tier(
    frequency: int,
    unique_variants: int,
    bypass_count: int,
    has_path_clusters: bool,
) -> ConfidenceTier:
    """Determine confidence tier from frequency and consistency."""
    if frequency >= 10 and (frequency / max(unique_variants, 1)) >= 2.0:
        return ConfidenceTier.HIGH
    if frequency >= 5:
        if has_path_clusters:
            return ConfidenceTier.MEDIUM
        return ConfidenceTier.MEDIUM
    return ConfidenceTier.LOW


# ── Pattern confidence score mapping ─────────────────────────────────────


_CONFIDENCE_SCORES: dict[ConfidenceTier, float] = {
    ConfidenceTier.HIGH: 1.0,
    ConfidenceTier.MEDIUM: 0.6,
    ConfidenceTier.LOW: 0.3,
}


_RISK_SCORES: dict[RiskLevel, float] = {
    RiskLevel.LOW: 0.2,
    RiskLevel.MEDIUM: 0.5,
    RiskLevel.HIGH: 0.9,
}


# ── Risk assessment ──────────────────────────────────────────────────────


_HIGH_RISK_PATTERNS: list[re.Pattern] = [
    re.compile(r"rm\b"),
    re.compile(r"rmdir\b"),
    re.compile(r"drop\b", re.IGNORECASE),
    re.compile(r"delete\b", re.IGNORECASE),
    re.compile(r"--force\b"),
    re.compile(r"-f\b"),
]

_MEDIUM_RISK_PATTERNS: list[re.Pattern] = [
    re.compile(r"git reset"),
    re.compile(r"docker rm"),
    re.compile(r"kubectl delete"),
    re.compile(r"truncate\b", re.IGNORECASE),
    re.compile(r"mv\b"),
]


def _assess_risk_level(normalized: str) -> RiskLevel:
    """Assess risk level from command content."""
    for p in _HIGH_RISK_PATTERNS:
        if p.search(normalized):
            return RiskLevel.HIGH
    for p in _MEDIUM_RISK_PATTERNS:
        if p.search(normalized):
            return RiskLevel.MEDIUM
    return RiskLevel.LOW


# ── Safety filter ────────────────────────────────────────────────────────

# — Normalize: strip regex escapes, anchors, etc. —
_STRIP_ANCHORS = re.compile(r"[\^$\\]")

# — NeverSuggest patterns (destructive / irreversible) —
_NEVER_SUGGEST_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("fork_bomb", re.compile(r":\s*\(\s*\)\s*\{|fork\s+bomb")),
    ("rm_root_recursive", re.compile(r"\brm\s+-[rf]+\s+/\s*$")),  # rm -rf /  (trailing space allowed)
    ("rm_root_recursive2", re.compile(r"\brm\s+-[rf]+\s+/\*")),  # rm -rf /*
    ("dd_raw_disk", re.compile(r"\bdd\s+if=.*of=.*/dev/sd")),
    ("format_block_device", re.compile(r"\bmkfs\b")),
    ("drop_database", re.compile(r"\bdrop\s+(database|table)\b", re.IGNORECASE)),
    ("truncate_table", re.compile(r"\btruncate\s+(database|table)\b", re.IGNORECASE)),
    ("unbounded_row_delete", re.compile(r"\bdelete\s+from\b.*\bwhere\s+(1=1|true)\b", re.IGNORECASE)),
    ("infra_kubectl_delete_ns", re.compile(r"\bkubectl\s+delete\s+namespace\b", re.IGNORECASE)),
    ("infra_helm_uninstall", re.compile(r"\bhelm\s+uninstall\b", re.IGNORECASE)),
    ("infra_terraform_destroy", re.compile(r"\bterraform\s+destroy\b", re.IGNORECASE)),
    ("infra_docker_system_prune", re.compile(r"\bdocker\s+system\s+prune\b", re.IGNORECASE)),
    ("infra_aws_terminate", re.compile(r"\baws\s+ec2\s+terminate-instances\b", re.IGNORECASE)),
    ("infra_gcloud_delete", re.compile(r"\bgcloud\s+.*\bdelete\b", re.IGNORECASE)),
]

_SYSTEM_PATHS = ["/etc", "/bin", "/boot", "/dev", "/proc", "/sys"]
_SENSITIVE_PATHS = ["/root", "/home", "/var/lib"]


def check_suggestion_safety(commands: list[str]) -> SuggestionSafetyDecision:
    """Check if a suggestion is safe. Returns the most restrictive decision."""
    result = SuggestionSafetyDecision.ALLOW

    for cmd in commands:
        normalized = cmd.lower().strip()

        # Check NeverSuggest patterns
        for _name, pattern in _NEVER_SUGGEST_PATTERNS:
            if pattern.search(normalized):
                return SuggestionSafetyDecision.NEVER_SUGGEST

        # System path + rm/wildcard → NeverSuggest
        has_rm_force = bool(re.search(r"\brm\s+-rf?\b", normalized))
        has_wildcard = "*" in normalized or "?" in normalized
        on_system_path = any(p in normalized for p in _SYSTEM_PATHS)
        if has_rm_force and on_system_path and has_wildcard:
            return SuggestionSafetyDecision.NEVER_SUGGEST

        # System paths → RequireConfirmation
        if on_system_path:
            result = SuggestionSafetyDecision.REQUIRE_CONFIRMATION
        if any(p in normalized for p in _SENSITIVE_PATHS):
            result = SuggestionSafetyDecision.REQUIRE_CONFIRMATION

    return result


# ── Suggestion scoring ────────────────────────────────────────────────────


def _calculate_suggestion_score(
    confidence_tier: ConfidenceTier,
    risk_level: RiskLevel,
    safety_decision: SuggestionSafetyDecision,
    bypass_count: int,
) -> float:
    """Score a suggestion: confidence × (1 - 0.4 × risk), with safety adjustments."""
    c_score = _CONFIDENCE_SCORES[confidence_tier]
    r_score = _RISK_SCORES[risk_level]
    score = c_score * (1.0 - 0.4 * r_score)
    # Clamp
    score = max(0.0, min(1.0, score))

    # Safety adjustment
    if safety_decision == SuggestionSafetyDecision.REQUIRE_CONFIRMATION:
        score *= 0.85
    elif safety_decision == SuggestionSafetyDecision.NEVER_SUGGEST:
        score = 0.0

    # Bypass boost: +0.05 per bypass (capped)
    score += min(bypass_count * 0.05, 0.2)
    return max(0.0, min(1.0, score))


# ── Entry point ──────────────────────────────────────────────────────────


def generate_enhanced_suggestions(
    entries: list[dict[str, Any]],
    min_frequency: int = 3,
    similarity_threshold: float = 0.30,
) -> list[AllowlistSuggestion]:
    """Full pipeline: group → cluster → enrich → safety → score → sort.

    Args:
        entries: List of blocked command entries, each containing
            ``{"command": str, "working_dir": str, "was_bypassed": bool}``.
        min_frequency: Minimum command frequency to consider.
        similarity_threshold: Jaccard similarity threshold for clustering.

    Returns:
        Suggestions sorted by score descending.
    """
    if not entries:
        return []

    # 1. Group by command
    cmd_groups: dict[str, dict[str, Any]] = {}
    for entry in entries:
        command = entry.get("command", "").strip()
        if not command:
            continue
        if command not in cmd_groups:
            cmd_groups[command] = {
                "frequency_count": 0,
                "working_dirs": set(),
                "bypass_count": 0,
            }
        g = cmd_groups[command]
        g["frequency_count"] += 1
        wd = entry.get("working_dir", "")
        if wd:
            g["working_dirs"].add(wd)
        if entry.get("was_bypassed"):
            g["bypass_count"] += 1

    # 2. Filter by min_frequency
    filtered = {cmd: info for cmd, info in cmd_groups.items() if info["frequency_count"] >= min_frequency}
    if not filtered:
        return []

    # 3. Build record list for clustering
    records: list[_Record] = []
    for cmd in filtered:
        norm = cmd.lower().strip()
        tokens = tokenize_for_similarity(norm)
        records.append(_Record(
            original=cmd,
            normalized=norm,
            tokens=tokens,
        ))

    # 4. Group by program (first token), cluster within each group
    program_groups: dict[str, list[_Record]] = {}
    for r in records:
        prog = r.tokens[0] if r.tokens else ""
        if prog not in program_groups:
            program_groups[prog] = []
        program_groups[prog].append(r)

    # 5. Jaccard-cluster within each program group
    clusters: list[CommandCluster] = []
    for _prog, recs in program_groups.items():
        if len(recs) == 1:
            r = recs[0]
            clusters.append(CommandCluster(
                commands=[r.original],
                normalized=[r.normalized],
                frequency_count=cmd_groups.get(r.original, {}).get("frequency_count", 1),
                unique_count=1,
                bypass_count=cmd_groups.get(r.original, {}).get("bypass_count", 0),
            ))
            continue

        unclustered = list(recs)
        while unclustered:
            first = unclustered.pop(0)
            members = [_TempCluster(records=[first], rep_tokens=set(first.tokens))]
            remaining: list[_Record] = []
            for other in unclustered:
                best_sim = max(jaccard_similarity(list(m.rep_tokens), list(other.tokens)) for m in members)
                if best_sim >= similarity_threshold:
                    members.append(_TempCluster(records=[other], rep_tokens=set(other.tokens)))
                else:
                    remaining.append(other)
            unclustered = remaining

            # Collapse cluster if too diverse: check pairwise similarity via rep command
            rep_commands = [m.records[0] for m in members]
            if len(rep_commands) > 1:
                use_all = True
                for i in range(len(rep_commands)):
                    for j in range(i + 1, len(rep_commands)):
                        sim = jaccard_similarity(rep_commands[i].tokens, rep_commands[j].tokens)
                        if sim < similarity_threshold:
                            use_all = False
                            break
                    if not use_all:
                        break
                if not use_all:
                    # Add each member as its own cluster
                    for m in members:
                        r = m.records[0]
                        clusters.append(CommandCluster(
                            commands=[r.original],
                            normalized=[r.normalized],
                            frequency_count=cmd_groups.get(r.original, {}).get("frequency_count", 1),
                            unique_count=1,
                            bypass_count=cmd_groups.get(r.original, {}).get("bypass_count", 0),
                        ))
                    continue

            # Merge to single cluster
            all_commands: list[str] = []
            all_normalized: list[str] = []
            for m in members:
                for r in m.records:
                    if r.original not in all_commands:
                        all_commands.append(r.original)
                    if r.normalized not in all_normalized:
                        all_normalized.append(r.normalized)

            total_freq = sum(cmd_groups.get(r.original, {}).get("frequency_count", 1) for m in members for r in m.records)
            total_bypass = sum(cmd_groups.get(r.original, {}).get("bypass_count", 0) for m in members for r in m.records)

            clusters.append(CommandCluster(
                commands=all_commands,
                normalized=all_normalized,
                frequency_count=total_freq,
                unique_count=len(all_commands),
                bypass_count=total_bypass,
            ))

    # 6. Enrich: generate pattern, assess risk, confidence, safety
    suggestions: list[AllowlistSuggestion] = []
    for cluster in clusters:
        pattern = generate_pattern_from_cluster(cluster)
        cluster.proposed_pattern = pattern.regex

        # Reason / confidence
        reason = SuggestionReason.HIGH_FREQUENCY
        has_path_cluster = False
        # Check if all commands start with the same path pattern
        for cmd in cluster.commands:
            for p in _SYSTEM_PATHS + _SENSITIVE_PATHS:
                if p in cmd:
                    has_path_cluster = True
                    break

        tier = _calculate_confidence_tier(
            cluster.frequency_count,
            cluster.unique_count,
            cluster.bypass_count,
            has_path_cluster,
        )

        # Risk from the most severe command
        risk = RiskLevel.LOW
        for norm in cluster.normalized:
            cmd_risk = _assess_risk_level(norm)
            if cmd_risk == RiskLevel.HIGH:
                risk = RiskLevel.HIGH
                break
            if cmd_risk == RiskLevel.MEDIUM and risk != RiskLevel.HIGH:
                risk = RiskLevel.MEDIUM

        # Safety
        safety = check_suggestion_safety(cluster.commands)

        # Path patterns
        path_patterns: list[PathPattern] = []
        dir_counts: dict[str, int] = {}
        for cmd in cluster.commands:
            info = cmd_groups.get(cmd, {})
            for wd in info.get("working_dirs", set()):
                dir_counts[wd] = dir_counts.get(wd, 0) + 1
        for dir_path, count in sorted(dir_counts.items(), key=lambda x: -x[1]):
            path_patterns.append(PathPattern(
                pattern=dir_path + "/",
                occurrence_count=count,
                is_project_dir=True,
            ))

        # Score
        score = _calculate_suggestion_score(tier, risk, safety, cluster.bypass_count)

        # Recommendation
        if safety == SuggestionSafetyDecision.NEVER_SUGGEST:
            rec = "This pattern is dangerous and should never be suggested."
        elif safety == SuggestionSafetyDecision.REQUIRE_CONFIRMATION:
            rec = "Suggested with caution — confirm before allowlisting."
        elif tier == ConfidenceTier.HIGH:
            rec = "Strongly recommended for allowlisting."
        else:
            rec = "Consider reviewing before allowlisting."

        suggestions.append(AllowlistSuggestion(
            cluster=cluster,
            confidence_tier=tier,
            risk_level=risk,
            reason=reason,
            path_patterns=path_patterns,
            safety_decision=safety,
            score=score,
            recommendation=rec,
            bypass_count=cluster.bypass_count,
        ))

    # 7. Safety filter — remove NeverSuggest
    suggestions = [s for s in suggestions if s.safety_decision != SuggestionSafetyDecision.NEVER_SUGGEST]

    # 8. Sort by score descending, then confidence tier, then frequency
    suggestions.sort(key=lambda s: (-s.score, -_CONFIDENCE_SCORES.get(s.confidence_tier, 0), -s.cluster.frequency_count))

    return suggestions
