"""Self-test diagnostics for the experimental MCP server."""

from __future__ import annotations

from typing import Any, Literal

from command_policy import CommandPolicyError, validate_readonly_command
from gateway_client import GatewayClient, GatewayClientError
from tool_modes import get_tool_mode, tools_for_mode

CheckStatus = Literal["pass", "warn", "fail"]


def check_result(
    name: str,
    status: CheckStatus,
    detail: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "data": data or {},
    }


def overall_status(checks: list[dict[str, Any]]) -> CheckStatus:
    if any(c["status"] == "fail" for c in checks):
        return "fail"
    if any(c["status"] == "warn" for c in checks):
        return "warn"
    return "pass"


def run_self_test(client: GatewayClient) -> dict[str, Any]:
    """Run read-only diagnostics for the MCP gateway example."""
    checks: list[dict[str, Any]] = []

    # tool_mode_valid + expected_tool_count
    try:
        mode = get_tool_mode()
        tools = tools_for_mode(mode)
        checks.append(
            check_result(
                "tool_mode_valid",
                "pass",
                f"Tool mode is {mode}.",
                {"mode": mode, "tool_count": len(tools), "tools": sorted(tools)},
            )
        )
        checks.append(
            check_result(
                "expected_tool_count",
                "pass",
                f"{len(tools)} tools configured for mode {mode}.",
                {"tool_count": len(tools)},
            )
        )
    except Exception as exc:
        checks.append(check_result("tool_mode_valid", "fail", str(exc)))

    # api_key_present
    if client.api_key:
        checks.append(check_result("api_key_present", "pass", "GATEWAY_API_KEY is configured."))
    else:
        checks.append(check_result("api_key_present", "fail", "GATEWAY_API_KEY is missing."))

    # session_id_present
    if client.session_id:
        checks.append(
            check_result("session_id_present", "pass", "GATEWAY_SESSION_ID is configured.")
        )
    else:
        checks.append(
            check_result(
                "session_id_present",
                "warn",
                "GATEWAY_SESSION_ID is missing; session tools will require an explicit session_id.",
            )
        )

    # gateway_health
    try:
        data = client.health()
        checks.append(
            check_result("gateway_health", "pass", "Gateway health endpoint is reachable.", data)
        )
    except GatewayClientError as exc:
        checks.append(check_result("gateway_health", "fail", str(exc)))

    # session_health
    if client.session_id:
        try:
            data = client.session_health()
            connected = bool(data.get("connected"))
            checks.append(
                check_result(
                    "session_health",
                    "pass" if connected else "warn",
                    "Configured session is connected."
                    if connected
                    else "Configured session is not connected.",
                    data,
                )
            )
        except GatewayClientError as exc:
            checks.append(check_result("session_health", "warn", str(exc)))
    else:
        checks.append(
            check_result(
                "session_health",
                "warn",
                "Skipped because GATEWAY_SESSION_ID is missing.",
            )
        )

    # command_policy_allows_safe
    try:
        validate_readonly_command("git status --short")
        validate_readonly_command("pwd")
        checks.append(
            check_result(
                "command_policy_allows_safe",
                "pass",
                "Safe read-only commands are allowed.",
            )
        )
    except CommandPolicyError as exc:
        checks.append(check_result("command_policy_allows_safe", "fail", str(exc)))

    # command_policy_denies_destructive
    denied_ok = True
    denied_details: list[str] = []
    for command in ("rm -rf /", "git push origin master", "curl http://example.com"):
        try:
            validate_readonly_command(command)
        except CommandPolicyError:
            denied_details.append(command)
        else:
            denied_ok = False
            denied_details.append(f"Unexpectedly allowed: {command}")

    checks.append(
        check_result(
            "command_policy_denies_destructive",
            "pass" if denied_ok else "fail",
            "Destructive commands are denied."
            if denied_ok
            else "At least one destructive command was allowed.",
            {"commands": denied_details},
        )
    )

    # repo_status_optional
    if client.session_id:
        try:
            data = client.repo_status()
            checks.append(
                check_result(
                    "repo_status_optional",
                    "pass",
                    "Repository status collected.",
                    data,
                )
            )
        except GatewayClientError as exc:
            checks.append(
                check_result(
                    "repo_status_optional",
                    "warn",
                    f"Repository status was not collected. This is acceptable if "
                    f"the session working directory is not a git repository: {exc}",
                )
            )
    else:
        checks.append(
            check_result(
                "repo_status_optional",
                "warn",
                "Skipped because GATEWAY_SESSION_ID is missing.",
            )
        )

    status = overall_status(checks)
    return {
        "status": status,
        "checks": checks,
        "summary": {
            "pass": sum(1 for c in checks if c["status"] == "pass"),
            "warn": sum(1 for c in checks if c["status"] == "warn"),
            "fail": sum(1 for c in checks if c["status"] == "fail"),
        },
    }
