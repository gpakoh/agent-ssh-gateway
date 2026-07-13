"""Tests for execute_argv models and auth scope."""

from app.auth_middleware import VALID_AGENT_SCOPES
from app.models import ExecuteArgvRequest, ExecuteArgvResponse


def test_argv_scope_exists():
    assert "ssh:execute:argv" in VALID_AGENT_SCOPES


def test_execute_argv_request_valid():
    req = ExecuteArgvRequest(
        session_id="abc-123",
        argv=["python3", "-c", "print('hello')"],
    )
    assert req.session_id == "abc-123"
    assert req.argv == ["python3", "-c", "print('hello')"]
    assert req.stdin == ""
    assert req.timeout_s == 30


def test_execute_argv_request_minimal():
    req = ExecuteArgvRequest(session_id="x", argv=["ls"])
    assert req.stdin == ""
    assert req.timeout_s == 30


def test_execute_argv_request_empty_argv_rejected():
    from pydantic import ValidationError

    try:
        ExecuteArgvRequest(session_id="x", argv=[])
        raise AssertionError("Should have raised ValidationError")
    except ValidationError:
        pass


def test_execute_argv_request_arg_too_long_rejected():
    from pydantic import ValidationError

    try:
        ExecuteArgvRequest(session_id="x", argv=["x" * 256])
        raise AssertionError("Should have raised ValidationError")
    except ValidationError:
        pass


def test_execute_argv_request_nul_in_arg_rejected():
    from pydantic import ValidationError

    try:
        ExecuteArgvRequest(session_id="x", argv=["hello\x00world"])
        raise AssertionError("Should have raised ValidationError")
    except ValidationError:
        pass


def test_execute_argv_request_timeout_bounds():
    from pydantic import ValidationError

    try:
        ExecuteArgvRequest(session_id="x", argv=["ls"], timeout_s=0)
        raise AssertionError("Should have raised ValidationError")
    except ValidationError:
        pass

    try:
        ExecuteArgvRequest(session_id="x", argv=["ls"], timeout_s=3601)
        raise AssertionError("Should have raised ValidationError")
    except ValidationError:
        pass


def test_execute_argv_request_total_argv_length():
    from pydantic import ValidationError

    # Total UTF-8 length of all args <= 65536
    try:
        ExecuteArgvRequest(
            session_id="x",
            argv=["a" * 30000, "b" * 30000, "c" * 6000],
        )
        raise AssertionError("Should have raised ValidationError")
    except ValidationError:
        pass


def test_execute_argv_request_stdin_limit():
    from pydantic import ValidationError

    try:
        ExecuteArgvRequest(
            session_id="x", argv=["ls"], stdin="x" * (1024 * 1024 + 1)
        )
        raise AssertionError("Should have raised ValidationError")
    except ValidationError:
        pass


def test_execute_argv_response():
    resp = ExecuteArgvResponse(
        stdout="hello\n",
        stderr="",
        exit_code=0,
        duration=0.123,
    )
    assert resp.stdout == "hello\n"
    assert resp.exit_code == 0
    assert resp.duration == 0.123
