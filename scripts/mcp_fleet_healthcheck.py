#!/usr/bin/env python3
"""MCP Fleet healthcheck — one-shot diagnostics for all adapters.

Usage:
    python scripts/mcp_fleet_healthcheck.py

Exit code:
    0 — all checks passed
    1 — one or more checks failed
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass
class Adapter:
    name: str
    systemd_service: str
    env_file: str
    public_url: str
    expected_tools: int
    internal_port: int
    public_port: int


ADAPTERS: list[Adapter] = [
    Adapter(
        "Gateway",
        "agent-ssh-gateway-mcp",
        "/etc/agent-ssh-gateway-mcp.env",
        "https://ssh.xloud.ru/mcp",
        106,
        8788,
        0,
    ),
    Adapter(
        "Context7",
        "agent-mcp-context7",
        "/etc/agent-mcp-context7.env",
        "https://ssh.xloud.ru/mcp/context7",
        2,
        8780,
        8790,
    ),
    Adapter(
        "GitHub",
        "agent-mcp-github",
        "/etc/agent-mcp-github.env",
        "https://ssh.xloud.ru/mcp/github",
        8,
        8781,
        8791,
    ),
    Adapter(
        "Gitea",
        "agent-mcp-gitea",
        "/etc/agent-mcp-gitea.env",
        "https://ssh.xloud.ru/mcp/gitea",
        12,
        8782,
        8792,
    ),
    Adapter(
        "Docker",
        "agent-mcp-docker",
        "/etc/agent-mcp-docker.env",
        "https://ssh.xloud.ru/mcp/docker",
        7,
        8783,
        8793,
    ),
    Adapter(
        "Postgres",
        "agent-mcp-postgres",
        "/etc/agent-mcp-postgres.env",
        "https://ssh.xloud.ru/mcp/postgres",
        6,
        8784,
        8794,
    ),
]


@dataclass
class CheckResult:
    passed: bool
    detail: str = ""
    tool_count: int = 0


def ok(detail: str = "", tool_count: int = 0) -> CheckResult:
    return CheckResult(passed=True, detail=detail, tool_count=tool_count)


def fail(detail: str = "") -> CheckResult:
    return CheckResult(passed=False, detail=detail)


def get_token_from_env(env_file: str) -> str:
    """Read token for healthcheck.

    For gateway (oauth mode), prefers MCP_HEALTHCHECK_BEARER_TOKEN.
    Falls back to MCP_PUBLIC_TOKEN for fleet adapters.
    """
    token_key = "MCP_HEALTHCHECK_BEARER_TOKEN" if "gateway" in env_file else "MCP_PUBLIC_TOKEN"
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith(token_key + "="):
                    return line.split("=", 1)[1]
    except FileNotFoundError:
        pass
    return ""


def check_systemd(service: str) -> CheckResult:
    try:
        out = (
            subprocess.check_output(
                ["systemctl", "is-active", service],
                stderr=subprocess.STDOUT,
                timeout=10,
            )
            .decode()
            .strip()
        )
        if out == "active":
            return ok("active")
        return fail(f"not running ({out})")
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        return fail(str(e).split("\n")[0][:120])


def _mcp_request(
    full_url: str, body: dict, sid: str | None = None, token: str | None = None
) -> tuple[dict, str]:
    """Send JSON-RPC to an SSE MCP endpoint, read first SSE frame.

    Returns (parsed_result_dict, session_id).
    """
    from urllib.parse import urlparse

    parsed = urlparse(full_url)
    path_qs = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if sid:
        headers["Mcp-Session-Id"] = sid

    host = parsed.hostname
    port = parsed.port or 443
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=15)
    try:
        conn.request("POST", path_qs, json.dumps(body), headers)
        resp = conn.getresponse()

        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n\n" in buf:
                break

        raw = buf.decode("utf-8", errors="replace")
        result = None
        for line in raw.split("\n"):
            if line.startswith("data:"):
                result = json.loads(line[5:])
                break

        ret_sid = resp.getheader("mcp-session-id", "")
        resp.close()
        return result or {}, ret_sid
    finally:
        conn.close()


def check_mcp_endpoint(url: str, token: str, expected: int) -> CheckResult:
    if not token:
        return fail("no token found")

    try:
        result, sid = _mcp_request(
            url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "healthcheck", "version": "1.0"},
                },
            },
            token=token,
        )

        if not sid:
            return fail("no session ID in response")

        if "error" in result:
            return fail(result["error"].get("message", str(result["error"])))

        result2, _ = _mcp_request(
            url,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            sid=sid,
            token=token,
        )

        if "error" in result2:
            return fail(result2["error"].get("message", str(result2["error"])))

        tools = result2.get("result", {}).get("tools", [])
        count = len(tools)

        if count == expected:
            return ok(f"{count} tools", count)
        elif count > 0:
            return CheckResult(
                passed=False,
                detail=f"expected {expected} tools, got {count}",
                tool_count=count,
            )
        else:
            return fail("no tools returned")
    except Exception as e:
        return fail(str(e).split(":")[-1].strip()[:120])


def check_file_security(env_file: str) -> CheckResult:
    if not os.path.exists(env_file):
        return fail("not found")
    try:
        st = os.stat(env_file)
        perms = oct(st.st_mode & 0o777)
        if perms == "0o600":
            return ok(f"{env_file} chmod 600")
        return fail(f"{env_file} perms {perms}, expected 600")
    except OSError as e:
        return fail(str(e)[:120])


def check_nginx_route(url: str, token: str) -> CheckResult:
    """Verify nginx proxy is alive using a lightweight POST (not SSE GET).

    A GET to /mcp creates an SSE session that never ends (timeout).
    We use a POST with a minimal JSON-RPC ping instead.
    """
    if not token:
        return fail("no token — skipping nginx route check")
    try:
        parsed = urlparse(url)
        path_qs = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }

        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "healthcheck", "version": "1.0"},
                },
            }
        )

        host = parsed.hostname
        port = parsed.port or 443
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=10)
        try:
            conn.request("POST", path_qs, body, headers)
            resp = conn.getresponse()
            code = resp.status
            resp.close()

            if code in (200, 202, 400, 401):
                return ok("route OK")
            elif code == 302:
                return fail("nginx returned 302 redirect — location mismatch")
            else:
                return fail(f"unexpected HTTP {code}")
        finally:
            conn.close()
    except Exception as e:
        err = str(e)
        if "HTTP Error" in err:
            code = err.split("HTTP Error ")[1].split(":")[0]
            if code in ("405", "400", "401"):
                return ok("route OK")
        return fail(err.split(":")[-1].strip()[:120])


def run_checks() -> dict[str, dict[str, CheckResult]]:
    results: dict[str, dict[str, CheckResult]] = {}

    for a in ADAPTERS:
        results[a.name] = {}
        token = get_token_from_env(a.env_file)

        # systemd
        results[a.name]["systemd"] = check_systemd(a.systemd_service)

        # env file security
        results[a.name]["env_file"] = check_file_security(a.env_file)

        # MCP endpoint tools/list (Gateway already has /mcp in URL)
        results[a.name]["endpoint"] = check_mcp_endpoint(a.public_url, token, a.expected_tools)
        results[a.name]["nginx"] = check_nginx_route(a.public_url, token)

    return results


def print_report(results: dict[str, dict[str, CheckResult]]) -> tuple[int, int]:
    passed_all = 0
    failed_all = 0
    col_w = max(len(a.name) for a in ADAPTERS) + 1

    print(f"\n{BOLD}{'=' * 62}{RESET}")
    print(f"{BOLD}  MCP Fleet Healthcheck{' ' * 34}{RESET}")
    print(f"{BOLD}{'=' * 62}{RESET}")

    for a in ADAPTERS:
        r = results[a.name]
        label = f"{a.name:>{col_w}}"

        sys_ok = r["systemd"].passed
        env_ok = r["env_file"].passed
        mcp_ok = r["endpoint"].passed
        nginx_ok = r["nginx"].passed
        all_ok = sys_ok and env_ok and mcp_ok and nginx_ok

        tool_str = (
            f"[{r['endpoint'].tool_count}/{a.expected_tools} tools]"
            if r["endpoint"].tool_count
            else ""
        )

        if all_ok:
            status = f"{GREEN}OK{RESET}"
            passed_all += 1
        else:
            status = f"{RED}FAIL{RESET}"
            failed_all += 1

        print(f"  {status}  {label}  {tool_str}")

        if not sys_ok:
            print(f"         {RED}systemd: {r['systemd'].detail}{RESET}")
        if not env_ok:
            print(f"         {RED}env:     {r['env_file'].detail}{RESET}")
        if not mcp_ok:
            print(f"         {RED}endpoint:{r['endpoint'].detail}{RESET}")
        if not nginx_ok:
            print(f"         {RED}nginx:   {r['nginx'].detail}{RESET}")

    print(f"  {'─' * 58}")
    total = passed_all + failed_all
    if failed_all == 0:
        print(f"  {GREEN}{BOLD}All {total}/{total} adapters healthy{RESET}")
    else:
        print(f"  {RED}{BOLD}{passed_all}/{total} healthy, {failed_all} failing{RESET}")
        print(f"  {YELLOW}Run with --verbose for full details{RESET}")

    print()
    return passed_all, failed_all


def print_detail(results: dict[str, dict[str, CheckResult]]) -> None:
    print(f"\n{BOLD}{'=' * 62}{RESET}")
    print(f"{BOLD}  Detailed Results{' ' * 42}{RESET}")
    print(f"{BOLD}{'=' * 62}{RESET}")

    for a in ADAPTERS:
        r = results[a.name]
        print(f"\n  {BOLD}{a.name}{RESET}")
        for check_name in ("systemd", "env_file", "nginx"):
            cr = r[check_name]
            icon = f"{GREEN}✓{RESET}" if cr.passed else f"{RED}✗{RESET}"
            print(f"    {icon} {check_name}: {cr.detail[:160]}")

        cr = r["endpoint"]
        icon = f"{GREEN}✓{RESET}" if cr.passed else f"{RED}✗{RESET}"
        tools_str = f" ({cr.tool_count} tools)" if cr.tool_count else ""
        print(f"    {icon} endpoint:{cr.detail[:160]}{tools_str}")
        print(f"      url: {a.public_url}")
        print(f"      env: {a.env_file}")
        print(f"      internal port: {a.internal_port}, public port: {a.public_port}")


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    results = run_checks()
    passed, failed = print_report(results)

    if verbose:
        print_detail(results)

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
