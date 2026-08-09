import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NGINX_TEMPLATE = ROOT / "nginx-ssh-gateway.conf"


def _template() -> str:
    return NGINX_TEMPLATE.read_text(encoding="utf-8")


def test_api_key_placeholder_is_only_in_proxy_locations():
    text = _template()
    assert text.count("__API_KEY__") == 5


def test_default_location_uses_mtls_bypass_or_authelia_fallback():
    text = _template()
    match = re.search(r"location / \{(?P<body>.*?)\n    \}", text, re.S)
    assert match is not None

    body = match.group("body")
    assert '$ssl_client_verify != "SUCCESS"' in body
    assert "rewrite ^(.*)$ /_mtls-auth$1 last;" in body
    assert 'proxy_set_header X-API-Key "__API_KEY__";' in body


def test_authelia_fallback_location_is_internal_and_authenticated():
    text = _template()
    match = re.search(r"location /_mtls-auth/ \{(?P<body>.*?)\n    \}", text, re.S)
    assert match is not None

    body = match.group("body")
    assert "internal;" in body
    assert "auth_request /authelia;" in body
    assert "error_page 401 =302 https://__AUTH_DOMAIN__/" in body
    assert 'proxy_set_header X-API-Key "__API_KEY__";' in body


def test_hsts_header_is_present_on_the_https_server_block():
    """P1 MAJOR audit finding: no Strict-Transport-Security header meant
    a browser tricked into an initial plain-HTTP request had no defense
    until the port-80 redirect completed."""
    text = _template()
    assert 'add_header Strict-Transport-Security "max-age=31536000" always;' in text


def test_health_is_split_from_nginx_only_liveness():
    """MINOR Ops audit finding: /health used to `return 200` unconditionally
    from nginx itself, so a dead/degraded backend (Gateway process down,
    Redis/Postgres unreachable) was invisible to external monitoring --
    /nginx-health keeps the old static-liveness behavior under its own
    name; /health now actually proxies to the backend.
    """
    text = _template()

    liveness_match = re.search(r"location /nginx-health \{(?P<body>.*?)\n    \}", text, re.S)
    assert liveness_match is not None
    assert "return 200 '{\"status\":\"ok\"}';" in liveness_match.group("body")

    health_match = re.search(r"location /health \{(?P<body>.*?)\n    \}", text, re.S)
    assert health_match is not None
    body = health_match.group("body")
    assert "return 200" not in body
    assert "proxy_pass http://__BACKEND_IP__:8085/health;" in body
