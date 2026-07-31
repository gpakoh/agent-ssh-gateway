"""DNS pack destructive pattern tests (P18)."""

from __future__ import annotations

from app.packs.registry import build_registry


class TestDnsPack:
    def test_dns_pack_patterns(self):
        """DNS pack (P18) covers nsupdate, dig zone transfer, cloudflare, route53."""
        r = build_registry()
        cases = {
            "nsupdate -l delete example.com": "dns-nsupdate-local",
            "dig example.com axfr": "dns-dig-zone-transfer",
            "wrangler dns-records delete --id 1": "cloudflare-wrangler-dns-delete",
            "aws route53 delete-health-check --health-check-id 1": "route53-delete-health-check",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            assert expected in names, f"{cmd!r}: expected {expected}, got {names}"

    def test_new_packs_patterns_carry_suggestions(self):
        """All patterns in dns + package_managers packs have suggestions (P17 convention)."""
        r = build_registry()
        for pack_id in ("dns", "package_managers"):
            pack = r.get(pack_id)
            assert pack is not None
            for dp in pack.destructive_patterns:
                assert dp.suggestions, f"{pack_id}/{dp.name} has no suggestions"
                assert all(s.command and s.description for s in dp.suggestions)
