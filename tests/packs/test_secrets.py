"""Secrets pack destructive pattern tests (P18)."""

from __future__ import annotations

from app.packs.registry import build_registry


class TestSecretsPack:
    def test_secrets_pack_patterns(self):
        """Secrets pack (P18) covers vault, aws ssm/secretsmanager, doppler, 1password."""
        r = build_registry()
        cases = {
            "vault kv destroy -versions=1 secret/x": "vault-kv-destroy",
            "vault secrets disable kv": "vault-secrets-disable",
            "vault policy delete my-policy": "vault-policy-delete",
            "vault auth disable userpass": "vault-auth-disable",
            "vault token revoke 123": "vault-token-revoke",
            "aws ssm delete-parameter --name /app/DB_PASS": "aws-ssm-delete-parameter",
            "aws secretsmanager delete-resource-policy --secret-id x": "aws-secretsmanager-delete-resource-policy",
            "doppler secrets delete KEY": "doppler-secrets-delete",
            "op item delete login-item": "op-item-delete",
            "op vault delete prod": "op-vault-delete",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            assert expected in names, f"{cmd!r}: expected {expected}, got {names}"

    def test_secrets_pack_reads_not_blocked(self):
        """Read/list operations on secrets tools must NOT be blocked."""
        r = build_registry()
        for cmd in (
            "vault kv get secret/x",
            "vault kv list secret/",
            "vault read secret/x",
            "vault secrets list",
            "aws secretsmanager describe-secret --secret-id x",
            "aws ssm get-parameter --name /app/DB_PASS",
            "doppler secrets get KEY",
            "op item get login-item",
        ):
            matches = r.evaluate(cmd)
            assert len(matches) == 0, f"False positive for {cmd!r}: {[m.pattern_name for m in matches]}"
