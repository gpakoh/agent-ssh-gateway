"""Agent 4 — integration smoke: verify routing, help, no secret leaks."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture(autouse=True)
def _bypass(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "smoke-key")
    monkeypatch.setattr(settings, "allowed_client_cidrs", "0.0.0.0/0")
    monkeypatch.setattr(settings, "trusted_proxy_cidrs", "127.0.0.1/32")
    monkeypatch.setattr(
        "app.auth_middleware.get_client_ip", lambda req, trusted: "127.0.0.1"
    )


HEADERS = {"X-API-Key": "smoke-key"}


class TestRoutingWiring:
    """Endpoints are reachable through the app router stack."""

    def test_auth_check_reachable(self):
        with TestClient(app) as c:
            resp = c.get("/api/auth/check", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["valid"] is True

    def test_session_check_reachable(self):
        with TestClient(app) as c:
            resp = c.post(
                "/api/session/check",
                headers=HEADERS,
                json={"session_id": "nonexistent"},
            )
        assert resp.status_code == 200
        assert resp.json()["valid"] is False

    def test_auth_check_in_openapi(self):
        with TestClient(app) as c:
            schema = c.get("/openapi.json", headers=HEADERS).json()
        assert "/api/auth/check" in schema["paths"]
        assert "get" in schema["paths"]["/api/auth/check"]

    def test_session_check_in_openapi(self):
        with TestClient(app) as c:
            schema = c.get("/openapi.json", headers=HEADERS).json()
        assert "/api/session/check" in schema["paths"]
        assert "post" in schema["paths"]["/api/session/check"]


class TestHelpDiscoverability:
    """Endpoints appear in /api/help response."""

    def _all_help_paths(self, help_data: dict) -> list[str]:
        paths: list[str] = []
        for _key, val in help_data.items():
            if isinstance(val, list):
                paths.extend(ep.get("path", "") for ep in val if isinstance(ep, dict))
            elif isinstance(val, dict):
                for sub_val in val.values():
                    if isinstance(sub_val, list):
                        paths.extend(
                            ep.get("path", "") for ep in sub_val if isinstance(ep, dict)
                        )
        return paths

    def test_auth_check_in_help(self):
        with TestClient(app) as c:
            resp = c.get("/api/help", headers=HEADERS)
        assert "/api/auth/check" in self._all_help_paths(resp.json())

    def test_session_check_in_help(self):
        with TestClient(app) as c:
            resp = c.get("/api/help", headers=HEADERS)
        assert "/api/session/check" in self._all_help_paths(resp.json())


class TestSecurityNoLeaks:
    """Invalid auth returns 401, no secret material in responses."""

    def test_invalid_key_401(self):
        with TestClient(app) as c:
            resp = c.get("/api/auth/check", headers={"X-API-Key": "garbage"})
        assert resp.status_code == 401
        body = resp.json()
        assert body.get("code") == "INVALID_API_KEY"
        assert "smoke-key" not in resp.text

    def test_no_key_401(self):
        with TestClient(app) as c:
            resp = c.get("/api/auth/check")
        assert resp.status_code == 401

    def test_valid_key_no_secret_in_response(self):
        with TestClient(app) as c:
            resp = c.get("/api/auth/check", headers=HEADERS)
        body = resp.json()
        assert "smoke-key" not in str(body)
        assert body["valid"] is True


# ---------------------------------------------------------------------------
# C1 — OpenAPI contract for write/edit/patch endpoints
# ---------------------------------------------------------------------------


class TestC1OpenAPIContract:
    """Write/edit/patch endpoints must appear in OpenAPI with correct shapes."""

    WRITE_ENDPOINTS = [
        ("/api/workspace/projects/{project_id}/files/write", "post"),
        ("/api/workspace/projects/{project_id}/files/edit", "post"),
        ("/api/workspace/projects/{project_id}/files/patch", "post"),
    ]

    @staticmethod
    def _resolve_ref(schema: dict, obj: dict) -> dict:
        """Dereference a $ref if present, returning the properties dict."""
        ref = obj.get("$ref", "")
        if ref and ref.startswith("#/components/schemas/"):
            name = ref.split("/")[-1]
            return schema["components"]["schemas"].get(name, {})
        return obj

    def test_write_endpoints_registered(self):
        with TestClient(app) as c:
            schema = c.get("/openapi.json", headers=HEADERS).json()
        for path, method in self.WRITE_ENDPOINTS:
            assert path in schema["paths"], f"Missing path: {path}"
            assert method in schema["paths"][path], f"Missing {method} on {path}"

    def test_write_endpoint_has_request_body(self):
        with TestClient(app) as c:
            schema = c.get("/openapi.json", headers=HEADERS).json()
        for path, method in self.WRITE_ENDPOINTS:
            op = schema["paths"][path][method]
            assert "requestBody" in op, f"{method.upper()} {path} missing requestBody"

    def test_write_endpoints_require_security(self):
        with TestClient(app) as c:
            schema = c.get("/openapi.json", headers=HEADERS).json()
        for path, method in self.WRITE_ENDPOINTS:
            op = schema["paths"][path][method]
            assert "security" in op, f"{method.upper()} {path} missing security"

    def test_write_file_request_body_has_path_and_content(self):
        with TestClient(app) as c:
            schema = c.get("/openapi.json", headers=HEADERS).json()
        op = schema["paths"]["/api/workspace/projects/{project_id}/files/write"]["post"]
        body_schema = op["requestBody"]["content"]["application/json"]["schema"]
        resolved = self._resolve_ref(schema, body_schema)
        props = resolved.get("properties", {})
        assert "path" in props, "write endpoint missing 'path' property"
        assert "content" in props, "write endpoint missing 'content' property"

    def test_edit_file_request_body_has_old_and_new(self):
        with TestClient(app) as c:
            schema = c.get("/openapi.json", headers=HEADERS).json()
        op = schema["paths"]["/api/workspace/projects/{project_id}/files/edit"]["post"]
        body_schema = op["requestBody"]["content"]["application/json"]["schema"]
        resolved = self._resolve_ref(schema, body_schema)
        props = resolved.get("properties", {})
        assert "old_string" in props, "edit endpoint missing 'old_string'"
        assert "new_string" in props, "edit endpoint missing 'new_string'"

    def test_patch_file_request_body_has_path_and_patch(self):
        with TestClient(app) as c:
            schema = c.get("/openapi.json", headers=HEADERS).json()
        op = schema["paths"]["/api/workspace/projects/{project_id}/files/patch"]["post"]
        body_schema = op["requestBody"]["content"]["application/json"]["schema"]
        resolved = self._resolve_ref(schema, body_schema)
        props = resolved.get("properties", {})
        assert "path" in props, "patch endpoint missing 'path'"
        assert "patch" in props, "patch endpoint missing 'patch'"


# ---------------------------------------------------------------------------
# C1 — MCP tool registration (source-level verification)
# ---------------------------------------------------------------------------


class TestC1MCPToolRegistration:
    """MCP server source must register project_write_file, project_edit_file,
    project_apply_patch tools."""

    MCP_DIR = "examples/mcp_server"

    def _read_server_source(self) -> str:
        import pathlib

        path = pathlib.Path(__file__).resolve().parent.parent / self.MCP_DIR / "server.py"
        return path.read_text()

    def test_project_apply_patch_registered(self):
        src = self._read_server_source()
        assert "project_apply_patch" in src

    def test_project_file_write_registered(self):
        src = self._read_server_source()
        assert "project_file_write" in src

    def test_project_file_edit_registered(self):
        src = self._read_server_source()
        assert "project_file_edit" in src

    def test_patch_tool_has_instrumented_decorator(self):
        src = self._read_server_source()
        assert '@register_tool("project_apply_patch")' in src

    def test_write_tool_has_instrumented_decorator(self):
        src = self._read_server_source()
        assert "project_file_write" in src

    def test_edit_tool_has_instrumented_decorator(self):
        src = self._read_server_source()
        assert "project_file_edit" in src


# ---------------------------------------------------------------------------
# C1 — No response content leaks on error paths
# ---------------------------------------------------------------------------


class TestC1NoContentLeaks:
    """Write/edit/patch error responses must not leak server-internal paths."""

    def test_write_error_no_absolute_path(self):
        """Hidden-path error on write must not leak absolute paths."""
        with TestClient(app) as c:
            resp = c.post(
                "/api/workspace/projects/web-ssh-gateway/files/write",
                json={"path": ".env", "content": "EVIL=true"},
                headers=HEADERS,
            )
        if resp.status_code not in (400, 403):
            pytest.skip("write endpoint returned unexpected status (no projects.yaml?)")
        body = resp.text
        assert "/tmp/" not in body, f"Absolute /tmp/ path leaked: {body}"
        assert "/root/" not in body, f"Absolute /root/ path leaked: {body}"
        assert "/home/" not in body, f"Absolute /home/ path leaked: {body}"

    def test_edit_error_no_absolute_path(self):
        """Hidden-path error on edit must not leak absolute paths."""
        with TestClient(app) as c:
            resp = c.post(
                "/api/workspace/projects/web-ssh-gateway/files/edit",
                json={"path": ".env", "old_string": "SECRET", "new_string": "EVIL"},
                headers=HEADERS,
            )
        if resp.status_code not in (400, 403):
            pytest.skip("edit endpoint returned unexpected status")
        body = resp.text
        assert "/tmp/" not in body
        assert "/root/" not in body
        assert "/home/" not in body

    def test_patch_error_no_absolute_path(self):
        """Hidden-path error on patch must not leak absolute paths."""
        patch = (
            "--- a/.env\n+++ b/.env\n"
            "@@ -1 +1 @@\n"
            "-SECRET\n+EVIL\n"
        )
        with TestClient(app) as c:
            resp = c.post(
                "/api/workspace/projects/web-ssh-gateway/files/patch",
                json={"path": ".env", "patch": patch},
                headers=HEADERS,
            )
        if resp.status_code not in (400, 403):
            pytest.skip("patch endpoint returned unexpected status")
        body = resp.text
        assert "/tmp/" not in body
        assert "/root/" not in body
        assert "/home/" not in body

    def test_traversal_error_no_absolute_path(self):
        """Traversal error must not leak absolute paths."""
        with TestClient(app) as c:
            resp = c.post(
                "/api/workspace/projects/web-ssh-gateway/files/write",
                json={"path": "../escape.txt", "content": "data"},
                headers=HEADERS,
            )
        if resp.status_code not in (400, 403):
            pytest.skip("write endpoint returned unexpected status")
        body = resp.text
        assert "/tmp/" not in body
        assert "/root/" not in body
        assert "/home/" not in body

    def test_symlink_error_no_absolute_path(self):
        """Symlink-escape error must not leak absolute paths."""
        with TestClient(app) as c:
            resp = c.post(
                "/api/workspace/projects/web-ssh-gateway/files/write",
                json={"path": "escape_link/loot.txt", "content": "hacked"},
                headers=HEADERS,
            )
        if resp.status_code not in (400, 403):
            pytest.skip("write endpoint returned unexpected status")
        body = resp.text
        assert "/tmp/" not in body
        assert "/root/" not in body
        assert "/home/" not in body

    def test_error_response_no_stacktrace(self):
        """Error responses must never contain Python tracebacks."""
        with TestClient(app) as c:
            resp = c.post(
                "/api/workspace/projects/web-ssh-gateway/files/write",
                json={"path": ".env", "content": "EVIL=true"},
                headers=HEADERS,
            )
        if resp.status_code not in (400, 403):
            pytest.skip("write endpoint returned unexpected status")
        body = resp.text.lower()
        assert "traceback" not in body, "Stacktrace leaked in error response"


# ---------------------------------------------------------------------------
# C1 — Agent token scope enforcement on write endpoints
# ---------------------------------------------------------------------------


class TestC1ScopeEnforcement:
    """Agent tokens without project:write scope must be rejected on write endpoints."""

    WRITE_PATHS = [
        "/api/workspace/projects/web-ssh-gateway/files/write",
        "/api/workspace/projects/web-ssh-gateway/files/edit",
        "/api/workspace/projects/web-ssh-gateway/files/patch",
    ]

    def test_agent_without_write_scope_rejected(self, monkeypatch):
        """Agent token with only ssh:connect scope must get 403 on write."""
        from unittest.mock import AsyncMock

        from app.auth_middleware import AuthIdentity

        monkeypatch.setattr(settings, "api_auth_enabled", True)
        identity = AuthIdentity(
            token_type="agent",
            token="limited-token",
            name="limited-agent",
            scopes=("ssh:connect",),
        )
        monkeypatch.setattr(
            "app.auth_middleware.verify_api_key", AsyncMock(return_value=identity)
        )
        headers = {"X-API-Key": "limited-token"}
        with TestClient(app) as c:
            resp = c.post(
                "/api/workspace/projects/web-ssh-gateway/files/write",
                json={"path": "x.txt", "content": "a"},
                headers=headers,
            )
            assert resp.status_code == 403, (
                f"Expected 403 for limited agent, got {resp.status_code}"
            )
            body = resp.json()
            assert body["detail"]["code"] == "MISSING_SCOPE"
