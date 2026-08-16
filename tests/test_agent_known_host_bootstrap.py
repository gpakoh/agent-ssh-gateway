from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ensure-agent-known-host.py"


def _load_main() -> tuple[Any, dict[str, Any]]:
    namespace = runpy.run_path(str(SCRIPT))
    return namespace["main"], namespace


def test_known_agent_host_is_idempotent(monkeypatch) -> None:
    main, namespace = _load_main()
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def fake_request(
        method: str, path: str, *, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        calls.append((method, path, body))
        return {"status": "known"}

    main.__globals__["_request"] = fake_request
    monkeypatch.setenv("AGENT_SSH_HOST", "agent-sshd")
    monkeypatch.setenv("AGENT_SSH_PORT", "2222")

    assert main() == 0
    assert [method for method, _, _ in calls] == ["GET"]


def test_unknown_agent_host_is_added_then_rechecked(monkeypatch) -> None:
    main, namespace = _load_main()
    calls: list[tuple[str, str, dict[str, object] | None]] = []
    checks = iter(("unknown", "known"))

    def fake_request(
        method: str, path: str, *, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        calls.append((method, path, body))
        if method == "GET":
            return {"status": next(checks)}
        assert body == {"host": "agent-sshd", "port": 2222}
        return {"status": "added"}

    main.__globals__["_request"] = fake_request
    monkeypatch.setenv("AGENT_SSH_HOST", "agent-sshd")
    monkeypatch.setenv("AGENT_SSH_PORT", "2222")

    assert main() == 0
    assert [method for method, _, _ in calls] == ["GET", "POST", "GET"]
