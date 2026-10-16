#!/usr/bin/env python3
"""Provision the fixed least-privilege Site Audit EspoCRM API identity.

This helper is invoked only by the guarded MCP Docker adapter.  It accepts no
caller-controlled CRM endpoint, role name, user name or secret value.  The
operator project is mounted at /operator and the helper talks only to the
fixed ``crm-web`` service on the Site Audit control network.

Secret material never appears in stdout.  Provision mode writes the generated
API key directly into the operator .env using an exact preimage SHA-256 lease
and an atomic same-directory replace.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from dotenv import dotenv_values

CRM_BASE_URL = "http://crm-web/api/v1"
CRM_PUBLIC_URL = "http://crm-web"
ROLE_NAME = "Site Audit CRM Projection"
API_USER_NAME = "site-audit-projection"
OPERATOR_ENV = Path("/operator/.env")
MAX_RESPONSE_BYTES = 1024 * 1024

EXPECTED_ROLE_DATA: dict[str, dict[str, str]] = {
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

_SPECIAL_PERMISSION_FIELDS = (
    "assignmentPermission",
    "userPermission",
    "portalPermission",
    "groupEmailAccountPermission",
    "exportPermission",
    "massUpdatePermission",
    "followerManagementPermission",
    "dataPrivacyPermission",
    "messagePermission",
    "auditPermission",
    "mentionPermission",
    "lockPermission",
)

_ENV_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=.*$"
)


class ProvisionError(RuntimeError):
    """Expected fail-closed provisioning error with non-secret text only."""


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_env_bytes() -> bytes:
    try:
        info = OPERATOR_ENV.lstat()
    except FileNotFoundError as exc:
        raise ProvisionError("operator_env_missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProvisionError("operator_env_not_regular")
    return OPERATOR_ENV.read_bytes()


def _load_env() -> dict[str, str]:
    raw = dotenv_values(OPERATOR_ENV)
    result: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, str):
            result[key] = value
    return result


def _configured(value: str | None) -> bool:
    return bool(isinstance(value, str) and value.strip())


def _admin_headers(env: dict[str, str]) -> dict[str, str]:
    username = env.get("CRM_ADMIN_USERNAME", "").strip()
    password = env.get("CRM_ADMIN_PASSWORD", "")
    if not username or not password:
        raise ProvisionError("crm_admin_credentials_missing")
    auth = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Espo-Authorization": auth,
    }


def _api_key_headers(api_key: str) -> dict[str, str]:
    if not _valid_secret(api_key):
        raise ProvisionError("crm_api_key_invalid")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
    }


def _valid_secret(value: object) -> bool:
    return (
        isinstance(value, str)
        and 20 <= len(value) <= 200
        and value.isascii()
        and value.isprintable()
        and not any(ch.isspace() for ch in value)
    )


def _request_json(
    method: str,
    path: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    if not path or path.startswith("/") or ".." in path:
        raise ProvisionError("crm_request_path_invalid")
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = request.Request(
        f"{CRM_BASE_URL}/{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProvisionError("crm_response_too_large")
            if response.status < 200 or response.status >= 300:
                raise ProvisionError(f"crm_http_{response.status}")
    except error.HTTPError as exc:
        raise ProvisionError(f"crm_http_{exc.code}") from None
    except (error.URLError, TimeoutError, OSError) as exc:
        raise ProvisionError("crm_transport_error") from exc
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvisionError("crm_response_invalid_json") from exc
    if not isinstance(decoded, dict):
        raise ProvisionError("crm_response_invalid_shape")
    return decoded


def _where_equals(attribute: str, value: str) -> str:
    return json.dumps(
        [{"type": "equals", "attribute": attribute, "value": value}],
        separators=(",", ":"),
    )


def _find_unique(
    entity_type: str,
    attribute: str,
    value: str,
    headers: dict[str, str],
) -> dict[str, Any] | None:
    query = parse.urlencode(
        {
            "where": _where_equals(attribute, value),
            "maxSize": "20",
        }
    )
    payload = _request_json("GET", f"{entity_type}?{query}", headers)
    records = payload.get("list")
    if not isinstance(records, list):
        raise ProvisionError("crm_list_invalid_shape")
    matches = [record for record in records if isinstance(record, dict) and record.get(attribute) == value]
    if len(matches) > 1:
        raise ProvisionError(f"duplicate_{entity_type.lower()}_identity")
    if not matches:
        return None
    record_id = matches[0].get("id")
    if not isinstance(record_id, str) or not record_id:
        raise ProvisionError(f"{entity_type.lower()}_id_missing")
    return _request_json("GET", f"{entity_type}/{parse.quote(record_id, safe='')}", headers)


def _role_conflict(role: dict[str, Any]) -> str | None:
    data = role.get("data")
    if not isinstance(data, dict):
        return "role_data_missing"
    for scope, expected in EXPECTED_ROLE_DATA.items():
        actual = data.get(scope)
        if not isinstance(actual, dict):
            return f"role_scope_{scope.lower()}_missing"
        for action, level in expected.items():
            if actual.get(action) != level:
                return f"role_scope_{scope.lower()}_{action}_mismatch"
    for scope, actual in data.items():
        if scope in EXPECTED_ROLE_DATA or not isinstance(actual, dict):
            continue
        if any(value not in (None, "no", False) for value in actual.values()):
            return "role_has_extra_scope_access"
    for field in _SPECIAL_PERMISSION_FIELDS:
        if role.get(field) not in (None, "no", False):
            return f"role_{field}_not_minimal"
    return None


def _user_conflict(user: dict[str, Any], role_id: str) -> str | None:
    if user.get("userName") != API_USER_NAME:
        return "api_user_name_mismatch"
    if user.get("type") != "api":
        return "api_user_type_mismatch"
    if user.get("authMethod") != "ApiKey":
        return "api_user_auth_method_mismatch"
    if user.get("isActive") is False:
        return "api_user_inactive"
    if user.get("isAdmin") is True or user.get("isSuperAdmin") is True:
        return "api_user_is_admin"
    roles = user.get("rolesIds")
    if not isinstance(roles, list):
        roles = []
    if set(str(item) for item in roles) != {role_id}:
        return "api_user_roles_mismatch"
    return None


def _role_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": ROLE_NAME,
        "data": EXPECTED_ROLE_DATA,
        "fieldData": {},
    }
    for field in _SPECIAL_PERMISSION_FIELDS:
        payload[field] = "no"
    return payload


def _user_payload(role_id: str) -> dict[str, Any]:
    return {
        "userName": API_USER_NAME,
        "type": "api",
        "authMethod": "ApiKey",
        "isActive": True,
        "rolesIds": [role_id],
    }


def _create_or_reconcile_role(headers: dict[str, str]) -> tuple[dict[str, Any], bool]:
    existing = _find_unique("Role", "name", ROLE_NAME, headers)
    if existing is not None:
        conflict = _role_conflict(existing)
        if conflict:
            raise ProvisionError(conflict)
        return existing, False
    try:
        created = _request_json("POST", "Role", headers, _role_payload())
    except ProvisionError:
        reconciled = _find_unique("Role", "name", ROLE_NAME, headers)
        if reconciled is None:
            raise
        conflict = _role_conflict(reconciled)
        if conflict:
            raise ProvisionError(conflict) from None
        return reconciled, True
    role_id = created.get("id")
    if not isinstance(role_id, str) or not role_id:
        reconciled = _find_unique("Role", "name", ROLE_NAME, headers)
        if reconciled is None:
            raise ProvisionError("role_create_outcome_unknown")
        created = reconciled
    else:
        created = _request_json("GET", f"Role/{parse.quote(role_id, safe='')}", headers)
    conflict = _role_conflict(created)
    if conflict:
        raise ProvisionError(conflict)
    return created, True


def _create_or_reconcile_user(
    headers: dict[str, str],
    role_id: str,
) -> tuple[dict[str, Any], bool]:
    existing = _find_unique("User", "userName", API_USER_NAME, headers)
    if existing is not None:
        conflict = _user_conflict(existing, role_id)
        if conflict:
            raise ProvisionError(conflict)
        return existing, False
    try:
        created = _request_json("POST", "User", headers, _user_payload(role_id))
    except ProvisionError:
        reconciled = _find_unique("User", "userName", API_USER_NAME, headers)
        if reconciled is None:
            raise
        conflict = _user_conflict(reconciled, role_id)
        if conflict:
            raise ProvisionError(conflict) from None
        return reconciled, True
    user_id = created.get("id")
    if not isinstance(user_id, str) or not user_id:
        reconciled = _find_unique("User", "userName", API_USER_NAME, headers)
        if reconciled is None:
            raise ProvisionError("api_user_create_outcome_unknown")
        created = reconciled
    else:
        # Preserve the generated key from the create response if present; a
        # detail GET may omit it in future EspoCRM releases.
        generated_key = created.get("apiKey")
        detail = _request_json("GET", f"User/{parse.quote(user_id, safe='')}", headers)
        if _valid_secret(generated_key) and not _valid_secret(detail.get("apiKey")):
            detail["apiKey"] = generated_key
        created = detail
    conflict = _user_conflict(created, role_id)
    if conflict:
        raise ProvisionError(conflict)
    return created, True


def _env_crm_state(env: dict[str, str]) -> str:
    url_set = _configured(env.get("AUDIT_CRM_URL"))
    key_set = _configured(env.get("AUDIT_CRM_API_KEY"))
    if url_set != key_set:
        return "partial"
    if not url_set:
        return "absent"
    if env.get("AUDIT_CRM_URL", "").strip() != CRM_PUBLIC_URL:
        return "conflict"
    if not _valid_secret(env.get("AUDIT_CRM_API_KEY", "")):
        return "conflict"
    return "ready"


def _replace_env(raw: bytes, api_key: str) -> bytes:
    if not _valid_secret(api_key):
        raise ProvisionError("crm_api_key_invalid")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvisionError("operator_env_not_utf8") from exc
    updates = {"AUDIT_CRM_URL": CRM_PUBLIC_URL, "AUDIT_CRM_API_KEY": api_key}
    seen = {key: 0 for key in updates}
    output: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _ENV_ASSIGNMENT_RE.match(line.rstrip("\r\n"))
        if match and match.group("key") in updates:
            key = match.group("key")
            seen[key] += 1
            if seen[key] > 1:
                raise ProvisionError(f"operator_env_duplicate_{key.lower()}")
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            output.append(f"{match.group('prefix')}{key}={updates[key]}{ending}")
        else:
            output.append(line)
    if output and not output[-1].endswith(("\n", "\r\n")):
        output[-1] += "\n"
    for key, value in updates.items():
        if seen[key] == 0:
            output.append(f"{key}={value}\n")
    return "".join(output).encode("utf-8")


def _atomic_write_env(expected_sha256: str, api_key: str) -> tuple[str, str]:
    raw = _read_env_bytes()
    if _sha256_bytes(raw) != expected_sha256:
        raise ProvisionError("operator_env_lease_mismatch")
    info = OPERATOR_ENV.lstat()
    replacement = _replace_env(raw, api_key)
    parent = OPERATOR_ENV.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".env.crm.", dir=parent)
    try:
        os.fchmod(fd, stat.S_IMODE(info.st_mode))
        try:
            os.fchown(fd, info.st_uid, info.st_gid)
        except PermissionError:
            pass
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, OPERATOR_ENV)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    final = _read_env_bytes()
    return _sha256_bytes(final), _sha256_bytes(api_key.encode("utf-8"))


def inspect_state() -> dict[str, Any]:
    raw = _read_env_bytes()
    env = _load_env()
    result: dict[str, Any] = {
        "version": 1,
        "env_sha256": _sha256_bytes(raw),
        "crm_env_state": _env_crm_state(env),
        "admin_user_configured": _configured(env.get("CRM_ADMIN_USERNAME")),
        "admin_password_configured": _configured(env.get("CRM_ADMIN_PASSWORD")),
        "admin_auth_ok": False,
        "role_state": "unknown",
        "api_user_state": "unknown",
    }
    headers = _admin_headers(env)
    identity = _request_json("GET", "App/user", headers)
    user = identity.get("user")
    if not isinstance(user, dict) or not isinstance(user.get("id"), str):
        raise ProvisionError("crm_admin_auth_invalid_shape")
    result["admin_auth_ok"] = True
    role = _find_unique("Role", "name", ROLE_NAME, headers)
    if role is None:
        result["role_state"] = "absent"
        result["api_user_state"] = "absent"
        existing_user = _find_unique("User", "userName", API_USER_NAME, headers)
        if existing_user is not None:
            result["api_user_state"] = "conflict"
        return result
    role_id = role.get("id")
    if not isinstance(role_id, str) or not role_id:
        raise ProvisionError("role_id_missing")
    result["role_id"] = role_id
    conflict = _role_conflict(role)
    result["role_state"] = "conflict" if conflict else "matching"
    if conflict:
        result["role_conflict"] = conflict
        return result
    api_user = _find_unique("User", "userName", API_USER_NAME, headers)
    if api_user is None:
        result["api_user_state"] = "absent"
        return result
    user_id = api_user.get("id")
    if isinstance(user_id, str) and user_id:
        result["api_user_id"] = user_id
    user_conflict = _user_conflict(api_user, role_id)
    result["api_user_state"] = "conflict" if user_conflict else "matching"
    if user_conflict:
        result["api_user_conflict"] = user_conflict
    return result


def provision(expected_env_sha256: str) -> dict[str, Any]:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_env_sha256):
        raise ProvisionError("expected_env_sha256_invalid")
    raw = _read_env_bytes()
    if _sha256_bytes(raw) != expected_env_sha256:
        raise ProvisionError("operator_env_lease_mismatch")
    env = _load_env()
    crm_env_state = _env_crm_state(env)
    if crm_env_state in {"partial", "conflict"}:
        raise ProvisionError(f"crm_env_{crm_env_state}")
    headers = _admin_headers(env)
    identity = _request_json("GET", "App/user", headers)
    if not isinstance(identity.get("user"), dict):
        raise ProvisionError("crm_admin_auth_invalid_shape")

    role, role_created = _create_or_reconcile_role(headers)
    role_id = role.get("id")
    if not isinstance(role_id, str) or not role_id:
        raise ProvisionError("role_id_missing")
    api_user, user_created = _create_or_reconcile_user(headers, role_id)
    user_id = api_user.get("id")
    if not isinstance(user_id, str) or not user_id:
        raise ProvisionError("api_user_id_missing")

    if crm_env_state == "ready":
        api_key = env.get("AUDIT_CRM_API_KEY", "")
    else:
        candidate_api_key = api_user.get("apiKey")
        if not isinstance(candidate_api_key, str) or not _valid_secret(candidate_api_key):
            # A reconciled API user without a retrievable key is an ambiguous
            # partial state.  Do not create a second identity or rotate a key
            # implicitly; an operator must reconcile it explicitly.
            raise ProvisionError("api_user_key_unavailable")
        api_key = candidate_api_key

    # Prove that the exact key can authenticate and read the only parent scope
    # before making it durable in the operator env.
    _request_json("GET", "Account?maxSize=1&select=id", _api_key_headers(api_key))

    if crm_env_state == "ready":
        final_env_sha = _sha256_bytes(raw)
        key_fingerprint = _sha256_bytes(api_key.encode("utf-8"))
        env_updated = False
    else:
        final_env_sha, key_fingerprint = _atomic_write_env(expected_env_sha256, api_key)
        env_updated = True
        final_env = _load_env()
        if _env_crm_state(final_env) != "ready":
            raise ProvisionError("operator_env_postcondition_failed")

    return {
        "version": 1,
        "outcome": "ready",
        "role_id": role_id,
        "api_user_id": user_id,
        "role_created": role_created,
        "api_user_created": user_created,
        "env_updated": env_updated,
        "env_sha256": final_env_sha,
        "api_key_fingerprint": key_fingerprint,
        "crm_projection_ready": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("inspect", "provision"), required=True)
    parser.add_argument("--expected-env-sha256", default="")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        payload = inspect_state() if args.mode == "inspect" else provision(args.expected_env_sha256)
    except ProvisionError as exc:
        print(json.dumps({"version": 1, "outcome": "blocked", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
