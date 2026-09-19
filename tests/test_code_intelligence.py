from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import aiohttp
import pytest

from app.code_intelligence import CodeIntelligence


@dataclass
class _FakeResponse:
    status: int = 200
    payload: dict[str, Any] | None = None
    text_value: str = ""
    json_error: Exception | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload or {}

    async def text(self):
        return self.text_value


class _RaisingContext:
    def __init__(self, exc: BaseException):
        self.exc = exc

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, response_context):
        self.response_context = response_context
        self.post_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self.response_context


def _make_ci() -> CodeIntelligence:
    return CodeIntelligence(ssh_manager=None, file_editor=None)


def _install_session(monkeypatch, response_context):
    session = _FakeSession(response_context)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    return session


@pytest.mark.asyncio
async def test_generate_code_success_posts_once_and_cleans_markdown(monkeypatch):
    monkeypatch.setenv("OPENCODE_ADAPTER_URL", "http://adapter:8007")
    generated = "def generated():\n    return '" + ("x" * 80) + "'"
    session = _install_session(
        monkeypatch,
        _FakeResponse(payload={"response": f"```python\n{generated}\n```"}),
    )

    result = await _make_ci().generate_code("s1", "create a function")

    assert result == generated
    assert len(session.post_calls) == 1
    args, kwargs = session.post_calls[0]
    assert args == ("http://adapter:8007/api/generate",)
    assert kwargs["json"]["model"] == "openrouter/auto"
    assert kwargs["json"]["stream"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_context",
    [
        _FakeResponse(payload={"response": "Error: provider unavailable"}),
        _FakeResponse(payload={"response": "short"}),
        _FakeResponse(status=503, text_value="unavailable"),
        _RaisingContext(TimeoutError("ambiguous timeout")),
        _RaisingContext(aiohttp.ClientConnectionError("ambiguous reset")),
    ],
    ids=["adapter-error", "short-response", "non-200", "timeout", "transport"],
)
async def test_generate_code_failure_never_replays_and_falls_back(
    monkeypatch, response_context
):
    monkeypatch.setenv("OPENCODE_ADAPTER_URL", "http://adapter:8007")
    session = _install_session(monkeypatch, response_context)
    ci = _make_ci()
    expected = ci._generate_fallback("create a function", "python")

    result = await ci.generate_code("s1", "create a function")

    assert result == expected
    assert len(session.post_calls) == 1


@pytest.mark.asyncio
async def test_generate_code_malformed_json_after_post_never_replays(monkeypatch):
    monkeypatch.setenv("OPENCODE_ADAPTER_URL", "http://adapter:8007")
    session = _install_session(
        monkeypatch,
        _FakeResponse(json_error=ValueError("invalid response JSON")),
    )
    ci = _make_ci()
    expected = ci._generate_fallback("create a function", "python")

    result = await ci.generate_code("s1", "create a function")

    assert result == expected
    assert len(session.post_calls) == 1


@pytest.mark.asyncio
async def test_generate_code_cancellation_propagates_without_replay(monkeypatch):
    monkeypatch.setenv("OPENCODE_ADAPTER_URL", "http://adapter:8007")
    session = _install_session(monkeypatch, _RaisingContext(asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await _make_ci().generate_code("s1", "create a function")

    assert len(session.post_calls) == 1
