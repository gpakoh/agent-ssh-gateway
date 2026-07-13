"""Tests for custom exception types used in P0 Core."""

from app.exceptions import JobNotFoundError, PermissionDeniedError


class TestJobNotFoundError:
    def test_message_contains_job_id(self):
        exc = JobNotFoundError("job-abc-123")
        assert "job-abc-123" in str(exc)

    def test_is_exception(self):
        assert issubclass(JobNotFoundError, Exception)


class TestPermissionDeniedError:
    def test_message(self):
        exc = PermissionDeniedError("Job belongs to a different owner")
        assert "different owner" in str(exc)

    def test_is_exception(self):
        assert issubclass(PermissionDeniedError, Exception)
