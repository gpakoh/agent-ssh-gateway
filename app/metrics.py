"""Prometheus metrics for monitoring."""

import logging

from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest

from app.version import APP_VERSION

logger = logging.getLogger(__name__)

# Bounded allowlist for command_root label — anything else maps to "other".
_COMMAND_ROOT_ALLOWLIST: frozenset[str] = frozenset({
    "git", "docker", "pytest", "ruff", "mypy", "uv", "python", "pip",
    "ls", "cat", "grep", "find", "echo", "pwd", "whoami", "ssh",
    "curl", "wget", "systemctl", "journalctl", "make", "cargo", "node",
    "npm", "pnpm", "yarn", "go", "java", "mvn", "gradle",
})


def _normalize_command_root(raw: str | None) -> str:
    """Normalize command_root to a bounded label value."""
    if not raw:
        return "other"
    root = raw.split("/")[-1].lower()  # handle /usr/bin/git → git
    return root if root in _COMMAND_ROOT_ALLOWLIST else "other"


class MetricsCollector:
    """Prometheus metrics collector.

    Tracks:
    - Request rates and latencies
    - SSH connection stats
    - Job queue stats
    - Error rates
    """

    def __init__(self):
        # Request metrics
        self.request_count = Counter(
            "ssh_gateway_requests_total", "Total requests", ["method", "endpoint", "status"]
        )

        self.request_duration = Histogram(
            "ssh_gateway_request_duration_seconds",
            "Request duration",
            ["method", "endpoint"],
            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
        )

        # SSH metrics
        self.ssh_connections = Gauge("ssh_gateway_ssh_connections_active", "Active SSH connections")

        self.ssh_connection_duration = Histogram(
            "ssh_gateway_ssh_connection_duration_seconds",
            "SSH connection duration",
            buckets=[60, 300, 600, 1800, 3600, 7200, 86400],
        )

        self.ssh_commands = Counter(
            "ssh_gateway_ssh_commands_total",
            "Total SSH commands executed",
            ["status", "profile", "command_root"],
        )

        # Job metrics
        self.jobs_enqueued = Counter(
            "ssh_gateway_jobs_enqueued_total", "Total jobs enqueued", ["priority"]
        )

        self.jobs_completed = Counter(
            "ssh_gateway_jobs_completed_total", "Total jobs completed", ["status"]
        )

        self.jobs_duration = Histogram(
            "ssh_gateway_jobs_duration_seconds",
            "Job execution duration",
            buckets=[1, 5, 10, 30, 60, 300, 600, 1800],
        )

        self.queue_depth = Gauge("ssh_gateway_queue_depth", "Current queue depth", ["queue"])

        # Circuit breaker metrics — aggregate count by state, not per-host.
        # Per-host labels would be unbounded cardinality (arbitrary SSH
        # targets); per-host detail is available via /api/circuit-breaker/stats.
        self.circuit_breaker_count = Gauge(
            "ssh_gateway_circuit_breakers_count",
            "Number of circuit breakers currently in each state",
            ["state"],
        )

        # Lock metrics
        self.locks_active = Gauge("ssh_gateway_locks_active", "Active distributed locks")

        # File metrics
        self.file_operations = Counter(
            "ssh_gateway_file_operations_total", "Total file operations", ["operation", "status"]
        )

        # Event hook metrics
        self.event_hook_deliveries_total = Counter(
            "ssh_gateway_event_hook_deliveries_total",
            "Event hook deliveries",
            ["status", "event"],
        )
        self.event_hook_delivery_attempts_total = Counter(
            "ssh_gateway_event_hook_delivery_attempts_total",
            "Event hook delivery attempts",
        )
        self.event_hook_delivery_latency = Histogram(
            "ssh_gateway_event_hook_delivery_latency_seconds",
            "Event hook delivery latency",
            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
        )
        self.event_hook_dead_letter = Gauge(
            "ssh_gateway_event_hook_dead_letter_count",
            "Dead letter deliveries",
        )

        # System info
        self.info = Info("ssh_gateway", "SSH Gateway information")
        self.info.info({"version": APP_VERSION})

    def record_request(self, method: str, endpoint: str, status: int, duration: float):
        """Record HTTP request metrics."""
        self.request_count.labels(method=method, endpoint=endpoint, status=str(status)).inc()
        self.request_duration.labels(method=method, endpoint=endpoint).observe(duration)

    def record_ssh_command(
        self,
        status: str = "allowed",
        profile: str = "default",
        command_root: str | None = None,
    ):
        """Record SSH command execution.

        Args:
            status: One of 'allowed', 'denied', 'error'.
            profile: Command policy profile name (bounded set).
            command_root: Normalized root command (bounded allowlist, else 'other').
        """
        root = _normalize_command_root(command_root)
        self.ssh_commands.labels(status=status, profile=profile, command_root=root).inc()

    def record_job_enqueued(self, priority: int = 0):
        """Record job enqueue."""
        self.jobs_enqueued.labels(priority=str(priority)).inc()

    def record_job_completed(self, duration: float, status: str = "success"):
        """Record job completion."""
        self.jobs_completed.labels(status=status).inc()
        self.jobs_duration.observe(duration)

    def update_queue_depth(self, pending: int, processing: int, dead: int):
        """Update queue depth metrics."""
        self.queue_depth.labels(queue="pending").set(pending)
        self.queue_depth.labels(queue="processing").set(processing)
        self.queue_depth.labels(queue="dead").set(dead)

    def set_circuit_breaker_counts(self, counts: dict[str, int]) -> None:
        """Set circuit breaker gauges from a state -> count mapping.

        Overwrites all three known states each call so a state that drops to
        zero breakers is reflected (not left stale from a prior scrape).
        """
        for state in ("closed", "half_open", "open"):
            self.circuit_breaker_count.labels(state=state).set(counts.get(state, 0))

    def update_active_locks(self, count: int):
        """Update active locks metric."""
        self.locks_active.set(count)

    def record_file_operation(self, operation: str, status: str = "success"):
        """Record file operation."""
        self.file_operations.labels(operation=operation, status=status).inc()

    def record_event_hook_delivery(self, status: str = "success", event: str = "unknown"):
        """Record event hook delivery."""
        self.event_hook_deliveries_total.labels(status=status, event=event).inc()

    def record_event_hook_attempt(self):
        """Record a delivery attempt."""
        self.event_hook_delivery_attempts_total.inc()

    def record_event_hook_latency(self, seconds: float):
        """Record delivery latency."""
        self.event_hook_delivery_latency.observe(seconds)

    def set_event_hook_dead_letter_count(self, count: int):
        """Set dead letter delivery count."""
        self.event_hook_dead_letter.set(count)

    def get_metrics(self) -> bytes:
        """Get Prometheus metrics in text format."""
        return generate_latest()


# Global metrics instance
metrics = MetricsCollector()
