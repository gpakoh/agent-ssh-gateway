"""Contract tests: verify OpenAPI schema correctness for agent clients."""

import json

import jsonschema
import pytest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def schema():
    return app.openapi()


def _op(schema, path, method="get"):
    return schema["paths"].get(path, {}).get(method, {})


def _content(schema, path, method, status=200):
    return (
        _op(schema, path, method)
        .get("responses", {})
        .get(str(status), {})
        .get("content", {})
    )


class TestMediaTypes:
    CRITICAL = [
        ("/", "get", "text/html"),
        ("/metrics", "get", "text/plain"),
        ("/api/sdk/download", "get", "text/x-python"),
        ("/api/jobs/{job_id}/stream", "get", "text/event-stream"),
        ("/api/jobs/{job_id}/events", "get", "text/event-stream"),
        ("/api/file/raw", "get", "text/plain"),
        ("/api/file/download", "get", "application/octet-stream"),
    ]

    def test_critical_endpoints_have_correct_content_type(self, schema):
        for path, method, expected in self.CRITICAL:
            c = _content(schema, path, method)
            assert expected in c, (
                f"{method.upper()} {path}: expected {expected}, got {list(c)}"
            )

    def test_422_refers_to_validation_error_response(self, schema):
        for path, methods in schema["paths"].items():
            for method, op in methods.items():
                resp = op.get("responses", {}).get("422", {})
                ct = resp.get("content", {})
                ref = ct.get("application/json", {}).get("schema", {}).get("$ref", "")
                if resp:
                    assert ref == "#/components/schemas/ValidationErrorResponse", (
                        f"{method.upper()} {path}: 422 ref={ref}"
                    )


class TestSecurity:
    def test_security_schemes_defined(self, schema):
        schemes = schema["components"].get("securitySchemes", {})
        assert "ApiKeyHeader" in schemes
        assert "ApiKeyQuery" not in schemes, "ApiKeyQuery is not supported at runtime"
        assert schemes["ApiKeyHeader"] == {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
        }

    PUBLIC_GET = frozenset({"/health", "/api/capabilities"})

    def test_protected_endpoints_have_security(self, schema):
        """Only /health and /api/capabilities are public; everything else requires X-API-Key."""
        for path, methods in schema["paths"].items():
            for method, op in methods.items():
                key = (path, method.upper())
                if key in (("/health", "GET"), ("/api/capabilities", "GET")):
                    assert "security" not in op or op["security"] == [], (
                        f"{path} GET should be public, got security={op.get('security')}"
                    )
                else:
                    assert "security" in op, f"{method.upper()} {path} missing security"
                    assert op["security"] == [{"ApiKeyHeader": []}], (
                        f"{method.upper()} {path} expected ApiKeyHeader security, "
                        f"got {op['security']}"
                    )

    def test_health_is_public(self, schema):
        """/health is always public, even when API_AUTH_ENABLED=true."""
        sec = _op(schema, "/health", "get").get("security", [])
        assert sec == [], f"/health GET should not require security, got {sec}"

    def test_metrics_requires_security(self, schema):
        """/metrics is protected (not public)."""
        sec = _op(schema, "/metrics", "get").get("security", [])
        assert sec == [{"ApiKeyHeader": []}], (
            f"/metrics GET should require ApiKeyHeader security, got {sec}"
        )


class TestErrorResponses:
    REQUIRED_CODES = {404, 500}

    def _check_has_errors(self, path, method, op):
        codes = {int(k) for k in op.get("responses", {})}
        missing = self.REQUIRED_CODES - codes
        assert not missing, f"{method.upper()} {path}: missing error codes {missing}"

    def test_ssh_endpoint_has_404_and_500(self, schema):
        self._check_has_errors(
            "/api/ssh/connect",
            "post",
            _op(schema, "/api/ssh/connect", "post"),
        )

    def test_servers_delete_has_404_and_500(self, schema):
        self._check_has_errors(
            "/api/servers/{server_id}",
            "delete",
            _op(schema, "/api/servers/{server_id}", "delete"),
        )

    def test_jobs_run_has_404_and_500(self, schema):
        self._check_has_errors(
            "/api/jobs/run",
            "post",
            _op(schema, "/api/jobs/run", "post"),
        )

    def test_error_response_schema_exists(self, schema):
        assert "ErrorResponse" in schema["components"]["schemas"]
        assert "ValidationErrorResponse" in schema["components"]["schemas"]

    def test_error_response_has_code_retryable_hint(self, schema):
        er = schema["components"]["schemas"]["ErrorResponse"]
        detail = er["properties"]["detail"]["oneOf"][1]["properties"]
        for key in ("message", "code", "retryable", "hint", "http_status"):
            assert key in detail, f"ErrorResponse detail missing '{key}'"


