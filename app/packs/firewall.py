from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

FIREWALL_PATTERNS: tuple[DestructivePattern, ...] = (
DestructivePattern(
        name="iptables-flush",
        regex=r"\biptables\b.*(?:\s-F\b|\s+--flush\b)",
        reason="iptables --flush removes all firewall rules",
        severity=Severity.CRITICAL,
        description="Flushes all iptables rules. Network access will be lost immediately.",
        suggestions=(
            PatternSuggestion(command="iptables -L -n -v", description="List current rules before flushing", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="iptables-save > /tmp/rules.backup", description="Backup rules first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="iptables-policy-drop",
        regex=r"\biptables\b.*\s-P\s+(?:INPUT|FORWARD|OUTPUT)\s+DROP\b",
        reason="Setting default policy to DROP disconnects all traffic",
        severity=Severity.CRITICAL,
        description="Changes the default policy to DROP. SSH session will be terminated.",
        suggestions=(
            PatternSuggestion(command="iptables -L -n", description="Check current policy first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="at now +5 minutes <<< 'iptables -P INPUT ACCEPT'", description="Schedule automatic rollback", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="iptables-delete-chains",
        regex=r"\biptables\b.*\s-X\b",
        reason="Deleting custom iptables chains removes security rules",
        severity=Severity.HIGH,
        description="Removes all user-defined chains. Security rules and jump rules are lost.",
        suggestions=(
            PatternSuggestion(command="iptables-save > /tmp/rules.backup", description="Backup rules first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="iptables -L -n", description="List all rules before deleting chains", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="iptables-restore",
        regex=r"\biptables-restore\b",
        reason="iptables-restore replaces the entire ruleset at once",
        severity=Severity.HIGH,
        description="Replaces all iptables rules. A malformed ruleset disconnects the server.",
        suggestions=(
            PatternSuggestion(command="iptables-save > /tmp/rules.backup", description="Backup current rules first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="iptables-restore --test < {file}", description="Test ruleset before applying", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="ip6tables-flush",
        regex=r"\bip6tables\b.*(?:\s-F\b|\s+--flush\b)",
        reason="ip6tables --flush removes all IPv6 firewall rules",
        severity=Severity.CRITICAL,
        description="Flushes all ip6tables rules. IPv6 network access will be lost.",
        suggestions=(
            PatternSuggestion(command="ip6tables -L -n -v", description="List IPv6 rules first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="ip6tables-save > /tmp/rules6.backup", description="Backup IPv6 rules first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="iptables-insert-reject",
        regex=r"\biptables\b.*\s-[IA]\s+\S+(?:\s+\d+)?\s+-j\s+(?:DROP|REJECT)\b",
        reason="Inserting a DROP/REJECT rule locks out matching traffic",
        severity=Severity.HIGH,
        description="A DROP/REJECT rule at the top of INPUT chain blocks SSH if rule matches.",
        suggestions=(
            PatternSuggestion(command="iptables -L INPUT -n -v", description="Check INPUT chain first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="iptables --check INPUT -s {ip} -j DROP", description="Test if a similar rule already exists", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="ufw-disable",
        regex=r"\bufw\b.*\bdisable\b",
        reason="Disabling ufw removes all firewall protection",
        severity=Severity.CRITICAL,
        description="ufw is disabled. No firewall rules are enforced.",
        suggestions=(
            PatternSuggestion(command="ufw status verbose", description="Check current ufw status first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="ufw reload", description="Reload instead of disable", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="ufw-reset",
        regex=r"\bufw\b.*\breset\b",
        reason="Resetting ufw deletes all custom rules",
        severity=Severity.HIGH,
        description="All ufw rules are deleted and firewall is disabled.",
        suggestions=(
            PatternSuggestion(command="ufw status numbered", description="List rules with numbers before reset", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="ufw delete {num}", description="Delete individual rules instead of full reset", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="ufw-default-deny",
        regex=r"\bufw\b.*\bdefault\s+deny\b",
        reason="Setting ufw default to deny blocks all incoming traffic",
        severity=Severity.HIGH,
        description="Changes the default ufw policy to deny. SSH access may be lost.",
        suggestions=(
            PatternSuggestion(command="ufw status verbose", description="Check current default policy first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="ufw default deny incoming", description="Only deny incoming, keep outgoing allowed", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="ufw-delete",
        regex=r"\bufw\b.*\bdelete\b",
        reason="Deleting ufw rules may remove SSH access rules",
        severity=Severity.MEDIUM,
        description="Deletes a ufw rule. Removing the wrong rule may lock out SSH.",
        suggestions=(
            PatternSuggestion(command="ufw status numbered", description="List rules with numbers first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="ufw show added", description="Show ufw user-defined rules", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="nft-flush-ruleset",
        regex=r"\bnft\s+flush\s+ruleset\b",
        reason="nft flush ruleset removes all nftables rules",
        severity=Severity.CRITICAL,
        description="Flushes the entire nftables ruleset. All firewall rules are removed at once.",
        suggestions=(
            PatternSuggestion(command="nft list ruleset", description="List current ruleset before flushing", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="nft list ruleset > /tmp/nftables.backup", description="Backup ruleset first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="nft-delete-table",
        regex=r"\bnft\s+delete\s+table\b",
        reason="Deleting an nftables table removes all chains and rules",
        severity=Severity.HIGH,
        description="Deletes an entire nftables table. All chains, rules, and sets are removed.",
        suggestions=(
            PatternSuggestion(command="nft list table {table}", description="List table rules before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="nft list ruleset | grep -A5 'table {table}'", description="Inspect table contents", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="nft-load-stdin",
        regex=r"\bnft\b\s+-[fF]\s+(?:/dev/stdin|-)(?:\s|$)",
        reason="Loading nftables rules from stdin can inject destructive rules",
        severity=Severity.HIGH,
        description="Loading rules from stdin or a pipe is risky. A malformed ruleset disconnects the server.",
        suggestions=(
            PatternSuggestion(command="nft -c -f {file}", description="Check syntax before loading", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="nft list ruleset > /tmp/backup.nft", description="Backup current ruleset first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
)

def build_firewall_pack() -> Pack:
    return Pack(id="firewall", name="Firewall patterns",
        destructive_patterns=FIREWALL_PATTERNS,
        keywords=("iptables", "ip6tables", "ufw", "nft"),
    )
