"""Remote Streamable HTTP MCP server for ChatGPT Developer Mode.

Architecture:
  public :8788  →  OAuthProxyMiddleware  →  reverse proxy  →  internal MCP :8789

Auth modes (MCP_AUTH_MODE env var):
  oauth  — only Bearer token (default, mcp_token rejected)
  token  — legacy ?mcp_token= query param (rollback only)

OAuth paths (/.well-known/, /oauth/) are always public to enable
the OAuth authorization flow.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parents[1]
MCP_SERVER_DIR = EXAMPLES_DIR / "mcp_server"

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent / ".env")
load_dotenv(MCP_SERVER_DIR / ".env", override=False)

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import (  # noqa: E402
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

sys.path.insert(0, str(MCP_SERVER_DIR))
sys.path.insert(0, str(EXAMPLES_DIR.parent))

from tool_modes import is_mcp_client_safe_mode  # noqa: E402
from tool_scopes import (  # noqa: E402
    check_fleet_route,
    extract_tool_from_body,
    get_profile_scopes,
    get_required_scopes,
    has_required_scope,
)

_spec = importlib.util.spec_from_file_location("mcp_server_module", MCP_SERVER_DIR / "server.py")
assert _spec is not None, f"Could not find spec for {MCP_SERVER_DIR / 'server.py'}"
_mcp_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mcp_mod)
mcp = _mcp_mod.mcp

MCP_PUBLIC_URL = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")
prov = getattr(_mcp_mod, "_auth_provider", None)
if prov is not None and MCP_PUBLIC_URL:
    prov.public_base_url = MCP_PUBLIC_URL

MCP_INTERNAL_HOST = os.environ.get("MCP_INTERNAL_HOST", "127.0.0.1")
MCP_INTERNAL_PORT = int(os.environ.get("MCP_INTERNAL_PORT", "8789"))
MCP_INTERNAL_URL = f"http://{MCP_INTERNAL_HOST}:{MCP_INTERNAL_PORT}"

logger = logging.getLogger("mcp_client_remote")

MCP_AUTH_MODE = os.environ.get("MCP_AUTH_MODE", "oauth").strip().lower()
if MCP_AUTH_MODE not in ("token", "oauth"):
    raise ValueError(f"Invalid MCP_AUTH_MODE={MCP_AUTH_MODE!r}; expected one of ('token', 'oauth')")

MCP_PUBLIC_TOKEN = os.environ.get("MCP_PUBLIC_TOKEN", "")

MCP_SCOPE_ENFORCEMENT = os.environ.get("MCP_SCOPE_ENFORCEMENT", "enforce").strip().lower()
if MCP_SCOPE_ENFORCEMENT not in ("off", "audit", "enforce"):
    raise ValueError(
        f"Invalid MCP_SCOPE_ENFORCEMENT={MCP_SCOPE_ENFORCEMENT!r}; expected off|audit|enforce"
    )

MCP_DEFAULT_ACCESS_PROFILE = (
    os.environ.get("MCP_ACCESS_PROFILE")
    or os.environ.get("MCP_DEFAULT_ACCESS_PROFILE")
    or "operator"
)
# Fail fast on a typo'd profile at startup rather than at first request --
# get_profile_scopes() itself raises for an unknown profile (no more
# silent fallback to "operator"), so this would otherwise only surface
# the first time _get_token_scopes() resolved MCP_PUBLIC_TOKEN.
get_profile_scopes(MCP_DEFAULT_ACCESS_PROFILE)
MCP_CLIENT_SAFE_MODE = is_mcp_client_safe_mode()

MCP_AUTHORIZE_PASSWORD = os.environ.get("MCP_AUTHORIZE_PASSWORD", "")

OAUTH_PUBLIC_PREFIXES = (
    "/.well-known/",
    "/oauth/",
)
# Advertised as bare top-level endpoints in openid_configuration()'s
# authorization_endpoint/token_endpoint/registration_endpoint (see below),
# in addition to their /oauth/-nested forms -- both must stay public.
OAUTH_PUBLIC_EXACT_OR_NESTED = (
    "/authorize",
    "/token",
    "/register",
    "/health",
)


def _is_oauth_public_path(path: str) -> bool:
    """OAuth endpoints don't require mcp_token or Bearer.

    Boundary-aware on purpose: a bare `path.startswith(("/authorize",
    "/token", "/register", "/health"))` would silently treat any future
    route merely starting with one of those substrings (e.g.
    /authorized_keys, /tokens-export, /registered-hosts,
    /health-debug-internal) as public too, with no way to notice until it
    ships. Each of these must match exactly or be followed by "/".
    """
    if path.startswith(OAUTH_PUBLIC_PREFIXES):
        return True
    return any(path == p or path.startswith(p + "/") for p in OAUTH_PUBLIC_EXACT_OR_NESTED)


class OAuthProxyMiddleware(BaseHTTPMiddleware):
    """Require Bearer token or mcp_token (token mode only).

    mode=oauth: only Bearer (default, mcp_token rejected)
    mode=token: Bearer or ?mcp_token= with MCP_PUBLIC_TOKEN (rollback)
    """

    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path

        # OAuth public paths — always pass through
        if _is_oauth_public_path(path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        mcp_token = request.query_params.get("mcp_token", "")
        has_bearer = auth_header.startswith("Bearer ")
        has_mcp_token = bool(mcp_token)

        if MCP_AUTH_MODE == "oauth":
            if has_bearer:
                token_preview = auth_header[7:19] + "..."
                logger.info(f"AUTH OK path={path} token={token_preview}")
                request.state.auth_token = auth_header.removeprefix("Bearer ")
                return await call_next(request)
            if has_mcp_token:
                logger.warning(f"AUTH REJECT mcp_token path={path}")
                return JSONResponse(
                    {"error": "mcp_token is not accepted in oauth mode"},
                    status_code=401,
                    headers={"WWW-Authenticate": f'Bearer realm="mcp", resource="{MCP_PUBLIC_URL}/"'},
                )
            logger.warning(f"AUTH REJECT no_bearer path={path} method={request.method}")
            return JSONResponse(
                {"error": "Missing Authorization: Bearer header"},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer realm="mcp", resource="{MCP_PUBLIC_URL}/"'},
            )

        # token mode: Bearer or mcp_token query param
        if has_bearer:
            token = auth_header.removeprefix("Bearer ")
            if token == MCP_PUBLIC_TOKEN:
                request.state.auth_token = token
                return await call_next(request)
            return JSONResponse(
                {"error": "invalid Bearer token"},
                status_code=403,
            )
        if has_mcp_token:
            if mcp_token == MCP_PUBLIC_TOKEN:
                request.state.auth_token = mcp_token
                return await call_next(request)
            return JSONResponse(
                {"error": "invalid mcp_token"},
                status_code=403,
            )
        return JSONResponse(
            {"error": "Missing Authorization header or mcp_token"},
            status_code=401,
        )


def _default_access_profile() -> str:
    """Return the effective default access profile.

    Safe mode (MCP_CLIENT_SAFE_MODE=true) forces the ``mcp_client_safe``
    profile, so a misconfigured MCP_ACCESS_PROFILE can never widen the
    default token's scopes beyond the safe bundle.
    """
    if MCP_CLIENT_SAFE_MODE:
        return "mcp_client_safe"
    return MCP_DEFAULT_ACCESS_PROFILE


async def _get_token_scopes(auth_token: str | None) -> list[str]:
    """Resolve token scopes from auth provider or fallback profile."""
    if not auth_token:
        return []

    try:
        prov = getattr(_mcp_mod, "_auth_provider", None)
        if prov and hasattr(prov, "load_access_token"):
            token_info = await prov.load_access_token(auth_token)
            if token_info:
                return getattr(token_info, "scopes", [])
    except Exception:
        pass

    # The shared default token (token mode) resolves to the configured
    # default access profile; any other unknown token stays fail-closed.
    if MCP_PUBLIC_TOKEN and auth_token == MCP_PUBLIC_TOKEN:
        return list(get_profile_scopes(_default_access_profile()))
    return []


async def _check_tool_scope(request: Request, path: str, body: bytes) -> JSONResponse | None:
    """Check scope enforcement for a request. Returns blocking response or None."""
    if MCP_SCOPE_ENFORCEMENT == "off":
        return None

    auth_token = getattr(request.state, "auth_token", None)
    token_scopes = await _get_token_scopes(auth_token)

    # Fleet route check
    allowed, scope = check_fleet_route(path, token_scopes)
    if not allowed:
        msg = f"SCOPE_DENIED fleet_route={path} required={scope} token_scopes={token_scopes}"
        print(msg, file=sys.stderr)
        if MCP_SCOPE_ENFORCEMENT == "enforce":
            return JSONResponse(
                {"error": "insufficient_scope", "required_scope": scope},
                status_code=403,
            )
        return None

    # Tool-level check (JSON-RPC tools/call)
    tool_name = extract_tool_from_body(body)
    if not tool_name:
        return None

    required = get_required_scopes(tool_name)
    if has_required_scope(token_scopes, tool_name):
        if MCP_SCOPE_ENFORCEMENT == "audit":
            print(
                f"SCOPE_ALLOWED tool={tool_name} required={required} token_scopes={token_scopes}",
                file=sys.stderr,
            )
        return None

    # Denied
    msg = f"SCOPE_DENIED tool={tool_name} required={required} token_scopes={token_scopes}"
    print(msg, file=sys.stderr)

    if MCP_SCOPE_ENFORCEMENT == "enforce":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32000,
                    "message": f"insufficient_scope: requires one of {required}",
                },
            },
            status_code=403,
        )

    return None


async def proxy_request(request: Request) -> StreamingResponse | JSONResponse | Response:
    """Proxy an HTTP request to the internal MCP server."""
    target_path = request.url.path
    if target_path == "/":
        target_path = "/mcp"
    url = f"{MCP_INTERNAL_URL}{target_path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    body = await request.body()
    headers = dict(request.headers)
    headers.pop("host", None)

    auth_token = getattr(request.state, "auth_token", None)

    # Scope check
    scope_block = await _check_tool_scope(request, request.url.path, body)
    if scope_block is not None:
        return scope_block

    if auth_token and "authorization" not in {k.lower() for k in headers}:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                content=body,
                headers=headers,
            )
            # Log the path only, never `url` -- in token rollback mode `url`
            # carries the full query string, including a live ?mcp_token=
            # secret verbatim, straight into the application log at INFO.
            logger.info(
                f"PROXY {request.method} {request.url.path} -> {resp.status_code}"
            )
            if resp.status_code == 401:
                resp_body = await resp.aread()
                logger.warning(f"PROXY 401 body={resp_body[:500]!r}")
                return Response(content=resp_body, status_code=resp.status_code, headers=dict(resp.headers))
            return StreamingResponse(
                content=resp.aiter_bytes(),
                status_code=resp.status_code,
                headers=dict(resp.headers),
            )
    except httpx.RequestError as exc:
        return JSONResponse(
            {"error": f"Upstream unreachable: {exc}"},
            status_code=502,
        )


CONSENT_HTML = """<!DOCTYPE html>
<html lang="ru">
<head><meta charset="utf-8"><title>Authorization — MCP Gateway</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0f1117; color: #e1e4e8; display: flex; align-items: center;
         justify-content: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px;
          padding: 40px; max-width: 440px; width: 100%; margin: 20px; }}
  h1 {{ font-size: 22px; margin-bottom: 8px; }}
  p {{ color: #8b949e; font-size: 14px; margin-bottom: 24px; line-height: 1.5; }}
  label {{ display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; color: #c9d1d9; }}
  input[type=password] {{ width: 100%; padding: 10px 12px; background: #0d1117;
         border: 1px solid #30363d; border-radius: 8px; color: #e1e4e8; font-size: 15px;
         outline: none; transition: border-color .2s; }}
  input[type=password]:focus {{ border-color: #58a6ff; }}
  .error {{ color: #f85149; font-size: 13px; margin-top: 12px; display: none; }}
  .error.visible {{ display: block; }}
  .scope-list {{ list-style: none; background: #0d1117; border: 1px solid #30363d;
                 border-radius: 8px; padding: 10px 14px; margin-bottom: 20px;
                 font-family: ui-monospace, monospace; font-size: 13px; color: #c9d1d9; }}
  .scope-list li {{ padding: 2px 0; }}
  button {{ width: 100%; padding: 10px; margin-top: 20px; background: #238636;
           border: none; border-radius: 8px; color: #fff; font-size: 15px; font-weight: 500;
           cursor: pointer; transition: background .2s; }}
  button:hover {{ background: #2ea043; }}
  button:disabled {{ opacity: .6; cursor: not-allowed; }}
</style></head>
<body>
<div class="card">
  <h1>Authorize MCP Gateway</h1>
  <p>Enter the authorization password to connect this MCP server to ChatGPT.</p>
  <label>Requested access:</label>
  <ul class="scope-list">{scope_list}</ul>
  <form method="post" id="auth-form">
    <input type="hidden" name="client_id" value="{client_id}">
    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
    <input type="hidden" name="scope" value="{scope}">
    <input type="hidden" name="state" value="{state}">
    <input type="hidden" name="code_challenge" value="{code_challenge}">
    <input type="hidden" name="resource" value="{resource}">
    <label for="password">Authorization Password</label>
    <input type="password" id="password" name="password"
           placeholder="Enter password" autofocus required>
    <div class="error" id="error-msg">{error}</div>
    <button type="submit" id="submit-btn">Authorize</button>
  </form>
</div>
<script>
  const err = document.getElementById('error-msg');
  if (err.textContent.trim()) err.classList.add('visible');
  document.getElementById('auth-form').addEventListener('submit', function() {{
    document.getElementById('submit-btn').disabled = true;
    document.getElementById('submit-btn').textContent = 'Authorizing…';
  }});
</script>
</body>
</html>"""


async def consent_handler(request: Request):
    import html as html_lib
    from urllib.parse import urlencode, urlparse, urlunparse

    if request.method == "GET":
        # Reflected XSS: these come straight from attacker-controlled query
        # params on a public, unauthenticated endpoint (/oauth/consent is
        # always public — see OAUTH_PUBLIC_PREFIXES) and used to go straight
        # into CONSENT_HTML.format() unescaped. `error` in particular renders
        # in a plain text context (<div id="error-msg">{error}</div>), so
        # e.g. ?error=<script>...</script> executed verbatim in the victim's
        # browser on the real consent page they'd see while authorizing a
        # real MCP client — a crafted link could exfiltrate the password
        # they type into this exact form. html.escape() everything that
        # goes into the template, not just the ones that looked risky.
        client_id = html_lib.escape(request.query_params.get("client_id", ""))
        redirect_uri = html_lib.escape(request.query_params.get("redirect_uri", ""))
        raw_scope = request.query_params.get("scope", "mcp:read mcp:project")
        scope = html_lib.escape(raw_scope)
        # The operator authorizing this client previously never saw which
        # scopes it was requesting -- the scope value only traveled inside
        # a hidden form field. mcp:execute/mcp:admin/etc. are all valid
        # requestable scopes; render each one visibly so "Authorize" isn't
        # a blind click.
        scope_list = "".join(
            f"<li>{html_lib.escape(s)}</li>" for s in raw_scope.split() if s
        ) or "<li>(none)</li>"
        state = html_lib.escape(request.query_params.get("state", ""))
        code_challenge = html_lib.escape(request.query_params.get("code_challenge", ""))
        resource = html_lib.escape(request.query_params.get("resource", ""))
        error = html_lib.escape(request.query_params.get("error", ""))
        page_html = CONSENT_HTML.format(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            scope_list=scope_list,
            state=state,
            code_challenge=code_challenge,
            resource=resource,
            error=error,
        )
        return HTMLResponse(page_html, status_code=200)

    form = await request.form()
    password = form.get("password", "")
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    scope_str: str = str(form.get("scope", "mcp:read mcp:project"))
    state = form.get("state", "")
    code_challenge = form.get("code_challenge", "")
    resource = form.get("resource", "")

    if not password or not secrets.compare_digest(str(password), MCP_AUTHORIZE_PASSWORD):
        from urllib.parse import urlencode

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope_str,
            "state": state,
            "code_challenge": code_challenge,
            "resource": resource,
            "error": "Invalid password. Try again.",
        }
        return RedirectResponse(url="/oauth/consent?" + urlencode(params), status_code=303)

    from examples.mcp_server.oauth_provider import _parse_scopes

    prov = getattr(_mcp_mod, "_auth_provider", None)
    if not prov:
        return JSONResponse({"error": "OAuth provider not available"}, status_code=500)

    scopes = _parse_scopes(scope_str)
    try:
        result = prov.create_authorization_code(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state,
            scopes=scopes,
        )
    except ValueError as exc:
        from urllib.parse import urlencode

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope_str,
            "state": state,
            "code_challenge": code_challenge,
            "resource": resource,
            "error": f"Requested scopes not allowed: {exc}",
        }
        return RedirectResponse(url="/oauth/consent?" + urlencode(params), status_code=303)
    parsed = urlparse(redirect_uri)
    qs = {}
    if parsed.query:
        for part in parsed.query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                qs[k] = v
    qs["code"] = result["code"]
    qs["state"] = state
    return RedirectResponse(url=urlunparse(parsed._replace(query=urlencode(qs))), status_code=303)


async def openid_configuration(request: Request) -> JSONResponse:
    """Return minimal OpenID Connect discovery metadata."""
    base = MCP_PUBLIC_URL.rstrip("/")
    scopes = getattr(_mcp_mod, "SUPPORTED_SCOPES", None)
    if scopes is None:
        scopes = [
            "mcp:read", "mcp:project", "mcp:handoff", "mcp:agent-run",
            "mcp:execute", "mcp:repo", "mcp:docker", "mcp:postgres",
            "mcp:docs", "mcp:admin",
        ]
    return JSONResponse({
        "issuer": f"{base}/",
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "scopes_supported": scopes,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
        "code_challenge_methods_supported": ["S256"],
    })


def create_proxy_app() -> Starlette:
    """Create auth-guarded proxy to internal MCP server."""
    proxy = Starlette()
    proxy.add_middleware(OAuthProxyMiddleware)
    proxy.add_route("/oauth/consent", consent_handler, methods=["GET", "POST"])
    proxy.add_route("/.well-known/openid-configuration", openid_configuration, methods=["GET"])
    proxy.add_route("/", proxy_request, methods=["GET", "POST", "DELETE"])
    proxy.add_route("/{path:path}", proxy_request, methods=["GET", "POST", "DELETE"])
    return proxy


proxy_app = create_proxy_app()


def run():
    """Start both internal MCP server and public proxy."""
    import threading

    internal_host = MCP_INTERNAL_HOST
    internal_port = MCP_INTERNAL_PORT
    public_host = os.environ.get("MCP_HOST", "127.0.0.1")
    public_port = int(os.environ.get("MCP_PORT", "8788"))

    mcp.settings.host = internal_host
    mcp.settings.port = internal_port

    t = threading.Thread(
        target=mcp.run,
        kwargs={"transport": "streamable-http"},
        daemon=True,
    )
    t.start()

    print(f"  MCP internal : {internal_host}:{internal_port}", file=sys.stderr)
    print(f"  MCP public   : {public_host}:{public_port}", file=sys.stderr)
    print(f"  auth mode    : {MCP_AUTH_MODE}", file=sys.stderr)
    print(f"  scope enforce: {MCP_SCOPE_ENFORCEMENT}", file=sys.stderr)
    print(f"  safe mode    : {MCP_CLIENT_SAFE_MODE}", file=sys.stderr)
    print(f"  default prof : {_default_access_profile()}", file=sys.stderr)
    tok_display = MCP_PUBLIC_TOKEN[:8] + "..." if MCP_PUBLIC_TOKEN else "(not set)"
    print(f"  mcp_token    : {tok_display}", file=sys.stderr)

    uvicorn.run(proxy_app, host=public_host, port=public_port)


if __name__ == "__main__":
    run()
