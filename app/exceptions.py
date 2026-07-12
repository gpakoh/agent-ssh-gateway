"""Application-level exceptions for job operations."""


class JobNotFoundError(Exception):
    """Raised when a job ID is not found in the job store."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Job {job_id} not found")


class PermissionDeniedError(Exception):
    """Raised when the caller does not own the requested resource."""
