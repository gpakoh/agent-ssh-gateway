"""Read-only project release guidance sourced from the Infra catalog."""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from tool_results import tool_error, tool_success

from examples.mcp_server.mcp_infra.adapters import coordination_adapter, docker, remote
from examples.mcp_server.mcp_infra.tool_registry import register_tool

_CATALOG_PATH = "deploy/project-release-guides.json"
_CATALOG_LIMIT = 262144
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
_SHA = re.compile(r"[0-9a-f]{40}")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Service(_Model):
    service: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    container: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    purpose: str = Field(min_length=1, max_length=400)


class _Workflow(_Model):
    repository: str = Field(min_length=1, max_length=80)
    branch: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=240)
    trigger: Literal[
        "manual_exact_sha", "manual_operator", "push_after_merge", "push_guarded", "blocked"
    ]
    audited_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    audited_blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    sha_input: str | None = Field(default=None, max_length=80)
    description: str = Field(min_length=1, max_length=400)


class _Condition(_Model):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=400)
    hint: str = Field(min_length=1, max_length=400)


class _SourceEvidence(_Model):
    repository: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=240)
    ref: str = Field(min_length=1, max_length=120)
    audited_blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class _Guide(_Model):
    repository: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    compose_project: str | None = Field(default=None, min_length=1, max_length=80)
    services: list[_Service] = Field(default_factory=list, max_length=20)
    workflow: _Workflow | None = None
    steps: list[str] = Field(min_length=1, max_length=20)
    conditions: list[_Condition] = Field(min_length=1, max_length=20)
    source_files: list[str] = Field(default_factory=list, max_length=20)
    source_evidence: list[_SourceEvidence] = Field(default_factory=list, max_length=20)
    notes: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def require_compose_identity_for_services(self) -> _Guide:
        if self.services and not self.compose_project:
            raise ValueError("services require a Compose project identity")
        return self


class _Catalog(_Model):
    schema_version: Literal[1]
    projects: dict[str, _Guide] = Field(max_length=300)


