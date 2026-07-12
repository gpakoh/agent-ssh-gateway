import threading
import time
from collections import defaultdict


class LatencyTracker:
    def __init__(self):
        self._records = defaultdict(list)
        self._lock = threading.Lock()

    def measure(self, tool_name: str):
        return _MeasureContext(self, tool_name)

    def record(self, tool_name: str, duration_ms: float):
        with self._lock:
            self._records[tool_name].append(duration_ms)

    @property
    def records(self) -> dict[str, list[float]]:
        with self._lock:
            return dict(self._records)

    def summary(self) -> dict:
        with self._lock:
            by_tool = {}
            for name, durations in self._records.items():
                by_tool[name] = {
                    "count": len(durations),
                    "min_ms": min(durations),
                    "max_ms": max(durations),
                    "avg_ms": sum(durations) / len(durations),
                }
            return {
                "total_calls": sum(len(v) for v in self._records.values()),
                "by_tool": by_tool,
            }


class _MeasureContext:
    def __init__(self, tracker: LatencyTracker, tool_name: str):
        self.tracker = tracker
        self.tool_name = tool_name
        self.start = 0.0

    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *args):
        duration = (time.monotonic() - self.start) * 1000
        self.tracker.record(self.tool_name, duration)


_tracker = LatencyTracker()


def get_tracker() -> LatencyTracker:
    return _tracker
