from __future__ import annotations

import hashlib
import importlib
import json

import pytest

provisioner = importlib.import_module("scripts.site_audit_crm_provisioner")


API_KEY = "A" * 40


def test_role_policy_is_minimal_for_projection_contract():
    assert provisioner.EXPECTED_ROLE_DATA == {
        "Account": {
            "create": "yes",
            "read": "all",
            "edit": "all",
            "delete": "no",
            "stream": "no",
        },
        "Document": {
            "create": "yes",
            "read": "all",
            "edit": "all",
            "delete": "no",
            "stream": "no",
        },
        "Attachment": {
            "create": "yes",
            "read": "all",
            "edit": "no",
            "delete": "no",
            "stream": "no",
        },
    }
    assert all(value == "no" for value in provisioner._role_payload().values() if value == "no")


def test_role_conflict_rejects_extra_access_and_wrong_required_access():
    role = provisioner._role_payload()
    assert provisioner._role_conflict(role) is None

    widened = json.loads(json.dumps(role))
    widened["data"]["Contact"] = {"read": "all"}
    assert provisioner._role_conflict(widened) == "role_has_extra_scope_access"

    weakened = json.loads(json.dumps(role))
    weakened["data"]["Document"]["edit"] = "no"
    assert provisioner._role_conflict(weakened) == "role_scope_document_edit_mismatch"


def test_api_user_conflict_requires_single_fixed_role_and_non_admin():
    user = {
        "userName": provisioner.API_USER_NAME,
        "type": "api",
        "authMethod": "ApiKey",
        "isActive": True,
        "isAdmin": False,
        "rolesIds": ["role-1"],
    }
    assert provisioner._user_conflict(user, "role-1") is None

    user["rolesIds"] = ["role-1", "role-2"]
    assert provisioner._user_conflict(user, "role-1") == "api_user_roles_mismatch"
    user["rolesIds"] = ["role-1"]
    user["isAdmin"] = True
    assert provisioner._user_conflict(user, "role-1") == "api_user_is_admin"


def test_replace_env_updates_only_fixed_crm_keys():
    original = (
        b"OTHER=value\n"
        b"AUDIT_CRM_URL=\n"
        b"AUDIT_CRM_API_KEY=REPLACE_WITH_ESPO_API_USER_KEY\n"
        b"UNCHANGED=keep\n"
    )
    replaced = provisioner._replace_env(original, API_KEY).decode()
    assert "OTHER=value\n" in replaced
    assert "UNCHANGED=keep\n" in replaced
    assert "AUDIT_CRM_URL=http://crm-web\n" in replaced
    assert f"AUDIT_CRM_API_KEY={API_KEY}\n" in replaced
    assert replaced.count("AUDIT_CRM_URL=") == 1
    assert replaced.count("AUDIT_CRM_API_KEY=") == 1


def test_replace_env_rejects_duplicate_crm_assignment():
    raw = b"AUDIT_CRM_URL=\nAUDIT_CRM_URL=http://duplicate\n"
    with pytest.raises(provisioner.ProvisionError, match="duplicate_audit_crm_url"):
        provisioner._replace_env(raw, API_KEY)


def test_atomic_env_write_uses_exact_preimage_lease_and_returns_fingerprint(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "CRM_ADMIN_USERNAME=admin\nCRM_ADMIN_PASSWORD=secret\nAUDIT_CRM_URL=\nAUDIT_CRM_API_KEY=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(provisioner, "OPERATOR_ENV", env)
    before = "sha256:" + hashlib.sha256(env.read_bytes()).hexdigest()

    after, key_fingerprint = provisioner._atomic_write_env(before, API_KEY)

    assert after == "sha256:" + hashlib.sha256(env.read_bytes()).hexdigest()
    assert key_fingerprint == "sha256:" + hashlib.sha256(API_KEY.encode()).hexdigest()
    assert key_fingerprint != API_KEY
    assert provisioner._load_env()["AUDIT_CRM_URL"] == "http://crm-web"
    assert provisioner._load_env()["AUDIT_CRM_API_KEY"] == API_KEY

    with pytest.raises(provisioner.ProvisionError, match="operator_env_lease_mismatch"):
        provisioner._atomic_write_env(before, API_KEY)


def test_inspect_state_never_returns_admin_secret(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "CRM_ADMIN_USERNAME=admin\nCRM_ADMIN_PASSWORD=super-secret\nAUDIT_CRM_URL=\nAUDIT_CRM_API_KEY=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(provisioner, "OPERATOR_ENV", env)
    monkeypatch.setattr(
        provisioner,
        "_request_json",
        lambda method, path, headers, payload=None, timeout=15.0: {"user": {"id": "admin-id"}}
        if path == "App/user"
        else {"list": []},
    )

    result = provisioner.inspect_state()

    assert result["crm_env_state"] == "absent"
    assert result["role_state"] == "absent"
    assert result["api_user_state"] == "absent"
    serialized = json.dumps(result)
    assert "super-secret" not in serialized
    assert "Espo-Authorization" not in serialized


def test_existing_api_user_without_env_key_fails_closed(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "CRM_ADMIN_USERNAME=admin\nCRM_ADMIN_PASSWORD=secret\nAUDIT_CRM_URL=\nAUDIT_CRM_API_KEY=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(provisioner, "OPERATOR_ENV", env)
    role = provisioner._role_payload() | {"id": "role-1"}
    user = provisioner._user_payload("role-1") | {"id": "user-1"}

    def find_unique(entity_type, attribute, value, headers):
        del attribute, value, headers
        return role if entity_type == "Role" else user

    monkeypatch.setattr(provisioner, "_find_unique", find_unique)
    monkeypatch.setattr(
        provisioner,
        "_request_json",
        lambda *args, **kwargs: {"user": {"id": "admin-id"}},
    )

    before = "sha256:" + hashlib.sha256(env.read_bytes()).hexdigest()
    with pytest.raises(provisioner.ProvisionError, match="api_user_key_unavailable"):
        provisioner.provision(before)
