from __future__ import annotations

from app.command_policy import DestructivePattern, Severity
from app.packs import Pack

FIREWALL_PATTERNS: tuple[DestructivePattern, ...] = (
DestructivePattern(
        name="iptables-flush",
        regex=r"\biptables\b.*(?:\s-F\b|\s+--flush\b)",
        reason="iptables --flush removes all firewall rules",
        severity=Severity.CRITICAL,
        description="Flushes all iptables rules. Network access will be lost immediately.",
        suggestions=(),
    ),
DestructivePattern(
        name="iptables-policy-drop",
        regex=r"\biptables\b.*\s-P\s+(?:INPUT|FORWARD|OUTPUT)\s+DROP\b",
        reason="Setting default policy to DROP disconnects all traffic",
        severity=Severity.CRITICAL,
        description="Changes the default policy to DROP. SSH session will be terminated.",
        suggestions=(),
    ),
DestructivePattern(
        name="iptables-delete-chains",
        regex=r"\biptables\b.*\s-X\b",
        reason="Deleting custom iptables chains removes security rules",
        severity=Severity.HIGH,
        description="Removes all user-defined chains. Security rules and jump rules are lost.",
        suggestions=(),
    ),
DestructivePattern(
        name="iptables-restore",
        regex=r"\biptables-restore\b",
        reason="iptables-restore replaces the entire ruleset at once",
        severity=Severity.HIGH,
        description="Replaces all iptables rules. A malformed ruleset disconnects the server.",
        suggestions=(),
    ),
DestructivePattern(
        name="ip6tables-flush",
        regex=r"\bip6tables\b.*(?:\s-F\b|\s+--flush\b)",
        reason="ip6tables --flush removes all IPv6 firewall rules",
        severity=Severity.CRITICAL,
        description="Flushes all ip6tables rules. IPv6 network access will be lost.",
        suggestions=(),
    ),
DestructivePattern(
        name="iptables-insert-reject",
        regex=r"\biptables\b.*\s-[IA]\s+\S+(?:\s+\d+)?\s+-j\s+(?:DROP|REJECT)\b",
        reason="Inserting a DROP/REJECT rule locks out matching traffic",
        severity=Severity.HIGH,
        description="A DROP/REJECT rule at the top of INPUT chain blocks SSH if rule matches.",
        suggestions=(),
    ),
DestructivePattern(
        name="ufw-disable",
        regex=r"\bufw\b.*\bdisable\b",
        reason="Disabling ufw removes all firewall protection",
        severity=Severity.CRITICAL,
        description="ufw is disabled. No firewall rules are enforced.",
        suggestions=(),
    ),
DestructivePattern(
        name="ufw-reset",
        regex=r"\bufw\b.*\breset\b",
        reason="Resetting ufw deletes all custom rules",
        severity=Severity.HIGH,
        description="All ufw rules are deleted and firewall is disabled.",
        suggestions=(),
    ),
DestructivePattern(
        name="ufw-default-deny",
        regex=r"\bufw\b.*\bdefault\s+deny\b",
        reason="Setting ufw default to deny blocks all incoming traffic",
        severity=Severity.HIGH,
        description="Changes the default ufw policy to deny. SSH access may be lost.",
        suggestions=(),
    ),
DestructivePattern(
        name="ufw-delete",
        regex=r"\bufw\b.*\bdelete\b",
        reason="Deleting ufw rules may remove SSH access rules",
        severity=Severity.MEDIUM,
        description="Deletes a ufw rule. Removing the wrong rule may lock out SSH.",
        suggestions=(),
    ),
DestructivePattern(
        name="nft-flush-ruleset",
        regex=r"\bnft\s+flush\s+ruleset\b",
        reason="nft flush ruleset removes all nftables rules",
        severity=Severity.CRITICAL,
        description="Flushes the entire nftables ruleset. All firewall rules are removed at once.",
        suggestions=(),
    ),
DestructivePattern(
        name="nft-delete-table",
        regex=r"\bnft\s+delete\s+table\b",
        reason="Deleting an nftables table removes all chains and rules",
        severity=Severity.HIGH,
        description="Deletes an entire nftables table. All chains, rules, and sets are removed.",
        suggestions=(),
    ),
DestructivePattern(
        name="nft-load-stdin",
        regex=r"\bnft\b\s+-[fF]\s+(?:/dev/stdin|-)(?:\s|$)",
        reason="Loading nftables rules from stdin can inject destructive rules",
        severity=Severity.HIGH,
        description="Loading rules from stdin or a pipe is risky. A malformed ruleset disconnects the server.",
        suggestions=(),
    ),
)

def build_firewall_pack() -> Pack:
    return Pack(id="firewall", name="Firewall patterns",
        destructive_patterns=FIREWALL_PATTERNS,
        keywords=("iptables", "ip6tables", "ufw", "nft"),
    )
