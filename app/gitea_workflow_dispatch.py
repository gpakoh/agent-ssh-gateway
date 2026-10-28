"""Explicit, journalled workflow dispatch; an uncertain POST is never resent.

The journal uses the same persistent audit volume as coordination-close. A
receipt is fsynced before POST and an OS lock excludes other MCP processes.
Only a run ID returned by that POST, then independently checked, can be bound.
Listing a single plausible run after a lost response is not correlation proof.
"""

from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

MAX_WORKFLOW_BYTES = 65536
MAX_RECEIPT_BYTES = 32768
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
WORKFLOW = re.compile(r"(?:\.gitea|\.github)/workflows/[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml\Z")
SENSITIVE = re.compile(r"token|password|passwd|secret|api.?key|private.?key|authorization", re.I)


class DispatchRefused(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_target(owner: str, repo: str, workflow: str, ref: str) -> str:
    if not all(isinstance(x, str) and NAME.fullmatch(x) for x in (owner, repo)):
        raise DispatchRefused("INVALID_INPUT", "Invalid repository identity")
    if not isinstance(workflow, str) or not WORKFLOW.fullmatch(workflow) or ".." in workflow:
        raise DispatchRefused("INVALID_INPUT", "Expected one repository workflow YAML path")
    if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
        raise DispatchRefused("INVALID_INPUT", "Only explicit refs/heads/<branch> are supported")
    branch = ref.removeprefix("refs/heads/")
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch)
        or any(x in branch for x in ("..", "//"))
        or branch.endswith(("/", ".", ".lock"))
    ):
        raise DispatchRefused("INVALID_INPUT", "Invalid workflow branch ref")
    return branch


class WorkflowLoader(yaml.SafeLoader):
    """YAML 1.2 booleans: the workflow key 'on' must remain a string."""


WorkflowLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for _first, _resolvers in WorkflowLoader.yaml_implicit_resolvers.items():
    WorkflowLoader.yaml_implicit_resolvers[_first] = [
        item for item in _resolvers if item[0] != "tag:yaml.org,2002:bool"
    ]
WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false|True|False|TRUE|FALSE)$"), list("tTfF")
)


def _mapping(loader: WorkflowLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, (str, int, float, bool)) or key in result:
            raise DispatchRefused("WORKFLOW_INVALID", "Duplicate or unsupported workflow key")
        result[key] = loader.construct_object(value_node)
    return result


WorkflowLoader.add_constructor("tag:yaml.org,2002:map", _mapping)