def _catalog(data: object) -> tuple[_Catalog, str, str]:
    if not isinstance(data, dict) or data.get("truncated") or data.get("encoding") != "base64":
        raise ValueError("release guide catalog is unavailable or incomplete")
    encoded = data.get("content")
    commit = data.get("resolved_commit_sha")
    blob = data.get("sha")
    if (
        not isinstance(encoded, str)
        or len(encoded) > 4 * ((_CATALOG_LIMIT + 2) // 3) + 2048
        or not isinstance(commit, str)
        or not _SHA.fullmatch(commit)
        or not isinstance(blob, str)
        or not _SHA.fullmatch(blob)
    ):
        raise ValueError("release guide catalog provenance is invalid")
    try:
        raw = base64.b64decode("".join(encoded.split()), validate=True)
        if len(raw) > _CATALOG_LIMIT:
            raise ValueError("release guide catalog is too large")
        parsed = _Catalog.model_validate_json(raw)
    except (binascii.Error, UnicodeError, ValidationError) as exc:
        raise ValueError("release guide catalog is invalid") from exc
    for name, guide in parsed.projects.items():
        if not _NAME.fullmatch(name) or ".." in name:
            raise ValueError("release guide project identifier is invalid")
        if len({item.container for item in guide.services}) != len(guide.services):
            raise ValueError("release guide has duplicate containers")
        if len({item.id for item in guide.conditions}) != len(guide.conditions):
            raise ValueError("release guide has duplicate conditions")
        if len({(item.repository, item.path, item.ref) for item in guide.source_evidence}) != len(
            guide.source_evidence
        ):
            raise ValueError("release guide has duplicate source evidence")
        for item in guide.source_evidence:
            if not _NAME.fullmatch(item.repository) or ".." in item.repository:
                raise ValueError("release guide source repository is invalid")
            if item.path.startswith("/") or ".." in item.path.split("/") or "\\" in item.path:
                raise ValueError("release guide source path is invalid")
            if item.ref.startswith("-") or ".." in item.ref or "\\" in item.ref:
                raise ValueError("release guide source ref is invalid")
        for value in [*guide.steps, *guide.source_files, *guide.notes]:
            if not value or len(value) > 400:
                raise ValueError("release guide text is invalid")
    return parsed, commit, blob


async def _observe(service: _Service, compose_project: str) -> dict:
    try:
        response = await asyncio.wait_for(docker.docker_inspect(service.container), timeout=5.0)
    except Exception:
        response = None
    if not isinstance(response, dict) or not response.get("ok"):
        return {"status": "unknown", "reason": "Не удалось проверить контейнер через Gateway."}
    if response.get("meta", {}).get("truncated"):
        return {"status": "unknown", "reason": "Ответ Docker неполный."}
    rows = response.get("result")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return {"status": "unknown", "reason": "Не удалось подтвердить идентичность контейнера."}
    item = rows[0]
    config, state = item.get("Config"), item.get("State")
    if not isinstance(config, dict) or not isinstance(state, dict):
        return {"status": "unknown", "reason": "Ответ Docker не содержит состояние контейнера."}
    labels = config.get("Labels")
    if not isinstance(labels, dict) or (
        labels.get("com.docker.compose.project") != compose_project
        or labels.get("com.docker.compose.service") != service.service
    ):
        return {"status": "unknown", "reason": "Контейнер не подтверждён как нужный сервис Compose."}
    health = state.get("Health")
    health_status = health.get("Status") if isinstance(health, dict) else None
    image = config.get("Image")
    return {
        "status": "observed",
        "running": state.get("Running") if type(state.get("Running")) is bool else None,
        "health": health_status if health_status in {"healthy", "unhealthy", "starting"} else None,
        "image": image if isinstance(image, str) and len(image) <= 512 else None,
        "restart_count": item.get("RestartCount") if type(item.get("RestartCount")) is int else None,
    }


async def _observe_blob(owner: str, repository: str, path: str, ref: str, audited_sha: str) -> dict:
    """Compare a reviewed Git blob against its current or pinned source."""
    try:
        response = await asyncio.wait_for(
            remote.gitea_get_file(
                owner, repository, path, branch=ref,
                include_content=False, max_content_bytes=1,
            ),
            timeout=8.0,
        )
    except Exception:
        response = None
    if not isinstance(response, dict) or not response.get("ok"):
        return {"status": "unknown", "reason": "Не удалось проверить исходный файл."}
    if response.get("meta", {}).get("truncated"):
        return {"status": "unknown", "reason": "Ответ об исходном файле неполный."}
    data = response.get("result")
    if not isinstance(data, dict):
        return {"status": "unknown", "reason": "Ответ об исходном файле неполный."}
    blob, commit = data.get("sha"), data.get("resolved_commit_sha")
    if (
        not isinstance(blob, str) or not _SHA.fullmatch(blob)
        or not isinstance(commit, str) or not _SHA.fullmatch(commit)
    ):
        return {"status": "unknown", "reason": "Ревизия исходного файла не подтверждена."}
    return {
        "status": "unchanged" if blob == audited_sha else "changed",
        "current_commit_sha": commit,
        "current_blob_sha": blob,
    }


async def _observe_workflow(owner: str, workflow: _Workflow) -> dict:
    return await _observe_blob(
        owner, workflow.repository, workflow.path, workflow.branch, workflow.audited_blob_sha
    )


async def _observe_source(owner: str, source: _SourceEvidence, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        return await _observe_blob(
            owner, source.repository, source.path, source.ref, source.audited_blob_sha
        )


async def _bounded_observe(
    service: _Service, compose_project: str, semaphore: asyncio.Semaphore
) -> dict:
    async with semaphore:
        return await _observe(service, compose_project)


def _unavailable_workspace(project: str) -> str | None:
    try:
        registry = coordination_adapter.server_attr("_get_workspace_registry")()
        entries = registry.list_unavailable_projects()
    except Exception:
        return None
    for entry in entries:
        if (
            isinstance(entry, dict)
            and entry.get("project_id") == project
            and entry.get("source") == "source_registry"
            and entry.get("reason") in {"root_missing", "root_not_directory"}
        ):
            return str(entry["reason"])
    return None


def _source_freshness(guide: _Guide, observations: list[dict], infra_repo: str) -> str:
    if any(item.get("status") == "changed" for item in observations):
        return "changed"
    evidenced_paths = {
        item.path for item in guide.source_evidence if item.repository == infra_repo
    }
    if (
        not observations
        or any(item.get("status") != "unchanged" for item in observations)
        or not set(guide.source_files).issubset(evidenced_paths)
    ):
        return "unknown"
    return "unchanged"


async def project_release_guide(project: str) -> dict:
    """Show the reviewed Infra release guide and live container observations.

    Read-only: never starts a workflow, restarts a service or authorizes a release.
    Conditions not checked by this tool remain explicitly unknown. A missing profile
    returns unverified workflow candidates rather than guessing a deployment plan.
    """
    tool = "project_release_guide"
    if not isinstance(project, str) or not _NAME.fullmatch(project) or ".." in project:
        return tool_error(tool=tool, code="INVALID_INPUT", message="Invalid project identifier.")
    try:
        owner = coordination_adapter._owner()
        infra_repo = coordination_adapter._registered("infra-quart")
    except ValueError:
        return tool_error(tool=tool, code="SOURCE_UNAVAILABLE", message="Infra source is unavailable.")
    unavailable_reason = None
    try:
        repo: str | None = coordination_adapter._registered(project)
    except ValueError:
        unavailable_reason = _unavailable_workspace(project)
        if unavailable_reason is None:
            return tool_error(tool=tool, code="PROJECT_NOT_FOUND", message="Project is not registered.")
        repo = None
    try:
        response = await remote.gitea_get_file(
            owner, infra_repo, _CATALOG_PATH, branch="main",
            include_content=True, max_content_bytes=_CATALOG_LIMIT,
        )
    except Exception:
        response = None
    if not isinstance(response, dict) or not response.get("ok"):
        return tool_error(
            tool=tool, code="SOURCE_UNAVAILABLE", source="gitea",
            message="Не удалось прочитать каталог выпусков Infra.",
            hint="Проверьте доступ к infra-quart и опубликованный каталог; не считайте выпуск готовым.",
        )
    try:
        catalog, commit, blob = _catalog(response.get("result"))
    except ValueError:
        return tool_error(
            tool=tool, code="REMOTE_SCHEMA_INVALID", source="gitea",
            message="Каталог выпусков Infra неполный или имеет неверный формат.",
        )
    provenance = {"repository": f"{owner}/{infra_repo}", "path": _CATALOG_PATH,
                  "commit_sha": commit, "blob_sha": blob}
    guide = catalog.projects.get(project)
    if repo is None:
        # A configured-but-missing workspace grants no filesystem access. Use a
        # reviewed catalog mapping when present; otherwise the same-name Gitea
        # repository is merely an unverified discovery candidate.
        repo = guide.repository if guide is not None else project
        if not _NAME.fullmatch(repo) or ".." in repo:
            return tool_error(tool=tool, code="REMOTE_SCHEMA_INVALID", message="Invalid repository mapping.")
        try:
            repo_response = await asyncio.wait_for(remote.gitea_get_repo(owner, repo), timeout=8.0)
        except Exception:
            repo_response = None
        metadata = repo_response.get("result") if isinstance(repo_response, dict) and repo_response.get("ok") else None
        if not isinstance(metadata, dict) or metadata.get("full_name") != f"{owner}/{repo}":
            return tool_error(
                tool=tool, code="SOURCE_UNAVAILABLE", source="gitea",
                message="Репозиторий проекта не подтверждён; рабочий каталог недоступен.",
            )
    if guide is None:
        try:
            workflows = await remote.gitea_list_workflows(owner, repo)
        except Exception:
            workflows = None
        result = workflows.get("result") if isinstance(workflows, dict) and workflows.get("ok") else None
        candidates = result.get("workflows") if isinstance(result, dict) else None
        if not isinstance(candidates, list):
            candidates = None
        else:
            candidates = [
                {"name": row.get("name"), "path": row.get("path")}
                for row in candidates[:40] if isinstance(row, dict)
            ]
        return tool_success(tool, source="gitea", result={
            "project": project, "repository": f"{owner}/{repo}", "configured": False,
            "source": provenance, "workflow_candidates": candidates,
            "workspace": {"status": "unavailable", "reason": unavailable_reason} if unavailable_reason else None,
            "note": "Профиль не подготовлен. Список workflow и имя репозитория не подтверждают способ выпуска.",
            "ready_for_release": "unknown",
        })
    if (
        guide.repository != repo
        or (guide.workflow is not None and guide.workflow.repository not in {repo, infra_repo})
        or any(item.repository not in {repo, infra_repo} for item in guide.source_evidence)
    ):
        return tool_error(
            tool=tool, code="SOURCE_HASH_MISMATCH", source="gitea",
            message="Репозиторий проекта не совпадает с описанием Infra.",
        )
    semaphore = asyncio.Semaphore(3)
    observations = await asyncio.gather(
        *(_bounded_observe(service, guide.compose_project or "", semaphore) for service in guide.services)
    )
    workflow_observation = (
        await _observe_workflow(owner, guide.workflow) if guide.workflow is not None else None
    )
    source_observations = await asyncio.gather(
        *(_observe_source(owner, source, semaphore) for source in guide.source_evidence)
    )
    services = [
        {**service.model_dump(), "observation": observation}
        for service, observation in zip(guide.services, observations, strict=True)
    ]
    return tool_success(tool, source="gitea", result={
        "project": project, "configured": True, "title": guide.title,
        "repository": f"{owner}/{repo}", "source": provenance,
        "workflow": (
            {**guide.workflow.model_dump(), "observation": workflow_observation}
            if guide.workflow is not None else None
        ),
        "services": services,
        "steps": guide.steps,
        "conditions": [{**condition.model_dump(), "status": "unknown"} for condition in guide.conditions],
        "source_files": guide.source_files,
        "source_evidence": [
            {**source.model_dump(), "observation": observation}
            for source, observation in zip(guide.source_evidence, source_observations, strict=True)
        ],
        "source_freshness": _source_freshness(guide, source_observations, infra_repo),
        "workspace": {"status": "unavailable", "reason": unavailable_reason} if unavailable_reason else None,
        "notes": guide.notes,
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "ready_for_release": "unknown",
        "notice": (
            "Это подсказка, не разрешение на выпуск. Каталог взят из main Infra; "
            "фактическая версия Infra на сервере может отличаться. "
            "CI, откат и условия должны быть проверены отдельно. "
            "При изменении workflow или deploy-скрипта необходимо перепроверить инструкцию перед выпуском."
        ),
    })


def register_all() -> None:
    register_tool("project_release_guide")(project_release_guide)
