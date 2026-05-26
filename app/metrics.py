"""Prometheus metrics for monitoring."""

import logging
import time
from typing import Optional

from prometheus_client import Counter, Histogram, Gauge, Info, generate_latest

logger = logging.getLogger(__name__)


class MetricsCollector:
    """Prometheus metrics collector.
    
    Tracks:
    - Request rates and latencies
    - SSH connection stats
    - Job queue stats
    - Error rates
    """
    
    def __init__(self):
        # Request Metrics
        self.request_count = Counter(
            'ssh_gateway_requests_total',
            'Total requests',
            ['method', 'endpoint', 'status']
        )
        
        self.request_duration = Histogram(
            'ssh_gateway_request_duration_seconds',
            'Request duration',
            ['method', 'endpoint'],
            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0]
        )
        
        # SSH Metrics
        self.ssh_connections = Gauge(
            'ssh_gateway_ssh_connections_active',
            'Active SSH connections'
        )
        
        self.ssh_connection_duration = Histogram(
            'ssh_gateway_ssh_connection_duration_seconds',
            'SSH connection duration',
            buckets=[60, 300, 600, 1800, 3600, 7200, 86400]
        )
        
        self.ssh_commands = Counter(
            'ssh_gateway_ssh_commands_total',
            'Total SSH commands executed',
            ['status']
        )
        
        # Job Metrics
        self.jobs_enqueued = Counter(
            'ssh_gateway_jobs_enqueued_total',
            'Total jobs enqueued',
            ['priority']
        )
        
        self.jobs_completed = Counter(
            'ssh_gateway_jobs_completed_total',
            'Total jobs completed',
            ['status']
        )
        
        self.jobs_duration = Histogram(
            'ssh_gateway_jobs_duration_seconds',
            'Job execution duration',
            buckets=[1, 5, 10, 30, 60, 300, 600, 1800]
        )
        
        self.queue_depth = Gauge(
            'ssh_gateway_queue_depth',
            'Current queue depth',
            ['queue']
        )
        
        # Circuit Breaker Metrics
        self.circuit_breaker_state = Gauge(
            'ssh_gateway_circuit_breaker_state',
            'Circuit breaker state (0=closed, 1=half-open, 2=open)',
            ['host']
        )
        
        # Lock Metrics
        self.locks_active = Gauge(
            'ssh_gateway_locks_active',
            'Active distributed locks'
        )
        
        # File Metrics
        self.file_operations = Counter(
            'ssh_gateway_file_operations_total',
            'Total file operations',
            ['operation', 'status']
        )
        
        # System Info
        self.info = Info('ssh_gateway', 'SSH Gateway information')
        self.info.info({'version': '4.5.1'})
    
    def record_request(self, method: str, endpoint: str, status: int, duration: float):
        """Record HTTP request metrics."""
        self.request_count.labels(method=method, endpoint=endpoint, status=str(status)).inc()
        self.request_duration.labels(method=method, endpoint=endpoint).observe(duration)
    
    def record_ssh_command(self, status: str = "success"):
        """Record SSH command execution."""
        self.ssh_commands.labels(status=status).inc()
    
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
    
    def update_circuit_breaker(self, host: str, state: str):
        """Update circuit breaker state metric."""
        state_value = {"closed": 0, "half_open": 1, "open": 2}.get(state, 0)
        self.circuit_breaker_state.labels(host=host).set(state_value)
    
    def update_active_locks(self, count: int):
        """Update active locks metric."""
        self.locks_active.set(count)
    
    def record_file_operation(self, operation: str, status: str = "success"):
        """Record file operation."""
        self.file_operations.labels(operation=operation, status=status).inc()
    
    def get_metrics(self) -> bytes:
        """Get Prometheus metrics in text format."""
        return generate_latest()


# Global Metrics Instance
metrics = MetricsCollector()
