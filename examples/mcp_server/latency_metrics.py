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
            by_category: dict[str, dict] = {}
            for name, durations in self._records.items():
                cat = name.split("_", 1)[0] if "_" in name else name
                if cat not in by_category:
                    by_category[cat] = {"count": 0, "total_ms": 0.0}
                by_category[cat]["count"] += len(durations)
                by_category[cat]["total_ms"] += sum(durations)
            for cat_data in by_category.values():
                if cat_data["count"] > 0:
                    cat_data["avg_ms"] = round(
                        cat_data["total_ms"] / cat_data["count"], 1
                    )
                else:
                    cat_data["avg_ms"] = 0.0
                cat_data["total_ms"] = round(cat_data["total_ms"], 1)

            return {
                "total_calls": sum(len(v) for v in self._records.values()),
                "by_tool": by_tool,
                "by_category": by_category,
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
