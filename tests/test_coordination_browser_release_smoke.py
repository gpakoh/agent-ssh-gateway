"""Release qualification for the admin-only read-only Coordination browser."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "coordination_browser_smoke.py"
spec = importlib.util.spec_from_file_location("coordination_browser_smoke", SCRIPT)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

NO_TOKEN_DETAIL = "Coordination read credential is not configured"
COLUMNS = [
    {"state": state, "count": 0, "cards": []}
    for state in ("open", "accepted", "waiting_review", "resolved")
]


def _responses(*, token: bool = False, unauthorized: bool = False, bad_html: bool = False):
    def get(path: str, *, key: str | None = None):
        if key is None:
            if unauthorized:
                return 200, {}, b"public board exposure"
            return 401, {"cache-control": "no-store"}, b'{"detail":"unauthorized"}'
        if path == "/coordination":
            html = b"Py Coordination" if not bad_html else b"other page"
            return 200, {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"}, html
        if path == smoke._BOARD_PATH:
            if token:
                return 200, {"cache-control": "no-store"}, json.dumps({
                    "read_only": True, "columns": COLUMNS, "count": 0,
                    "project": "agent-ssh-gateway",
                }).encode()
            return 503, {}, json.dumps({"detail": NO_TOKEN_DETAIL}).encode()
        raise AssertionError("unexpected path")
    return get


@pytest.mark.parametrize("configured", [False, True])
def test_browser_auth_and_data_success(monkeypatch, configured: bool):
    seen = []
    fake = _responses(token=configured)

    def wrapped(path: str, *, key=None):
        seen.append((path, key))
        return fake(path, key=key)

    monkeypatch.setattr(smoke, "_get", wrapped)
    smoke._probe("protected-smoke-key", credential_configured=configured)
    assert [x[0] for x in seen] == [
        "/coordination", smoke._BOARD_PATH, "/coordination", smoke._BOARD_PATH,
    ]
    assert [x[1] for x in seen] == [None, None, "protected-smoke-key", "protected-smoke-key"]


def test_public_browser_route_is_release_failure(monkeypatch):
    monkeypatch.setattr(smoke, "_get", _responses(unauthorized=True))
    with pytest.raises(ValueError, match="unauthenticated"):
        smoke._probe("secret", credential_configured=False)


def test_html_route_must_serve_expected_page(monkeypatch):
    monkeypatch.setattr(smoke, "_get", _responses(bad_html=True))
    with pytest.raises(ValueError, match="content"):
        smoke._probe("secret", credential_configured=False)


def test_read_token_absence_must_be_explicit_not_generic_503(monkeypatch):
    fake = _responses()

    def altered(path: str, *, key=None):
        if key is not None and path == smoke._BOARD_PATH:
            return 503, {}, b'{"detail":"random server error"}'
        return fake(path, key=key)

    monkeypatch.setattr(smoke, "_get", altered)
    with pytest.raises(ValueError, match="fail closed"):
        smoke._probe("secret", credential_configured=False)


def test_configured_token_must_return_valid_kanban(monkeypatch):
    fake = _responses(token=True)

    def altered(path: str, *, key=None):
        if key is not None and path == smoke._BOARD_PATH:
            return 200, {"cache-control": "no-store"}, b'{"read_only":true,"columns":[],"count":0}'
        return fake(path, key=key)

    monkeypatch.setattr(smoke, "_get", altered)
    with pytest.raises(ValueError, match="column contract"):
        smoke._probe("secret", credential_configured=True)


def test_missing_gateway_key_does_not_open_connection(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    with patch.object(smoke, "_get") as probe:
        assert smoke.main() == 1
        probe.assert_not_called()


def test_main_does_not_print_tokens_or_exception_payload(monkeypatch, capsys):
    monkeypatch.setenv("API_KEY", "private-master-identity")
    monkeypatch.setenv("COORDINATION_GITEA_READ_TOKEN", "private-gitea-pat")

    def denied(_key, *, credential_configured):
        assert credential_configured
        raise RuntimeError("private-master-identity private-gitea-pat")


    monkeypatch.setattr(smoke, "_probe", denied)
    assert smoke.main() == 1
    captured = capsys.readouterr()
    assert "private-master-identity" not in captured.err
    assert "private-gitea-pat" not in captured.err


def test_host_smoke_wiring_and_docker_image_copy():
    root = Path(__file__).resolve().parents[1]
    deploy = (root / "scripts/deploy-from-registry.sh").read_text(encoding="utf-8")
    dockerfile = (root / "docker/Dockerfile").read_text(encoding="utf-8")
    assert "docker exec web-ssh-gateway python3 scripts/coordination_browser_smoke.py" in deploy
    assert "COPY --chown=appuser:appuser scripts/coordination_browser_smoke.py" in dockerfile
    assert 'if [ "$expected_sha" != "$DEPLOY_TAG" ]; then' in deploy
    assert "SKIP (prior rollback image)" in deploy
    assert deploy.index("scripts/gateway_black_box_smoke.py") < deploy.index(
        "scripts/coordination_browser_smoke.py"
    ) < deploy.index("scripts/mcp_black_box_smoke.py")


def test_http_transport_is_localhost_get_only_and_bounded(monkeypatch):
    requests = []

    class Reply:
        status = 200

        def getheaders(self):
            return [("Content-Type", "text/html"), ("Cache-Control", "no-store")]

        def read(self, limit):
            assert limit == smoke._MAX_BODY + 1
            return b"Py Coordination"

    class Connection:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("127.0.0.1", 8085, smoke._TIMEOUT)

        def request(self, method, path, headers):
            requests.append((method, path, headers))

        def getresponse(self):
            return Reply()

        def close(self):
            pass

    monkeypatch.setattr(smoke.http.client, "HTTPConnection", Connection)
    status, headers, body = smoke._get("/coordination", key="fixture-key")
    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert body == b"Py Coordination"
    assert requests == [("GET", "/coordination", {"Accept": "application/json", "X-API-Key": "fixture-key"})]
