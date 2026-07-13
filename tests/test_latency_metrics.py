import time

from examples.mcp_server.latency_metrics import LatencyTracker


def test_tracker_records_duration():
    tracker = LatencyTracker()
    with tracker.measure("health"):
        time.sleep(0.01)
    assert tracker.records["health"][0] >= 10  # >= 10ms


def test_tracker_multiple_calls():
    tracker = LatencyTracker()
    for _ in range(3):
        with tracker.measure("health"):
            pass
    assert len(tracker.records["health"]) == 3


def test_tracker_summary():
    tracker = LatencyTracker()
    with tracker.measure("test"):
        time.sleep(0.01)
    summary = tracker.summary()
    assert summary["total_calls"] >= 1
    assert "test" in summary["by_tool"]