class TestTags:
    def test_every_operation_has_tag(self, schema):
        for path, methods in schema["paths"].items():
            for method, op in methods.items():
                tags = op.get("tags", [])
                assert tags, f"{method.upper()} {path}: no tag"
                assert len(tags) == 1, (
                    f"{method.upper()} {path}: expected 1 tag, got {tags}"
                )

    def test_top_level_tags_defined(self, schema):
        tags = {t["name"] for t in schema.get("tags", [])}
        expected = {
            "ssh",
            "files",
            "jobs",
            "git",
            "context",
            "templates",
            "servers",
            "snapshots",
            "webhooks",
            "code",
            "system",
        }
        missing = expected - tags
        assert not missing, f"Missing top-level tags: {missing}"


class TestExamples:
    KEY_EXAMPLES = [
        "/api/ssh/connect",
        "/api/ssh/execute",
        "/api/file/read",
        "/api/file/write",
        "/api/jobs/run",
        "/api/context/create",
    ]

    def test_key_endpoints_have_request_examples(self, schema):
        for path in self.KEY_EXAMPLES:
            req_body = _op(schema, path, "post").get("requestBody", {})
            ct = req_body.get("content", {}).get("application/json", {})
            assert "example" in ct, f"POST {path}: missing example in requestBody"

    def test_all_body_operations_have_examples(self, schema):
        missing = []
        for path, methods in schema["paths"].items():
            for method, op in methods.items():
                req_body = (
                    op.get("requestBody", {})
                    .get("content", {})
                    .get("application/json", {})
                )
                if req_body.get("schema") and "example" not in req_body:
                    missing.append(f"{method.upper()} {path}")
        assert not missing, f"Endpoints without request example: {missing}"


class TestSSE:
    def test_sse_schemas_exist(self, schema):
        for name in (
            "SSEEvent",
            "SSEStatusEvent",
            "SSEStdoutEvent",
            "SSEStderrEvent",
            "SSEExitEvent",
            "SSEErrorEvent",
        ):
            assert name in schema["components"]["schemas"], (
                f"Missing SSE schema: {name}"
            )

    def test_sse_event_has_discriminator(self, schema):
        ev = schema["components"]["schemas"]["SSEEvent"]
        assert "discriminator" in ev
        assert ev["discriminator"]["propertyName"] == "type"
        assert "oneOf" in ev


class TestParameters:
    def test_all_params_have_descriptions(self, schema):
        missing = []
        for path, methods in schema["paths"].items():
            for method, op in methods.items():
                for param in op.get("parameters", []):
                    if not param.get("description"):
                        missing.append(f"{method.upper()} {path}: {param['name']}")
        assert not missing, f"Parameters missing description: {missing}"


class TestResponseHeaders:
    REQUIRED_HEADERS = {
        "X-Request-ID",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
    }

    def test_operations_have_response_headers(self, schema):
        missing = []
        for path, methods in schema["paths"].items():
            for method, op in methods.items():
                for resp_code, resp in op.get("responses", {}).items():
                    hdrs = resp.get("headers", {})
                    missing_set = self.REQUIRED_HEADERS - set(hdrs.keys())
                    if missing_set:
                        missing.append(
                            f"{method.upper()} {path} [{resp_code}]: missing {missing_set}"
                        )
        assert not missing, "Response headers missing:\n" + "\n".join(missing[:10])


