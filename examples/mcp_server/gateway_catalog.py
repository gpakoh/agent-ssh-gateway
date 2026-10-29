"""Small, grouped navigation over the actual live tool registry."""

from __future__ import annotations

from typing import Any

from tool_scopes import get_required_scopes

from app.skill_pack import skill_metadata, snapshot, validate_role

GROUPS = {
    "discovery": ("Начало работы", "Здоровье Gateway, каталог инструментов и загрузка skills."),
    "projects": ("Проекты", "Поиск проектов, возможности рабочей среды и её состояние."),
    "workspace": ("Код и файлы", "Чтение, поиск, изменения, тесты и проверка качества кода."),
    "repositories": ("Git и CI", "Ветки, коммиты, Issues, PR и запуски Gitea/GitHub."),
    "coordination": (
        "Задачи и координация",
        "Доска, обсуждения, принятие, отчёт и закрытие Issue.",
    ),
    "agents": ("Агенты", "Поручения, запуск, состояние, результаты и восстановление worker."),
    "execution": ("SSH и команды", "Сессии, разрешённые команды и фоновые jobs."),
    "docker": ("Контейнеры", "Docker/Compose: диагностика и защищённые операции."),
    "database": ("База данных", "Разрешённые операции PostgreSQL."),
    "documentation": ("Документация", "Поиск библиотек и чтение технической документации."),
    "sites": ("Сайты и дизайн", "Ассеты, предпросмотр, доступность и анимация сайтов."),
    "other": ("Дополнительные возможности", "Остальные зарегистрированные инструменты."),
}


def group_for(name: str) -> str:
    if name == "site_asset_transfer":
        return "sites"
    if name in {"health", "tools_manifest", "gateway_catalog", "gateway_skill", "self_test"}:
        return "discovery"
    if name.startswith("coord_"):
        return "coordination"
    if name.startswith(("git_", "gitea_", "github_")) or name in {
        "commit_head",
        "current_branch",
        "recent_commits",
        "remotes",
    }:
        return "repositories"
    if (
        "agent" in name
        or name.startswith(("fleet_", "supervisor_", "candidate_"))
        or name
        in {"run_opencode", "prepare_candidate_clone", "write_handoff_plan", "show_handoff_status"}
    ):
        return "agents"
    if name.startswith("docker_") or name == "confirm_operation":
        return "docker"
    if name.startswith("postgres_"):
        return "database"
    if name in {"resolve_library_id", "query_docs"}:
        return "documentation"
    if name.startswith("project_") or name in {"info", "repo_status", "get_tree_sitter_languages"}:
        return "projects"
    if name.startswith(("execute", "session_", "job_", "scan_command")) or name in {
        "list_sessions",
        "connect",
        "disconnect",
        "wait_job",
        "list_servers",
        "switch_server",
    }:
        return "execution"
    if name.startswith(
        ("workspace_", "run_", "todo_", "read_", "list_", "search_", "find_", "get_", "scan_")
    ) or name in {
        "apply_patch",
        "preview_patch",
        "replace_text",
        "ast_search",
        "ast_replace",
    }:
        return "workspace"
    return "other"


def build_catalog(
    tools: list[Any],
    *,
    group: str | None = None,
    query: str = "",
    tool_name: str | None = None,
    include_schema: bool = False,
    role: str = "controller",
    offset: int = 0,
    limit: int = 20,
    unavailable: dict[str, str] | None = None,
) -> dict[str, Any]:
    validate_role(role)
    if group is not None and group not in GROUPS:
        raise ValueError("Unknown group; call gateway_catalog without filters")
    if type(query) is not str or len(query) > 100:
        raise ValueError("query must contain at most 100 characters")
    if type(offset) is not int or not 0 <= offset <= 10000:
        raise ValueError("offset must be an integer from 0 to 10000")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    if type(include_schema) is not bool or (include_schema and not tool_name):
        raise ValueError("include_schema requires one exact tool_name")
    if tool_name is not None and (not isinstance(tool_name, str) or len(tool_name) > 120):
        raise ValueError("Invalid tool_name")
    pack = snapshot()
    skills = skill_metadata(pack, role)
    unavailable = unavailable or {}
    rows = []
    counts = dict.fromkeys(GROUPS, 0)
    for tool in sorted(tools, key=lambda t: t.name):
        category = group_for(tool.name)
        counts[category] += 1
        summary = " ".join((tool.description or "").strip().splitlines()[:1])[:180]
        if group is not None and category != group:
            continue
        if tool_name is not None and tool.name != tool_name:
            continue
        if (
            query.casefold()
            not in (tool.name + " " + summary + " " + " ".join(GROUPS[category])).casefold()
        ):
            continue
        row = {
            "name": tool.name,
            "group": category,
            "summary": summary,
            "scopes": get_required_scopes(tool.name),
            "available": tool.name not in unavailable,
        }
        if tool.name in unavailable:
            row["unavailable_reason"] = unavailable[tool.name]
        if include_schema:
            row["input_schema"] = tool.parameters
        rows.append(row)
    filtered_skills = [
        s
        for s in skills
        if (
            (group is None or s["group"] == group)
            and query.casefold() in (s["name"] + " " + s["title"] + " " + s["summary"]).casefold()
            and tool_name is None
        )
    ]
    overview = group is None and not query and tool_name is None
    return {
        "source": "live-server-tool-registry",
        "tool_count": len(tools),
        "groups": [
            {
                "id": name,
                "title": title,
                "summary": summary,
                "tool_count": counts[name],
                "skill_count": sum(s["group"] == name for s in skills),
                "open": {"tool": "gateway_catalog", "arguments": {"group": name, "role": role}},
            }
            for name, (title, summary) in GROUPS.items()
            if counts[name] or any(s["group"] == name for s in skills)
        ]
        if overview
        else [],
        "skills": filtered_skills,
        "pack_sha256": pack["pack_sha256"],
        "tools": [] if overview else rows[offset : offset + limit],
        "filtered_tool_count": len(rows),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if not overview and offset + limit < len(rows) else None,
        "client_contract": "Navigation only. Client tools/list may still be flat; this does not change tool registration, permissions or client-side schema loading.",
        "next_step": "Open a group or search query; load one skill with gateway_skill. Request include_schema with one exact tool_name only when needed.",
    }
