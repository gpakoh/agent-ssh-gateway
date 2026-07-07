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