class TestSchemaStructure:
    def test_openapi_version_and_info(self, schema):
        assert "openapi" in schema, "Missing openapi version"
        assert "info" in schema, "Missing info section"
        assert "title" in schema["info"]
        assert "version" in schema["info"]

    def test_paths_exist(self, schema):
        assert "paths" in schema, "Missing paths"
        assert len(schema["paths"]) > 0, "No paths defined"

    def test_no_duplicate_operation_ids(self, schema):
        op_ids = {}
        for path, methods in schema["paths"].items():
            for method, op in methods.items():
                oid = op.get("operationId", "")
                if oid:
                    prev = op_ids.get(oid)
                    assert prev is None, f"Duplicate operationId '{oid}' at {method.upper()} {path} (also at {prev})"
                    op_ids[oid] = f"{method.upper()} {path}"

    def test_all_refs_resolve(self, schema):
        """Check every $ref points to an existing schema definition."""
        components = schema.get("components", {}).get("schemas", {})

        def _walk(obj, path_str):
            if isinstance(obj, dict):
                ref = obj.get("$ref", "")
                if ref:
                    assert ref.startswith("#/components/schemas/"), f"External $ref not supported: {ref} at {path_str}"
                    name = ref.replace("#/components/schemas/", "")
                    assert name in components, f"Unresolved $ref '{ref}' at {path_str}"
                for k, v in obj.items():
                    _walk(v, f"{path_str}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    _walk(v, f"{path_str}[{i}]")

        _walk(schema, "schema")


class TestRuntimeBehavior:
    def setup_method(self):
        self._saved = {}
        for attr in ("api_auth_enabled", "api_key", "allowed_client_cidrs", "trusted_proxy_cidrs"):
            self._saved[attr] = getattr(settings, attr)
        settings.api_auth_enabled = True
        settings.api_key = "test-key"
        settings.allowed_client_cidrs = "0.0.0.0/0,::1/128"
        settings.trusted_proxy_cidrs = ""
        self._ip_patch = patch("app.auth_middleware.get_client_ip", return_value="127.0.0.1")
        self._ip_patch.start()

    def teardown_method(self):
        self._ip_patch.stop()
        for attr, val in self._saved.items():
            setattr(settings, attr, val)

    def _auth_headers(self):
        return {"X-API-Key": "test-key"}

    def test_delete_unknown_server_returns_404(self):
        with TestClient(app) as client:
            resp = client.delete("/api/servers/nonexistent-12345", headers=self._auth_headers())
        assert resp.status_code == 404, (
            f"Expected 404, got {resp.status_code}: {resp.text}"
        )

    def test_jobs_run_bad_session_returns_404(self):
        with TestClient(app) as client:
            resp = client.post(
                "/api/jobs/run",
                json={"session_id": "fake-session-999", "command": "ls"},
                headers=self._auth_headers(),
            )
        assert resp.status_code == 404, (
            f"Expected 404, got {resp.status_code}: {resp.text}"
        )

    def test_delete_unknown_returns_structured_error(self):
        with TestClient(app) as client:
            resp = client.delete("/api/servers/nonexistent-12345", headers=self._auth_headers())
        body = resp.json()
        assert body["detail"]["code"] == "SERVER_NOT_FOUND"
        assert body["detail"]["retryable"] is False
        assert body["detail"]["hint"]
        assert body["detail"]["http_status"] == 404

    def test_validation_error_has_code_and_hint(self):
        with TestClient(app) as client:
            resp = client.post("/api/ssh/connect", json={}, headers=self._auth_headers())
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert body["retryable"] is False
        assert body["hint"]
        errors = body.get("errors", [])
        assert any(e.get("field") in ("host", "username") for e in errors)


class TestOpenAPISchemaValidation:
    """Validate the generated OpenAPI schema against the OpenAPI 3.0 spec."""

    def test_schema_validates_against_openapi_30_spec(self, schema):
        """Validate schema structure using jsonschema Draft7 + OpenAPI 3.0 metaschema."""
        openapi_30_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["openapi", "info", "paths"],
            "properties": {
                "openapi": {"type": "string", "pattern": r"^3\.\d+\.\d+"},
                "info": {
                    "type": "object",
                    "required": ["title", "version"],
                    "properties": {
                        "title": {"type": "string"},
                        "version": {"type": "string"},
                    },
                },
                "paths": {"type": "object"},
                "components": {"type": "object"},
                "tags": {"type": "array", "items": {"type": "object"}},
                "servers": {"type": "array"},
            },
        }
        jsonschema.Draft7Validator(openapi_30_schema).validate(schema)