def workflow_inputs(raw: bytes, supplied: dict[str, Any]) -> dict[str, str]:
    """Validate scalar values strictly, then encode Gitea's string input map."""
    if len(raw) > MAX_WORKFLOW_BYTES or not isinstance(supplied, dict) or len(supplied) > 25:
        raise DispatchRefused("INVALID_INPUT", "Workflow or inputs exceed bounds")
    try:
        events = list(yaml.parse(raw))
        if len(events) > 20000 or sum(isinstance(e, yaml.AliasEvent) for e in events) > 25:
            raise DispatchRefused("WORKFLOW_INVALID", "Workflow YAML exceeds structural bounds")
        document = yaml.load(raw.decode("utf-8"), Loader=WorkflowLoader)
        triggers = document["on"]
        if isinstance(triggers, str):
            triggers = {triggers: None}
        elif isinstance(triggers, list):
            triggers = dict.fromkeys(triggers)
        if not isinstance(triggers, dict) or "workflow_dispatch" not in triggers:
            raise DispatchRefused("WORKFLOW_NOT_MANUAL", "Workflow has no workflow_dispatch event")
        dispatch = triggers["workflow_dispatch"]
        if dispatch is None:
            schema = {}
        elif isinstance(dispatch, dict):
            schema = dispatch.get("inputs", {})
        else:
            raise ValueError
        if not isinstance(schema, dict) or len(schema) > 25:
            raise ValueError
    except DispatchRefused:
        raise
    except (ValueError, KeyError, TypeError, RecursionError, yaml.YAMLError) as exc:
        raise DispatchRefused("WORKFLOW_INVALID", "Cannot validate workflow input schema") from exc
    if set(supplied) - set(schema):
        raise DispatchRefused("INVALID_INPUT", "Unknown workflow input")
    encoded: dict[str, str] = {}
    for key, spec in schema.items():
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", key)
            or SENSITIVE.search(key)
            or not isinstance(spec, dict)
            or type(spec.get("required", False)) is not bool
        ):
            raise DispatchRefused("WORKFLOW_INVALID", "Unsupported or sensitive workflow input")
        kind = spec.get("type", "string")
        if kind not in {"string", "boolean", "number", "choice"}:
            raise DispatchRefused("WORKFLOW_INVALID", "Unsupported workflow input type")
        if key not in supplied and "default" not in spec:
            if spec.get("required"):
                raise DispatchRefused("INVALID_INPUT", "Required workflow input is missing")
            continue
        value = supplied.get(key, spec.get("default"))
        valid = (
            (kind == "boolean" and type(value) is bool)
            or (kind == "number" and type(value) is int and abs(value) <= 2**53 - 1)
            or (kind == "number" and type(value) is float and math.isfinite(value))
            or (kind in {"string", "choice"} and type(value) is str)
        )
        if kind == "choice":
            choices = spec.get("options")
            valid = (
                valid
                and isinstance(choices, list)
                and 0 < len(choices) <= 100
                and all(type(x) is str for x in choices)
                and value in choices
            )
        if not valid or (spec.get("required") and kind in {"string", "choice"} and value == ""):
            raise DispatchRefused("INVALID_INPUT", "Workflow input has an invalid type or choice")
        text = str(value).lower() if type(value) is bool else str(value)
        if len(text) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise DispatchRefused("INVALID_INPUT", "Workflow input exceeds scalar bounds")
        encoded[key] = text
    if len(json.dumps(encoded).encode()) > 8192:
        raise DispatchRefused("INVALID_INPUT", "Workflow input payload exceeds bounds")
    return encoded


def _private(fd: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or info.st_mode & 0o077
    ):
        raise DispatchRefused(
            "RECEIPT_UNAVAILABLE", "Dispatch receipt is not a private regular file"
        )


