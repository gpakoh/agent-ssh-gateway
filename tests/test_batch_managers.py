"""Contract tests for batch/bulk managers (P19.1).

Verifies that both BatchOperationsManager (transactional file batch,
routers/batch.py) and BulkOperationsManager (concurrent executor,
routers/jobs.py's execute_batch_commands + routers/files.py's
read_files_bulk) keep their public contracts, so the two processing
paths cannot silently diverge.
"""

import pytest

from app.batch_operations import BatchOperationsManager
from app.bulk_operations_v2 import BulkOperationsManager


class FakeSSH:
    async def execute(self, session_id, command, timeout=30):
        return {"exit_code": 0, "stdout": f"ok:{command}", "stderr": ""}


class FakeFileEditor:
    async def read_file(self, session_id, path):
        return f"content of {path}"

    async def edit_file(self, session_id, path, operations):
        return {"success": True, "operations_applied": 1, "changed": True}


class FakeContext:
    def __init__(self):
        self.path = "/project"
        self.edits = []

    async def get_context(self, context_id):
        return self

    async def record_edit(self, context_id, path, source):
        self.edits.append(path)

    async def commit_changes(self, context_id, message, files):
        return {"success": True, "hash": "abc123"}

    async def validate_context(self, context_id):
        class Report:
            overall_status = "pass"
            summary = "ok"
            can_commit = True
        return Report()


@pytest.fixture
def bulk():
    return BulkOperationsManager(max_concurrency=4)


@pytest.fixture
def batch():
    return BatchOperationsManager(FakeSSH(), FakeFileEditor(), FakeContext())


class TestBulkOperationsManager:
    async def test_execute_bulk_returns_results_with_success(self, bulk):
        async def executor(item):
            return item * 2

        results = await bulk.execute_bulk([1, 2, 3], executor, max_concurrency=2)
        assert [r["success"] for r in results] == [True, True, True]
        assert [r["result"] for r in results] == [2, 4, 6]

    async def test_execute_bulk_catches_exceptions(self, bulk):
        async def executor(item):
            if item == 2:
                raise RuntimeError("boom")
            return item

        results = await bulk.execute_bulk([1, 2, 3], executor)
        assert results[0]["success"] is True
        assert results[1]["success"] is False
        assert "boom" in results[1]["error"]
        assert results[2]["success"] is True

    async def test_execute_batch_commands(self, bulk):
        results = await bulk.execute_batch_commands(
            "sess", ["echo a", "echo b"], FakeSSH(), max_concurrency=2
        )
        assert len(results) == 2
        assert all(r["success"] for r in results)
        assert "echo a" in results[0]["result"]["stdout"]

    async def test_read_files_bulk(self, bulk):
        files = await bulk.read_files_bulk(
            "sess", ["a.txt", "b.txt"], FakeFileEditor(), max_concurrency=2
        )
        assert files == {"a.txt": "content of a.txt", "b.txt": "content of b.txt"}

    async def test_read_files_bulk_skips_missing(self, bulk):
        class BrokenEditor:
            async def read_file(self, session_id, path):
                raise FileNotFoundError(path)

        files = await bulk.read_files_bulk("sess", ["x.txt"], BrokenEditor())
        assert files == {}


class TestBatchOperationsManager:
    async def test_execute_batch_read(self, batch):
        result = await batch.execute_batch(
            "sess", "ctx", [{"type": "read", "path": "a.txt"}]
        )
        assert result.overall_success is True
        assert result.operations[0].operation == "read"
        assert "content of" in result.operations[0].output

    async def test_execute_batch_edit_records_change(self, batch):
        result = await batch.execute_batch(
            "sess", "ctx", [{"type": "edit", "path": "b.txt", "operations": []}]
        )
        assert result.overall_success is True
        assert batch._context.edits == ["/project/b.txt"]

    async def test_execute_batch_unknown_type_fails(self, batch):
        result = await batch.execute_batch("sess", "ctx", [{"type": "nope", "path": "x"}])
        assert result.overall_success is False
        assert "Unknown operation type" in result.operations[0].error

    async def test_execute_batch_stops_on_error(self, batch):
        result = await batch.execute_batch(
            "sess",
            "ctx",
            [
                {"type": "nope", "path": "x"},
                {"type": "read", "path": "a.txt"},
            ],
        )
        assert result.overall_success is False
        assert len(result.operations) == 1

    async def test_execute_batch_auto_commit(self, batch):
        result = await batch.execute_batch(
            "sess",
            "ctx",
            [{"type": "edit", "path": "b.txt", "operations": []}],
            auto_commit=True,
            commit_message="batch",
        )
        assert result.git_commit == "abc123"
