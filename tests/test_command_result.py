from examples.mcp_server.tool_results import build_command_result, tool_success


def test_command_result_shape():
    data = build_command_result(
        outcome="passed",
        exit_code=0,
        stdout="ok",
        stderr="",
        execution_duration_ms=842,
        job_id=None,
        timestamps={"created": "2026-07-12T12:00:00Z", "started": None, "finished": None},
    )
    result = tool_success("test_tool", data)
    r = result["result"]
    assert r["outcome"] == "passed"
    assert r["exit_code"] == 0
    assert r["stdout"] == "ok"
    assert r["execution_duration_ms"] == 842
    assert r["job_id"] is None


def test_command_result_failed_outcome():
    data = build_command_result(
        outcome="failed",
        exit_code=1,
        stdout="",
        stderr="lint error",
        execution_duration_ms=50,
    )
    r = tool_success("test_tool", data)["result"]
    assert r["outcome"] == "failed"
    assert r["exit_code"] == 1


def test_command_result_completed_outcome():
    data = build_command_result(outcome="completed", exit_code=0, stdout="done", stderr="")
    r = tool_success("test_tool", data)["result"]
    assert r["outcome"] == "completed"
