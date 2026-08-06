"""Cloud pack destructive pattern tests, focused on the generic terraform patterns.

dns.py and monitoring.py already carried narrow, resource-targeted
`terraform destroy -target=...` patterns for their own domains, but nothing
in the pack library covered a bare `terraform destroy` (whole state/workspace)
or `terraform apply -auto-approve` (unreviewed apply) -- the two most common
and most catastrophic terraform invocations.
"""

from __future__ import annotations

from app.packs.registry import build_registry


class TestCloudPackTerraform:
    def test_bare_terraform_destroy_matched(self):
        r = build_registry()
        matches = r.evaluate("terraform destroy")
        names = {m.pattern_name for m in matches}
        assert "terraform-destroy-bare" in names

    def test_terraform_destroy_auto_approve_matched(self):
        r = build_registry()
        matches = r.evaluate("terraform destroy -auto-approve")
        names = {m.pattern_name for m in matches}
        assert "terraform-destroy-bare" in names

    def test_terraform_apply_auto_approve_matched(self):
        r = build_registry()
        matches = r.evaluate("terraform apply -auto-approve")
        names = {m.pattern_name for m in matches}
        assert "terraform-apply-auto-approve" in names

    def test_terraform_plan_not_matched(self):
        r = build_registry()
        matches = r.evaluate("terraform plan")
        names = {m.pattern_name for m in matches}
        assert "terraform-destroy-bare" not in names
        assert "terraform-apply-auto-approve" not in names

    def test_terraform_plan_destroy_not_matched(self):
        """terraform plan -destroy only previews -- must not match the destroy pattern."""
        r = build_registry()
        matches = r.evaluate("terraform plan -destroy")
        names = {m.pattern_name for m in matches}
        assert "terraform-destroy-bare" not in names

    def test_terraform_apply_without_auto_approve_not_matched(self):
        """Plain terraform apply still stops for interactive confirmation."""
        r = build_registry()
        matches = r.evaluate("terraform apply")
        names = {m.pattern_name for m in matches}
        assert "terraform-apply-auto-approve" not in names

    def test_cloud_pack_keywords_include_terraform(self):
        r = build_registry()
        pack = r.get("cloud")
        assert pack is not None
        assert "terraform" in pack.keywords

    def test_new_terraform_patterns_carry_suggestions(self):
        r = build_registry()
        pack = r.get("cloud")
        assert pack is not None
        for dp in pack.destructive_patterns:
            if dp.name.startswith("terraform-"):
                assert dp.suggestions, f"cloud/{dp.name} has no suggestions"
                assert all(s.command and s.description for s in dp.suggestions)
