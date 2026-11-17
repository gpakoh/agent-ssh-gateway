"""Read-only project release guidance sourced from the Infra catalog."""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tool_results import tool_error, tool_success

from examples.mcp_server.mcp_infra.adapters import coordination_adapter, docker, remote
from examples.mcp_server.mcp_infra.tool_registry import register_tool

_CATALOG_PATH = "deploy/project-release-guides.json"
_CATALOG_LIMIT = 65536
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
    trigger: Literal["manual_exact_sha", "push_after_merge", "push_guarded"]
    audited_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    audited_blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    sha_input: str | None = Field(default=None, max_length=80)
    description: str = Field(min_length=1, max_length=400)


class _Condition(_Model):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=400)
    hint: str = Field(min_length=1, max_length=400)


class _Guide(_Model):
    repository: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    compose_project: str = Field(min_length=1, max_length=80)
    services: list[_Service] = Field(min_length=1, max_length=20)
    workflow: _Workflow
    steps: list[str] = Field(min_length=1, max_length=20)
    conditions: list[_Condition] = Field(min_length=1, max_length=20)
    source_files: list[str] = Field(min_length=1, max_length=20)
    notes: list[str] = Field(max_length=20)


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


async def _observe_workflow(owner: str, workflow: _Workflow) -> dict:
    """Check whether the reviewed workflow is still the one on its branch."""
    try:
        response = await asyncio.wait_for(
            remote.gitea_get_file(
                owner, workflow.repository, workflow.path, branch=workflow.branch,
                include_content=False, max_content_bytes=1,
            ),
            timeout=8.0,
        )
    except Exception:
        response = None
    if not isinstance(response, dict) or not response.get("ok"):
        return {"status": "unknown", "reason": "Не удалось проверить текущий workflow."}
    if response.get("meta", {}).get("truncated"):
        return {"status": "unknown", "reason": "Ответ о workflow неполный."}
    data = response.get("result")
    if not isinstance(data, dict):
        return {"status": "unknown", "reason": "Ответ о workflow неполный."}
    blob, commit = data.get("sha"), data.get("resolved_commit_sha")
    if (
        not isinstance(blob, str) or not _SHA.fullmatch(blob)
        or not isinstance(commit, str) or not _SHA.fullmatch(commit)
    ):
        return {"status": "unknown", "reason": "Ревизия workflow не подтверждена."}
    return {
        "status": "unchanged" if blob == workflow.audited_blob_sha else "changed",
        "current_commit_sha": commit,
        "current_blob_sha": blob,
    }


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
        repo = coordination_adapter._registered(project)
        infra_repo = coordination_adapter._registered("infra-quart")
    except ValueError:
        return tool_error(tool=tool, code="PROJECT_NOT_FOUND", message="Project is not registered.")
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
            "note": "Профиль не подготовлен. Список workflow не подтверждает способ выпуска.",
            "ready_for_release": "unknown",
        })
    if guide.repository != repo or guide.workflow.repository not in {repo, infra_repo}:
        return tool_error(
            tool=tool, code="SOURCE_HASH_MISMATCH", source="gitea",
            message="Репозиторий проекта не совпадает с описанием Infra.",
        )
    workflow_observation, *observations = await asyncio.gather(
        _observe_workflow(owner, guide.workflow),
        *(_observe(service, guide.compose_project) for service in guide.services),
    )
    services = [
        {**service.model_dump(), "observation": observation}
        for service, observation in zip(guide.services, observations, strict=True)
    ]
    return tool_success(tool, source="gitea", result={
        "project": project, "configured": True, "title": guide.title,
        "repository": f"{owner}/{repo}", "source": provenance,
        "workflow": {**guide.workflow.model_dump(), "observation": workflow_observation},
        "services": services,
        "steps": guide.steps,
        "conditions": [{**condition.model_dump(), "status": "unknown"} for condition in guide.conditions],
        "source_files": guide.source_files, "notes": guide.notes,
        "observed_at_utc": datetime.now(UTC).isoformat(),
        "ready_for_release": "unknown",
        "notice": (
            "Это подсказка, не разрешение на выпуск. Каталог взят из main Infra; "
            "фактическая версия Infra на сервере может отличаться. "
            "CI, откат и условия должны быть проверены отдельно. "
            "При изменении workflow необходимо перепроверить инструкцию перед выпуском."
        ),
    })


def register_all() -> None:
    register_tool("project_release_guide")(project_release_guide)