@contextmanager
def operation_lock(root: Path, key: str):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DispatchRefused("RECEIPT_UNAVAILABLE", "Dispatch journal directory is not private")
    fd = os.open(
        root / (digest(key) + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        _private(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DispatchRefused(
                "DISPATCH_BUSY", "This dispatch operation is already active"
            ) from exc
        yield root / (digest(key) + ".json")
    finally:
        os.close(fd)


def read_receipt(path: Path) -> dict[str, Any] | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        _private(stream.fileno())
        raw = stream.read(MAX_RECEIPT_BYTES + 1)
    try:
        value = json.loads(raw)
        if (
            len(raw) > MAX_RECEIPT_BYTES
            or not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or value.get("phase") not in {"dispatched", "accepted", "unknown", "rejected"}
            or not all(
                isinstance(value.get(k), str) and HASH.fullmatch(value[k])
                for k in ("payload_sha256", "actor", "workflow_sha256")
            )
            or (
                value.get("run_id") is not None
                and (type(value["run_id"]) is not int or value["run_id"] < 1)
            )
        ):
            raise ValueError
        validate_target(value["owner"], value["repo"], value["workflow"], value["ref"])
        if (
            not SHA.fullmatch(value["expected_ref_sha"])
            or type(value["baseline_id"]) is not int
            or value["baseline_id"] < 0
        ):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise DispatchRefused(
            "RECEIPT_UNAVAILABLE", "Dispatch receipt is unreadable or invalid"
        ) from exc


def save_receipt(path: Path, value: dict[str, Any]) -> None:
    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    if len(raw) > MAX_RECEIPT_BYTES:
        raise DispatchRefused("RECEIPT_UNAVAILABLE", "Dispatch receipt exceeds bounds")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        fd = os.open(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _runs(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
        raise DispatchRefused("REMOTE_SHAPE_INVALID", "Invalid workflow run list")
    runs = data["workflow_runs"]
    if len(runs) > 50 or any(
        not isinstance(r, dict) or type(r.get("id")) is not int or r["id"] < 1 for r in runs
    ):
        raise DispatchRefused("REMOTE_SHAPE_INVALID", "Invalid workflow run identity")
    return runs


async def _identity(client: Any, owner: str, repo: str) -> tuple[str, str]:
    user = await client.get_user()
    meta = await client.get_repo(owner, repo)
    account = user.get("login")
    if (
        not isinstance(account, str)
        or not NAME.fullmatch(account)
        or meta.get("full_name") != f"{owner}/{repo}"
        or meta.get("archived") is not False
        or (meta.get("permissions") or {}).get("push") is not True
        or (meta.get("permissions") or {}).get("admin") is not True
    ):
        raise DispatchRefused(
            "AUTH_ERROR", "Repository identity or administrator permission is not verified"
        )
    base = meta.get("html_url", "")
    url = urlsplit(base)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path != f"/{owner}/{repo}"
    ):
        raise DispatchRefused("REMOTE_SHAPE_INVALID", "Invalid repository public URL")
    return account, base


async def _view(client: Any, receipt: dict[str, Any]) -> dict[str, Any]:
    """Read-only reconciliation. Plausible candidates never become our run."""
    result = {
        k: receipt[k]
        for k in (
            "owner",
            "repo",
            "workflow",
            "ref",
            "expected_ref_sha",
            "workflow_sha256",
            "payload_sha256",
        )
    }
    result.update(
        dispatch_outcome="unknown" if receipt["phase"] == "dispatched" else receipt["phase"],
        run_id=receipt.get("run_id"),
        verified=False,
    )
    if receipt["phase"] == "rejected":
        return {**result, "state": "rejected", "http_status": receipt.get("http_status")}
    try:
        runs = _runs(
            await client.list_workflow_runs(
                receipt["owner"], receipt["repo"], receipt["workflow"].rsplit("/", 1)[-1]
            )
        )
        candidates = [
            r
            for r in runs
            if r["id"] > receipt["baseline_id"]
            and r.get("event") == "workflow_dispatch"
            and r.get("head_sha") == receipt["expected_ref_sha"]
            and (r.get("actor") or {}).get("login") == receipt["service_account"]
        ]
        if receipt.get("run_id") is None:
            return {
                **result,
                "state": "accepted_unbound"
                if receipt["phase"] == "accepted"
                else "outcome_unknown",
                "candidate_run_ids": [r["id"] for r in candidates],
                "replay_allowed": False,
            }
        if receipt["run_id"] not in {r["id"] for r in candidates}:
            return {**result, "state": "run_not_verified", "replay_allowed": False}
        run = await client.get_action_run(receipt["owner"], receipt["repo"], receipt["run_id"])
        if (
            run.get("id") != receipt["run_id"]
            or type(run.get("id")) is not int
            or run.get("event") != "workflow_dispatch"
            or run.get("head_sha") != receipt["expected_ref_sha"]
            or (run.get("actor") or {}).get("login") != receipt["service_account"]
            or (run.get("repository") or {}).get("full_name")
            != f"{receipt['owner']}/{receipt['repo']}"
            or run.get("head_branch")
            not in {receipt["ref"], receipt["ref"].removeprefix("refs/heads/")}
            or type(run.get("run_attempt")) is not int
            or run["run_attempt"] < 1
        ):
            return {**result, "state": "run_identity_mismatch", "replay_allowed": False}
        return {
            **result,
            "state": "bound",
            "verified": True,
            "run_attempt": run["run_attempt"],
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "html_url": receipt["repo_url"] + "/actions/runs/" + str(receipt["run_id"]),
        }
    except Exception:
        return {**result, "state": "reconciliation_unavailable", "replay_allowed": False}


async def dispatch_workflow(
    client: Any,
    *,
    owner: str,
    repo: str,
    workflow: str,
    ref: str,
    expected_ref_sha: str,
    inputs: dict[str, Any],
    operation_key: str,
    reason: str,
    actor: str,
    journal_dir: Path,
    audit: Any,
    dry_run: bool = False,
) -> dict[str, Any]:
    branch = validate_target(owner, repo, workflow, ref)
    if (
        not isinstance(expected_ref_sha, str)
        or not SHA.fullmatch(expected_ref_sha)
        or not isinstance(operation_key, str)
        or not KEY.fullmatch(operation_key)
    ):
        raise DispatchRefused("INVALID_INPUT", "Expected immutable SHA and bounded operation key")
    if not isinstance(actor, str) or not HASH.fullmatch(actor):
        raise DispatchRefused("AUTH_ERROR", "Authenticated caller identity is required")
    if not isinstance(reason, str) or not 20 <= len(reason) <= 500 or type(dry_run) is not bool:
        raise DispatchRefused(
            "INVALID_INPUT", "Explicit operator reason and boolean dry_run are required"
        )
    try:
        supplied = json.loads(json.dumps(inputs, allow_nan=False))
        payload_sha = digest([owner, repo, workflow, ref, expected_ref_sha, supplied, reason])
    except (ValueError, TypeError) as exc:
        raise DispatchRefused("INVALID_INPUT", "Inputs must be finite JSON scalar values") from exc
    account, repo_url = await _identity(client, owner, repo)
    with operation_lock(journal_dir, operation_key) as path:
        old = read_receipt(path)
        if old is not None:
            if old["actor"] != actor:
                raise DispatchRefused("AUTH_ERROR", "Dispatch operation belongs to another caller")
            if old["payload_sha256"] != payload_sha:
                raise DispatchRefused(
                    "OPERATION_KEY_CONFLICT",
                    "Operation key is already bound to a different request",
                )
            return await _view(client, old)
        version = await client.get_version()
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[+\-].*)?$", str(version.get("version", "")))
        if not match or tuple(map(int, match.groups())) < (1, 24, 0):
            raise DispatchRefused(
                "CAPABILITY_UNAVAILABLE",
                "This Gitea version does not attest workflow_dispatch support",
            )
        head = await client.get_branch(owner, repo, branch)
        if (head.get("commit") or {}).get("id") != expected_ref_sha:
            raise DispatchRefused("REF_DRIFT", "Workflow branch no longer matches expected_ref_sha")
        # Dispatch addresses a file name, not a directory. Attest the path
        # Gitea actually resolves so a .github/.gitea name collision cannot
        # silently dispatch a different definition at the same commit.
        available = await client.list_workflows(owner, repo)
        entries = available.get("workflows") if isinstance(available, dict) else None
        if not isinstance(entries, list) or len(entries) > 1000:
            raise DispatchRefused("CAPABILITY_UNAVAILABLE", "Workflow registry is unavailable")
        selected = [
            w for w in entries if isinstance(w, dict) and w.get("id") == workflow.rsplit("/", 1)[-1]
        ]
        if (
            len(selected) != 1
            or selected[0].get("path") != workflow
            or selected[0].get("state") != "active"
        ):
            raise DispatchRefused(
                "WORKFLOW_IDENTITY_MISMATCH",
                "Workflow is not active at the reviewed repository path",
            )
        content = await client.get_file(
            owner,
            repo,
            path=workflow,
            branch=expected_ref_sha,
            include_content=True,
            max_content_bytes=MAX_WORKFLOW_BYTES,
        )
        try:
            if (
                content.get("type") != "file"
                or content.get("encoding") != "base64"
                or content.get("resolved_commit_sha") != expected_ref_sha
                or content.get("path") != workflow
            ):
                raise ValueError
            raw = base64.b64decode(content["content"], validate=True)
        except (ValueError, TypeError, KeyError) as exc:
            raise DispatchRefused(
                "WORKFLOW_INVALID", "Workflow source is not attested at the expected commit"
            ) from exc
        values = workflow_inputs(raw, supplied)
        before = _runs(await client.list_workflow_runs(owner, repo, workflow.rsplit("/", 1)[-1]))
        if ((await client.get_branch(owner, repo, branch)).get("commit") or {}).get(
            "id"
        ) != expected_ref_sha:
            raise DispatchRefused("REF_DRIFT", "Workflow ref changed during preflight")
        receipt = {
            "version": 1,
            "phase": "dispatched",
            "actor": actor,
            "owner": owner,
            "repo": repo,
            "workflow": workflow,
            "ref": ref,
            "expected_ref_sha": expected_ref_sha,
            "payload_sha256": payload_sha,
            "workflow_sha256": hashlib.sha256(raw).hexdigest(),
            "service_account": account,
            "repo_url": repo_url,
            "baseline_id": max((r["id"] for r in before), default=0),
            "run_id": None,
            "started_at": int(time.time()),
        }
        if dry_run:
            return {
                k: receipt[k]
                for k in (
                    "owner",
                    "repo",
                    "workflow",
                    "ref",
                    "expected_ref_sha",
                    "payload_sha256",
                    "workflow_sha256",
                )
            } | {"state": "preflight_passed", "dispatched": False, "input_names": sorted(values)}
        audit(
            {
                "actor": actor,
                "owner": owner,
                "repo": repo,
                "workflow": workflow,
                "expected_ref_sha": expected_ref_sha,
                "payload_sha256": payload_sha,
                "workflow_sha256": receipt["workflow_sha256"],
                "operation_key_sha256": digest(operation_key),
            }
        )
        save_receipt(path, receipt)  # Required durable boundary BEFORE POST.
        try:
            response = await client.dispatch_workflow(
                owner, repo, workflow.rsplit("/", 1)[-1], ref, values
            )
            run_id = response.get("run_id")
            if run_id is not None and (type(run_id) is not int or run_id <= receipt["baseline_id"]):
                raise ValueError("Invalid dispatch response")
            receipt.update(phase="accepted", run_id=run_id)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            receipt.update(
                phase="rejected" if 400 <= status < 500 else "unknown", http_status=status
            )
        except Exception:
            receipt.update(phase="unknown", run_id=None)
        # If the process dies or this write fails, the original dispatched
        # receipt still forbids any subsequent POST with this operation key.
        try:
            save_receipt(path, receipt)
        except (OSError, DispatchRefused):
            return {
                "state": "outcome_unknown",
                "verified": False,
                "replay_allowed": False,
                "receipt_persist_failed": True,
            }
        return await _view(client, receipt)


async def dispatch_status(
    client: Any,
    *,
    owner: str,
    repo: str,
    operation_key: str,
    actor: str,
    journal_dir: Path,
) -> dict[str, Any]:
    if (
        not all(isinstance(x, str) and NAME.fullmatch(x) for x in (owner, repo))
        or not isinstance(operation_key, str)
        or not KEY.fullmatch(operation_key)
    ):
        raise DispatchRefused("INVALID_INPUT", "Invalid repository or operation key")
    if not isinstance(actor, str) or not HASH.fullmatch(actor):
        raise DispatchRefused("AUTH_ERROR", "Authenticated caller identity is required")
    await _identity(client, owner, repo)
    with operation_lock(journal_dir, operation_key) as path:
        receipt = read_receipt(path)
        if receipt is None:
            return {"state": "not_found", "dispatched": False}
        if receipt["actor"] != actor or (receipt["owner"], receipt["repo"]) != (owner, repo):
            raise DispatchRefused(
                "AUTH_ERROR", "Dispatch operation does not match this caller and repository"
            )
        return await _view(client, receipt)
