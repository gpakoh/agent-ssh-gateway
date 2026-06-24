#!/usr/bin/env python3
"""MCP Fleet healthcheck — one-shot diagnostics for all adapters.

Usage:
    python scripts/mcp_fleet_healthcheck.py

Exit code:
    0 — all checks passed
    1 — one or more checks failed
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from urllib.request import Request, urlopen

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
    Adapter("Gateway", "agent-ssh-gateway-mcp", "/etc/agent-ssh-gateway-mcp.env",
            "https://ssh.xloud.ru/mcp", 62, 8788, 0),
    Adapter("Context7", "agent-mcp-context7", "/etc/agent-mcp-context7.env",
            "https://ssh.xloud.ru/mcp/context7", 2, 8780, 8790),
    Adapter("GitHub", "agent-mcp-github", "/etc/agent-mcp-github.env",
            "https://ssh.xloud.ru/mcp/github", 8, 8781, 8791),
    Adapter("Gitea", "agent-mcp-gitea", "/etc/agent-mcp-gitea.env",
            "https://ssh.xloud.ru/mcp/gitea", 12, 8782, 8792),
    Adapter("Docker", "agent-mcp-docker", "/etc/agent-mcp-docker.env",
            "https://ssh.xloud.ru/mcp/docker", 7, 8783, 8793),
    Adapter("Postgres", "agent-mcp-postgres", "/etc/agent-mcp-postgres.env",
            "https://ssh.xloud.ru/mcp/postgres", 6, 8784, 8794),
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
    token_key = "MCP_PUBLIC_TOKEN"
    if "gateway" in env_file:
        token_key = "MCP_PUBLIC_TOKEN"
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
        out = subprocess.check_output(
            ["systemctl", "is-active", service],
            stderr=subprocess.STDOUT,
            timeout=10,
        ).decode().strip()
        if out == "active":
            return ok("active")
        return fail(f"not running ({out})")
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        return fail(str(e).split("\n")[0][:120])


def check_mcp_endpoint(url: str, token: str, expected: int) -> CheckResult:
    if not token:
        return fail("no token found")

    full_url = f"{url}?mcp_token={token}"

    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "healthcheck", "version": "1.0"},
        },
    }).encode()

    req = Request(
        full_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )

    try:
        resp = urlopen(req, timeout=15)
        resp.read()  # consume body before next request
        headers = {k.lower(): v for k, v in resp.headers.items()}

        sid = headers.get("mcp-session-id", "")
        if not sid:
            return fail("no session ID in response")

        tools_payload = json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }).encode()

        req2 = Request(
            full_url,
            data=tools_payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": sid,
            },
            method="POST",
        )
        resp2 = urlopen(req2, timeout=15)
        body2 = resp2.read().decode()

        tool_names = []
        for line in body2.split("\n"):
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:])
                    for t in obj.get("result", {}).get("tools", []):
                        tool_names.append(t["name"])
                except json.JSONDecodeError:
                    continue

        count = len(tool_names)

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
    if not token:
        return fail("no token — skipping nginx route check")
    try:
        req = Request(
            f"{url}?mcp_token={token}",
            headers={"Accept": "application/json, text/event-stream"},
            method="GET",
        )
        resp = urlopen(req, timeout=10)
        code = resp.status
        if code in (200, 405, 400, 401):
            return ok("route OK")
        elif code == 302:
            return fail("nginx returned 302 redirect — location mismatch")
        else:
            return fail(f"unexpected HTTP {code}")
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

    print(f"\n{BOLD}{'='*62}{RESET}")
    print(f"{BOLD}  MCP Fleet Healthcheck{' '*34}{RESET}")
    print(f"{BOLD}{'='*62}{RESET}")

    for a in ADAPTERS:
        r = results[a.name]
        label = f"{a.name:>{col_w}}"

        sys_ok = r["systemd"].passed
        env_ok = r["env_file"].passed
        mcp_ok = r["endpoint"].passed
        nginx_ok = r["nginx"].passed
        all_ok = sys_ok and env_ok and mcp_ok and nginx_ok

        tool_str = f"[{r['endpoint'].tool_count}/{a.expected_tools} tools]" if r['endpoint'].tool_count else ""

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

    print(f"  {'─'*58}")
    total = passed_all + failed_all
    if failed_all == 0:
        print(f"  {GREEN}{BOLD}All {total}/{total} adapters healthy{RESET}")
    else:
        print(f"  {RED}{BOLD}{passed_all}/{total} healthy, {failed_all} failing{RESET}")
        print(f"  {YELLOW}Run with --verbose for full details{RESET}")

    print()
    return passed_all, failed_all


def print_detail(results: dict[str, dict[str, CheckResult]]) -> None:
    print(f"\n{BOLD}{'='*62}{RESET}")
    print(f"{BOLD}  Detailed Results{' '*42}{RESET}")
    print(f"{BOLD}{'='*62}{RESET}")

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
