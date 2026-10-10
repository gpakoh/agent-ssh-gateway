"""MCP adapter tests for admin-only supervisor integration tools."""

from __future__ import annotations

import errno
import hashlib
import inspect
from pathlib import Path

import pytest

from examples.mcp_server.mcp_infra.adapters import remote, supervisor
from examples.mcp_server.supervisor_integration import RecoveryResult


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _Registry:
    def __init__(self, roots: dict[str, Path]):
        self.roots = roots

    def project_info(self, project: str):
        root = self.roots.get(project)
        if root is None:
            raise ValueError("unknown project")
        return {"project_id": project, "root": str(root)}


@pytest.fixture
def immediate_run_tool(monkeypatch):
    def _run_tool(*, tool, title, fn, success_text):
        del tool, title, success_text
        return fn()

    monkeypatch.setattr(supervisor, "run_tool", _run_tool)


@pytest.fixture
def immediate_run_tool_async(monkeypatch):
    async def _run_tool_async(*, tool, title, fn, success_text):
        del tool, title, success_text
        return await fn()

    monkeypatch.setattr(supervisor, "run_tool_async", _run_tool_async)


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(supervisor, "_get_workspace_registry", lambda: _Registry({"demo": root}))
    journal_base = tmp_path / "journals"
    monkeypatch.setenv("MCP_SUPERVISOR_JOURNAL_ROOT", str(journal_base))
    return root, journal_base


def test_public_signatures_do_not_expose_roots():
    integrate = inspect.signature(supervisor.supervisor_integrate_file)
    assert list(integrate.parameters) == [
        "project",
        "relative_path",
        "expected_sha256",
        "new_content",
    ]
    recover = inspect.signature(supervisor.supervisor_recover_integrations)
    assert list(recover.parameters) == ["project"]
    assert "journal_root" not in integrate.parameters
    assert "project_root" not in integrate.parameters


def test_journal_namespace_is_server_controlled_and_project_specific(tmp_path, monkeypatch):
    base = tmp_path / "journal-base"
    monkeypatch.setenv("MCP_SUPERVISOR_JOURNAL_ROOT", str(base))
    p1 = tmp_path / "one"
    p2 = tmp_path / "two"
    p1.mkdir()
    p2.mkdir()

    j1 = supervisor._journal_root_for_project("one", p1)
    j2 = supervisor._journal_root_for_project("two", p2)

    assert j1.parent == base.resolve()
    assert j2.parent == base.resolve()
    assert j1 != j2
    assert len(j1.name) == 64
    int(j1.name, 16)
    assert p1.resolve() not in j1.parents
    assert p2.resolve() not in j2.parents


def test_relative_journal_configuration_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SUPERVISOR_JOURNAL_ROOT", "relative/journals")
    project_root = tmp_path / "project"
    project_root.mkdir()
    with pytest.raises(ValueError, match="absolute"):
        supervisor._journal_root_for_project("demo", project_root)


