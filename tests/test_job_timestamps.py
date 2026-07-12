"""Tests for monotonic timestamp fields on JobRecord."""

from app.job_manager import JobRecord


class TestJobRecordMonoTimestamps:
    """Verify mono timestamp fields exist and to_dict includes them."""

    def test_new_fields_exist_with_none_defaults(self):
        job = JobRecord(job_id="j1", session_id="s1", command="echo hi")
        assert job.queued_at_mono is None
        assert job.acquired_at_mono is None
        assert job.command_started_at_mono is None
        assert job.command_finished_at_mono is None
        assert job.completed_at_mono is None
        assert job.ssh_connect_started_at_mono is None
        assert job.ssh_connected_at_mono is None

    def test_to_dict_includes_mono_timestamps(self):
        job = JobRecord(job_id="j1", session_id="s1", command="echo hi")
        d = job.to_dict()
        assert "queued_at_mono" in d
        assert "completed_at_mono" in d
        assert d["queued_at_mono"] is None

    def test_to_dict_includes_completed_at_wall_clock(self):
        job = JobRecord(job_id="j1", session_id="s1", command="echo hi")
        d = job.to_dict()
        assert "completed_at" in d