def test_journal_configuration_inside_checkout_fails_closed(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv(
        "MCP_SUPERVISOR_JOURNAL_ROOT",
        str(project_root / ".private-journals"),
    )
    with pytest.raises(ValueError, match="outside"):
        supervisor._journal_root_for_project("demo", project_root)


def test_integrate_changes_existing_file_without_leaking_host_path(
    project, immediate_run_tool
):
    root, journal_base = project
    target = root / "config.txt"
    original = b"old\n"
    target.write_bytes(original)

    result = supervisor.supervisor_integrate_file(
        "demo",
        "config.txt",
        _sha(original),
        "new\n",
    )

    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "new\n"
    assert result["result"] == {
        "project": "demo",
        "path": "config.txt",
        "original_hash": _sha(original),
        "new_hash": _sha(b"new\n"),
    }
    assert str(root) not in repr(result)
    assert str(journal_base) not in repr(result)


def test_hash_mismatch_is_canonical_and_path_safe(project, immediate_run_tool):
    root, journal_base = project
    target = root / "config.txt"
    target.write_text("old\n", encoding="utf-8")

    result = supervisor.supervisor_integrate_file(
        "demo",
        "config.txt",
        _sha(b"different"),
        "new\n",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "CHECK_FAILED"
    assert result["error"]["retryable"] is False
    assert str(root) not in repr(result)
    assert str(journal_base) not in repr(result)
    assert target.read_text(encoding="utf-8") == "old\n"


def test_unknown_project_fails_closed(monkeypatch, immediate_run_tool):
    monkeypatch.setattr(supervisor, "_get_workspace_registry", lambda: _Registry({}))

    result = supervisor.supervisor_integrate_file(
        "missing",
        "config.txt",
        _sha(b"old"),
        "new",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "PROJECT_NOT_FOUND"
    assert result["error"]["retryable"] is False


def test_integrate_readonly_filesystem_is_canonical_and_path_safe(
    project, immediate_run_tool, monkeypatch
):
    root, journal_base = project

    def _raise(*_args, **_kwargs):
        raise OSError(errno.EROFS, "Read-only file system", str(root / "config.txt"))

    monkeypatch.setattr(supervisor, "integrate_file", _raise)

    result = supervisor.supervisor_integrate_file(
        "demo",
        "config.txt",
        _sha(b"old"),
        "new",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "WORKSPACE_READONLY"
    assert result["error"]["retryable"] is False
    assert result["error"]["hint"]
    assert str(root) not in repr(result)
    assert str(journal_base) not in repr(result)


def test_integrate_permission_error_is_canonical_and_path_safe(
    project, immediate_run_tool, monkeypatch
):
    root, journal_base = project

    def _raise(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(root / "config.txt"))

    monkeypatch.setattr(supervisor, "integrate_file", _raise)

    result = supervisor.supervisor_integrate_file(
        "demo",
        "config.txt",
        _sha(b"old"),
        "new",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert result["error"]["retryable"] is False
    assert result["error"]["hint"]
    assert str(root) not in repr(result)
    assert str(journal_base) not in repr(result)


def test_non_text_content_is_rejected(project, immediate_run_tool):
    result = supervisor.supervisor_integrate_file(
        "demo",
        "config.txt",
        _sha(b"old"),
        b"bytes are not MCP text",  # type: ignore[arg-type]
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_INPUT"


def test_recovery_serializes_without_raw_error_or_paths(
    project, immediate_run_tool, monkeypatch
):
    root, journal_base = project
    monkeypatch.setattr(
        supervisor,
        "recover_pending",
        lambda project_root, journal_root: [
            RecoveryResult(
                relative_path="a.txt",
                status="restored",
                journal_retained=False,
            ),
            RecoveryResult(
                relative_path="b.txt",
                status="error",
                journal_retained=True,
                error=f"unsafe target {root}/b.txt journal {journal_root}",
            ),
        ],
    )

    result = supervisor.supervisor_recover_integrations("demo")

    assert result["ok"] is True
    assert result["result"]["project"] == "demo"
    assert result["result"]["count"] == 2
    assert result["result"]["recoveries"] == [
        {
            "path": "a.txt",
            "status": "restored",
            "journal_retained": False,
        },
        {
            "path": "b.txt",
            "status": "error",
            "journal_retained": True,
            "error": "Manual intervention is required for this recovery entry.",
        },
    ]
    assert str(root) not in repr(result)
    assert str(journal_base) not in repr(result)


def test_recovery_unknown_project_fails_closed(monkeypatch, immediate_run_tool):
    monkeypatch.setattr(supervisor, "_get_workspace_registry", lambda: _Registry({}))
    result = supervisor.supervisor_recover_integrations("missing")
    assert result["ok"] is False
    assert result["error"]["code"] == "PROJECT_NOT_FOUND"


def test_prepare_candidate_clone_preserves_typed_remote_state_error(
    tmp_path, immediate_run_tool, monkeypatch
):
    config_dir = tmp_path / "registry"
    config_dir.mkdir()
    journal_root = tmp_path / "journals"
    monkeypatch.setattr(supervisor, "_resolve_registry_config_dir", lambda: config_dir)
    monkeypatch.setattr(
        supervisor,
        "_journal_root_for_project",
        lambda _project, _root: journal_root,
    )

    def _raise(*_args, **_kwargs):
        raise supervisor.CandidateCloneError(
            "SOURCE_REMOTE_STATE_UNKNOWN",
            "trusted remote state is unknown; refusing local base-ref fallback",
            retryable=True,
            details={
                "base_ref": "main",
                "remote_status": "unknown",
                "recovery_action": "refresh_or_restore_trusted_remote_access",
            },
        )

    monkeypatch.setattr(supervisor, "_prepare_candidate_clone", _raise)

    result = supervisor.prepare_candidate_clone("demo", "candidate/demo", "main")

    assert result["ok"] is False
    assert result["error"]["code"] == "SOURCE_REMOTE_STATE_UNKNOWN"
    assert result["error"]["retryable"] is True
    assert result["error"]["details"] == {
        "base_ref": "main",
        "remote_status": "unknown",
        "recovery_action": "refresh_or_restore_trusted_remote_access",
    }


class _CleanupReceipt:
    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": "candidate-demo",
            "removed": True,
            "already_absent": False,
        }


class _AuditLogger:
    def __init__(self, *, fail_required: bool = False) -> None:
        self.fail_required = fail_required
        self.required = []
        self.success = []

    def append_required(self, event) -> None:
        if self.fail_required:
            raise supervisor.AuditWriteError("audit unavailable")
        self.required.append(event)

    def append(self, event) -> None:
        self.success.append(event)


class _PullClient:
    def __init__(self, pages=None, error: Exception | None = None) -> None:
        self.pages = pages or {1: []}
        self.error = error
        self.calls: list[int] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def list_pull_requests(self, _owner, _repo, *, state, limit, page):
        assert state == "open"
        assert limit == 50
        self.calls.append(page)
        if self.error is not None:
            raise self.error
        return self.pages.get(page, [])


def _same_repo_pr(number: int, *, ref: str, sha: str) -> dict[str, object]:
    return {
        "number": number,
        "head": {
            "ref": ref,
            "sha": sha,
            "repo": {"full_name": "gpakoh/agent-ssh-gateway"},
        },
    }


def _foreign_pr(number: int, *, ref: str, sha: str) -> dict[str, object]:
    return {
        "number": number,
        "head": {
            "ref": ref,
            "sha": sha,
            "repo": {"full_name": "other/fork"},
        },
    }


def _configure_cleanup_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    client: _PullClient,
    audit: _AuditLogger,
    guard_calls: list[str],
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    config_dir = tmp_path / "registry"
    config_dir.mkdir()
    monkeypatch.setattr(
        supervisor,
        "_get_workspace_registry",
        lambda: _Registry({"demo": source_root}),
    )
    monkeypatch.setattr(supervisor, "_resolve_registry_config_dir", lambda: config_dir)
    monkeypatch.setattr(
        supervisor,
        "_journal_root_for_project",
        lambda _project, _root: tmp_path / "journals",
    )
    monkeypatch.setattr(
        supervisor,
        "_resolve_trusted_remote",
        lambda _root: (
            "gpakoh",
            "https://git.xloud.ru/gpakoh/agent-ssh-gateway.git",
            "managed-token",
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "_parse_gitea_remote",
        lambda _url: ("git.xloud.ru", "gpakoh", "agent-ssh-gateway"),
    )
    monkeypatch.setattr(remote, "_server_gitea_client", lambda: (lambda _token: client))
    monkeypatch.setattr(
        supervisor,
        "server_attr",
        lambda name: (lambda: audit)
        if name == "get_audit_logger"
        else (_ for _ in ()).throw(AssertionError(name)),
    )
    monkeypatch.setattr(supervisor, "_reset_project_registry_caches", lambda: True)

    def _cleanup(
        project_id,
        expected_head_sha,
        expected_branch,
        expected_source_project,
        preserved_ref,
        *,
        config_dir,
        journal_root,
        reference_guard,
    ):
        del project_id, expected_head_sha, expected_branch, expected_source_project
        del preserved_ref, config_dir, journal_root
        guard_calls.append("first")
        reference_guard()
        guard_calls.append("second")
        reference_guard()
        guard_calls.append("third")
        reference_guard()
        return _CleanupReceipt()

    monkeypatch.setattr(supervisor, "_candidate_cleanup", _cleanup)


async def test_candidate_cleanup_uses_fresh_guard_twice_and_required_audit_once(
    tmp_path, immediate_run_tool_async, monkeypatch
):
    head = "a" * 40
    client = _PullClient(
        {1: [_foreign_pr(7, ref="candidate/demo", sha=head)]}
    )
    audit = _AuditLogger()
    guard_calls: list[str] = []
    _configure_cleanup_adapter(
        tmp_path,
        monkeypatch,
        client=client,
        audit=audit,
        guard_calls=guard_calls,
    )

    result = await supervisor.candidate_cleanup(
        "candidate-demo",
        head,
        "candidate/demo",
        "demo",
        "archive/candidate-demo",
    )

    assert result["ok"] is True
    assert result["result"]["removed"] is True
    assert result["result"]["cache_reset"] is True
    assert guard_calls == ["first", "second", "third"]
    assert client.calls == [1, 1, 1]
    assert len(audit.required) == 1
    assert len(audit.success) == 1
    assert (
        audit.required[0].metadata["correlation_id"]
        == audit.success[0].metadata["correlation_id"]
    )


@pytest.mark.parametrize(
    ("ref", "sha", "reference_type"),
    [
        ("candidate/demo", "b" * 40, "branch"),
        ("different/ref", "a" * 40, "head_sha"),
    ],
)
async def test_candidate_cleanup_blocks_open_same_repo_pr_reference(
    tmp_path,
    immediate_run_tool_async,
    monkeypatch,
    ref,
    sha,
    reference_type,
):
    head = "a" * 40
    client = _PullClient({1: [_same_repo_pr(42, ref=ref, sha=sha)]})
    audit = _AuditLogger()
    guard_calls: list[str] = []
    _configure_cleanup_adapter(
        tmp_path,
        monkeypatch,
        client=client,
        audit=audit,
        guard_calls=guard_calls,
    )

    result = await supervisor.candidate_cleanup(
        "candidate-demo",
        head,
        "candidate/demo",
        "demo",
        "archive/candidate-demo",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "WORKSPACE_CONTENDED"
    assert result["error"]["retryable"] is True
    assert result["error"]["details"] == {
        "reference_check": "gitea_open_pull_requests",
        "pull_number": 42,
        "reference_type": reference_type,
    }
    assert guard_calls == ["first"]
    assert audit.required == []


async def test_candidate_cleanup_scans_later_open_pr_pages_before_mutation(
    tmp_path, immediate_run_tool_async, monkeypatch
):
    head = "a" * 40
    first_page = [
        _foreign_pr(index, ref=f"fork/{index}", sha=f"{index:040x}")
        for index in range(1, 51)
    ]
    client = _PullClient(
        {
            1: first_page,
            2: [_same_repo_pr(77, ref="candidate/demo", sha=head)],
        }
    )
    audit = _AuditLogger()
    guard_calls: list[str] = []
    _configure_cleanup_adapter(
        tmp_path,
        monkeypatch,
        client=client,
        audit=audit,
        guard_calls=guard_calls,
    )

    result = await supervisor.candidate_cleanup(
        "candidate-demo",
        head,
        "candidate/demo",
        "demo",
        "archive/candidate-demo",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "WORKSPACE_CONTENDED"
    assert result["error"]["details"]["pull_number"] == 77
    assert client.calls == [1, 2]
    assert audit.required == []


async def test_candidate_cleanup_fails_closed_on_matching_pr_with_unknown_repo(
    tmp_path, immediate_run_tool_async, monkeypatch
):
    head = "a" * 40
    client = _PullClient(
        {1: [{"number": 9, "head": {"ref": "candidate/demo", "sha": head}}]}
    )
    audit = _AuditLogger()
    guard_calls: list[str] = []
    _configure_cleanup_adapter(
        tmp_path,
        monkeypatch,
        client=client,
        audit=audit,
        guard_calls=guard_calls,
    )

    result = await supervisor.candidate_cleanup(
        "candidate-demo",
        head,
        "candidate/demo",
        "demo",
        "archive/candidate-demo",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "WORKSPACE_CONTENDED"
    assert result["error"]["details"]["pull_number"] == 9
    assert audit.required == []


async def test_candidate_cleanup_remote_reference_failure_is_typed_and_non_mutating(
    tmp_path, immediate_run_tool_async, monkeypatch
):
    client = _PullClient(error=TimeoutError("remote timeout"))
    audit = _AuditLogger()
    guard_calls: list[str] = []
    _configure_cleanup_adapter(
        tmp_path,
        monkeypatch,
        client=client,
        audit=audit,
        guard_calls=guard_calls,
    )

    result = await supervisor.candidate_cleanup(
        "candidate-demo",
        "a" * 40,
        "candidate/demo",
        "demo",
        "archive/candidate-demo",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "CHECK_FAILED"
    assert result["error"]["retryable"] is True
    assert result["error"]["details"]["reference_check"] == "gitea_open_pull_requests"
    assert audit.required == []


async def test_candidate_cleanup_audit_failure_blocks_before_core_mutation(
    tmp_path, immediate_run_tool_async, monkeypatch
):
    client = _PullClient()
    audit = _AuditLogger(fail_required=True)
    guard_calls: list[str] = []
    _configure_cleanup_adapter(
        tmp_path,
        monkeypatch,
        client=client,
        audit=audit,
        guard_calls=guard_calls,
    )

    result = await supervisor.candidate_cleanup(
        "candidate-demo",
        "a" * 40,
        "candidate/demo",
        "demo",
        "archive/candidate-demo",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "AUDIT_UNAVAILABLE"
    assert result["error"]["retryable"] is True
    assert guard_calls == ["first"]
    assert audit.success == []


def test_register_all_registers_exactly_five_tools(monkeypatch):
    registered: list[str] = []

    def _register(name):
        def _decorator(fn):
            registered.append(name)
            return fn

        return _decorator

    monkeypatch.setattr(supervisor, "register_tool", _register)
    monkeypatch.setattr(supervisor, "instrumented", lambda name: (lambda fn: fn))

    supervisor.register_all()

    assert registered == [
        "supervisor_integrate_file",
        "supervisor_recover_integrations",
        "supervisor_register_project",
        "prepare_candidate_clone",
        "candidate_cleanup",
    ]


def test_register_project_readonly_filesystem_is_canonical_and_path_safe(
    tmp_path, immediate_run_tool, monkeypatch
):
    config_dir = tmp_path / "registry"
    config_dir.mkdir()
    journal_root = tmp_path / "journals"
    monkeypatch.setattr(supervisor, "_resolve_registry_config_dir", lambda: config_dir)
    monkeypatch.setattr(supervisor, "_journal_root_for_project", lambda _project, _root: journal_root)

    def _raise(**_kwargs):
        raise OSError(errno.EROFS, "Read-only file system", str(config_dir / "projects.yaml"))

    monkeypatch.setattr(supervisor, "register_project", _raise)

    result = supervisor.supervisor_register_project("new-project", "new-project")

    assert result["ok"] is False
    assert result["error"]["code"] == "WORKSPACE_READONLY"
    assert result["error"]["hint"]
    assert str(config_dir) not in repr(result)
    assert str(journal_root) not in repr(result)


def test_register_project_signature_does_not_expose_server_paths():
    signature = inspect.signature(supervisor.supervisor_register_project)
    assert list(signature.parameters) == [
        "project_id",
        "root",
        "project_type",
        "description",
        "tags",
        "parent",
        "persist_to_source",
        "root_selector",
    ]
    assert "registry_path" not in signature.parameters
    assert "config_dir" not in signature.parameters
    assert "journal_root" not in signature.parameters
    assert "project_root" not in signature.parameters
