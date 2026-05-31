"""System, server, snapshot, webhook, search, code intelligence, analytics, tree, and batch routes."""

import json
import os
import logging
import base64
import time
import uuid

from fastapi import APIRouter, Query, HTTPException, Request, Response, Header, UploadFile, File
from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse

from app import state as _state
from app.state import _err
from app.config import settings
from app.auth_middleware import is_agent_token_valid
from app.security import validate_target_host
from app.metrics import metrics
from app.server_manager import ServerManager, ServerStatus
from app.models import (
    HealthResponse,
    CapabilitiesResponse,
    ServerListResponse,
    ServerInfo,
    AddServerRequest,
    ConnectServerRequest,
    ServerConnectResponse,
    CreateSnapshotRequest,
    SnapshotActionResponse,
    SnapshotListResponse,
    SnapshotInfo,
    RestoreSnapshotRequest,
    CreateWebhookRequest,
    WebhookConfigResponse,
    WebhookListResponse,
    DeployRequest,
    DeployResponse,
    GlobalSearchRequest,
    GlobalSearchResponse,
    SearchMatchItem,
    GlobalReplaceRequest,
    GlobalReplaceResponse,
    ReplaceResultItem,
    CodeSearchRequest,
    CodeSearchResponse,
    CodeSearchResultItem,
    CodeInsertRequest,
    CodeInsertResponse,
    CodeInsertSuggestion,
    CodeGenerateRequest,
    CodeGenerateResponse,
    CodeCompleteRequest,
    CodeCompleteResponse,
    ProjectAnalyticsRequest,
    ProjectAnalyticsResponse,
    FileStats,
    CodeStats,
    GitStats,
    TestStats,
    DependencyStats,
    FileTreeRequest,
    FileTreeResponse,
    FileTreeNode,
    ProjectStructureRequest,
    ProjectStructureResponse,
    FileMetadata,
    BatchExecuteRequest,
    BatchExecuteResponse,
    BatchOperationResultResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Health & System
# ---------------------------------------------------------------------------


@router.get("/health", tags=["system"], response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    redis_ok = _state.redis_queue is not None and _state.redis_queue._redis is not None
    pg_ok = _state.session_store is not None
    return HealthResponse(
        status="ok" if redis_ok or not settings.redis_url else "degraded",
        redis=redis_ok,
        postgres=pg_ok,
        ready=redis_ok or pg_ok or True,
    )


@router.get("/api/capabilities", tags=["system"], response_model=CapabilitiesResponse)
async def get_capabilities():
    """Return API capabilities and environment information.

    Unauthenticated — used by agents to discover server settings.
    """
    servers = _state.server_manager.list_servers() if _state.server_manager else []
    return CapabilitiesResponse(
        version="4.5.1",
        auth_mode="api_key" if settings.api_auth_enabled else "none",
        session_timeout=settings.session_timeout,
        cleanup_interval=settings.cleanup_interval,
        ssh_default_timeout=settings.ssh_default_timeout,
        max_sessions_per_ip=settings.max_sessions_per_ip,
        rate_limit_requests=settings.rate_limit_requests,
        rate_limit_window=settings.rate_limit_window,
        server_count=len(servers),
        agent_token_enabled=bool(await is_agent_token_valid(settings, settings.agent_token, _state.agent_token_store)),
        agent_token_ttl=settings.agent_token_ttl,
    )


@router.get("/api/config", tags=["system"])
async def get_config():
    """Return runtime configuration (secrets masked)."""
    from app.config import settings
    return {
        "session_timeout": settings.session_timeout,
        "cleanup_interval": settings.cleanup_interval,
        "ssh_default_timeout": settings.ssh_default_timeout,
        "max_sessions_per_ip": settings.max_sessions_per_ip,
        "rate_limit_requests": settings.rate_limit_requests,
        "rate_limit_window": settings.rate_limit_window,
        "persistent_sessions_enabled": settings.persistent_sessions_enabled,
        "known_hosts_store": settings.known_hosts_store or "null",
        "api_auth_enabled": settings.api_auth_enabled,
        "agent_token_enabled": bool(settings.agent_token),
        "agent_token_ttl": settings.agent_token_ttl,
        "read_only": getattr(settings, "read_only", False),
    }


@router.get("/api/help", tags=["help"])
async def api_help(request: Request):
    """List all API endpoints grouped by tag for agent consumption."""
    openapi = request.app.openapi()
    paths = openapi.get("paths", {})
    schemas = openapi.get("components", {}).get("schemas", {})
    known_tags = {t["name"] for t in (request.app.openapi_tags or [])}
    groups: dict[str, list[dict]] = {}

    def _resolve(s: dict) -> dict:
        if "$ref" in s:
            name = s["$ref"].split("/")[-1]
            resolved = schemas.get(name, {})
            if resolved.get("properties"):
                return resolved
        return s

    for path, methods in paths.items():
        for method, details in methods.items():
            if method == "parameters":
                continue
            tags = details.get("tags", ["other"])
            tag = next((t for t in tags if t in known_tags), tags[0] if tags else "other")

            entry = {"method": method.upper(), "path": path, "summary": details.get("summary", "")}
            params = []

            for p in details.get("parameters", []):
                params.append(_clean_param(p.get("name", ""), p.get("in", "query"), p.get("schema", {}), p.get("required", False), p.get("description", "")))

            body = details.get("requestBody", {})
            if body:
                content = body.get("content", {})
                for media_type, media_body in content.items():
                    schema = _resolve(media_body.get("schema", {}))
                    props = schema.get("properties", {})
                    required_set = set(schema.get("required", []))
                    for pname, pdetails in props.items():
                        params.append(_clean_param(pname, "body", pdetails, pname in required_set, pdetails.get("description", "")))

            if params:
                entry["params"] = params
            groups.setdefault(tag, []).append(entry)

    return groups


def _clean_param(name: str, location: str, schema: dict, required: bool, description: str) -> dict:
    ptype = schema.get("type", "string")
    if not ptype and "$ref" in schema:
        ptype = schema["$ref"].split("/")[-1]
    if isinstance(ptype, list):
        ptype = ptype[0] if ptype else "string"
    p = {"name": name, "in": location, "type": ptype, "required": required}
    desc = (description or "").strip().split(".")[0].strip()
    if desc:
        p["desc"] = desc
    return p


@router.get("/metrics", tags=["system"], response_class=PlainTextResponse)
async def prometheus_metrics():
    """Prometheus metrics endpoint."""
    return Response(content=metrics.get_metrics(), media_type="text/plain")


@router.get("/api/sdk/download", tags=["system"], response_class=PlainTextResponse)
async def download_sdk():
    """Download Python SDK.

    Note: auth is handled by the global middleware.
    """
    sdk_path = "/app/sdk/ssh_gateway.py"
    try:
        with open(sdk_path, "r") as f:
            content = f.read()
        return Response(
            content=content,
            media_type="text/x-python",
            headers={
                "Content-Disposition": "attachment; filename=ssh_gateway.py"
            }
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=_err(404, "SDK not found"))


@router.get("/api/circuit-breaker/stats", tags=["system"])
async def circuit_breaker_stats():
    """Get circuit breaker statistics."""
    return await _state.circuit_breakers.get_all_stats()


@router.get("/", tags=["system"], response_class=HTMLResponse)
async def root():
    """Serve the main page."""
    return FileResponse("app/static/index.html")


# ---------------------------------------------------------------------------
# Server Management
# ---------------------------------------------------------------------------


@router.get("/api/servers", tags=["servers"], response_model=ServerListResponse)
async def list_servers():
    """List all configured servers."""
    servers = _state.server_manager.list_servers()
    return ServerListResponse(
        servers=[ServerInfo(**_state.server_manager.to_dict(s)) for s in servers],
        count=len(servers),
    )


@router.post("/api/servers", tags=["servers"])
async def add_server(req: AddServerRequest):
    """Add a new server."""
    existing = _state.server_manager.get_server(req.id)
    if existing:
        raise HTTPException(status_code=409, detail=_err(409, f"Server with ID '{req.id}' already exists"))
    
    server = _state.server_manager.add_server(
        server_id=req.id,
        name=req.name,
        host=req.host,
        port=req.port,
        username=req.username,
        description=req.description,
        tags=req.tags,
    )
    return _state.server_manager.to_dict(server)


@router.delete("/api/servers/{server_id}", tags=["servers"])
async def delete_server(server_id: str):
    """Remove a server."""
    if not _state.server_manager.get_server(server_id):
        raise HTTPException(status_code=404, detail=_err(404, f"Server {server_id} not found"))
    _state.server_manager.remove_server(server_id)
    return {"status": "removed", "server_id": server_id}


@router.post("/api/servers/{server_id}/connect", tags=["servers"], response_model=ServerConnectResponse)
async def connect_server(server_id: str, req: ConnectServerRequest):
    """Connect to a server and return session."""
    server = _state.server_manager.get_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail=_err(404, f"Server {server_id} not found"))
    
    try:
        validate_target_host(
            server.host,
            settings.allowed_target_cidrs,
            settings.denied_target_cidrs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=_err(403, str(exc)))

    try:
        _password = req.password.get_secret_value() if req.password else None
        _private_key = req.private_key.get_secret_value() if req.private_key else None
        session_id = await _state.manager.create_session(
            host=server.host,
            port=server.port,
            username=server.username,
            password=_password,
            private_key=_private_key,
        )
        
        _state.server_manager.update_server_status(
            server_id,
            ServerStatus.ONLINE,
            session_id=session_id,
        )
        
        return ServerConnectResponse(
            server_id=server_id,
            session_id=session_id,
            status="connected",
            message=f"Connected to {server.name}",
        )
    except Exception as exc:
        _state.server_manager.update_server_status(server_id, ServerStatus.ERROR)
        raise HTTPException(status_code=502, detail=_err(502, f"Connection failed: {exc}"))


# ---------------------------------------------------------------------------
# Snapshot System
# ---------------------------------------------------------------------------


@router.post("/api/snapshots", tags=["snapshots"], response_model=SnapshotActionResponse)
async def create_snapshot(req: CreateSnapshotRequest):
    """Create a snapshot of current project state."""
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))
    
    try:
        snapshot = await _state.snapshot_manager.create_snapshot(
            session_id=ctx.session_id,
            context_id=req.context_id,
            name=req.name,
            description=req.description,
        )
        
        return SnapshotActionResponse(
            success=True,
            message=f"Snapshot '{snapshot.name}' created with {len(snapshot.files)} files",
            snapshot_id=snapshot.id,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_err(500, f"Snapshot creation failed: {exc}"))


@router.get("/api/snapshots", tags=["snapshots"])
async def list_snapshots(context_id: str):
    """List all snapshots for context."""
    ctx = await _state.context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))
    
    snapshots = await _state.snapshot_manager.list_snapshots(ctx.session_id, context_id)
    
    return SnapshotListResponse(
        snapshots=[
            SnapshotInfo(
                id=s.id,
                name=s.name,
                context_id=s.context_id,
                created_at=s.created_at,
                files=s.files,
                description=s.description,
                git_commit_before=s.git_commit_before,
                size_bytes=s.size_bytes,
            )
            for s in snapshots
        ],
        count=len(snapshots),
    )


@router.post("/api/snapshots/restore", tags=["snapshots"], response_model=SnapshotActionResponse)
async def restore_snapshot(req: RestoreSnapshotRequest):
    """Restore project from snapshot."""
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))
    
    try:
        result = await _state.snapshot_manager.restore_snapshot(
            session_id=ctx.session_id,
            context_id=req.context_id,
            snapshot_id=req.snapshot_id,
        )
        
        return SnapshotActionResponse(
            success=result["success"],
            message=f"Restored {result['restored_files']} of {result['total_files']} files",
            snapshot_id=req.snapshot_id,
            restored_files=result["restored_files"],
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_err(500, f"Restore failed: {exc}"))


@router.delete("/api/snapshots/{snapshot_id}", tags=["snapshots"])
async def delete_snapshot(snapshot_id: str, context_id: str):
    """Delete a snapshot."""
    ctx = await _state.context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))
    
    success = await _state.snapshot_manager.delete_snapshot(
        session_id=ctx.session_id,
        context_id=context_id,
        snapshot_id=snapshot_id,
    )
    
    return {"status": "deleted" if success else "not_found", "snapshot_id": snapshot_id}


# ---------------------------------------------------------------------------
# CI/CD Webhooks
# ---------------------------------------------------------------------------


@router.post("/api/webhooks", tags=["webhooks"])
async def create_webhook(req: CreateWebhookRequest):
    """Create a new webhook for auto-deployment."""
    config = _state.webhook_manager.add_webhook(
        name=req.name,
        webhook_type=req.webhook_type,
        secret=req.secret,
        target_path=req.target_path,
        deploy_command=req.deploy_command,
        context_id=req.context_id,
        notify_url=req.notify_url,
    )
    
    return WebhookConfigResponse(
        id=config.id,
        name=config.name,
        webhook_type=config.webhook_type.value,
        target_path=config.target_path,
        deploy_command=config.deploy_command,
        context_id=config.context_id,
        notify_url=config.notify_url,
        enabled=config.enabled,
    )


@router.get("/api/webhooks", tags=["webhooks"], response_model=WebhookListResponse)
async def list_webhooks():
    """List all webhooks."""
    configs = _state.webhook_manager.list_webhooks()
    return WebhookListResponse(
        webhooks=[
            WebhookConfigResponse(
                id=c.id,
                name=c.name,
                webhook_type=c.webhook_type.value,
                target_path=c.target_path,
                deploy_command=c.deploy_command,
                context_id=c.context_id,
                notify_url=c.notify_url,
                enabled=c.enabled,
            )
            for c in configs
        ],
        count=len(configs),
    )


@router.delete("/api/webhooks/{webhook_id}", tags=["webhooks"])
async def delete_webhook(webhook_id: str):
    """Delete a webhook."""
    success = _state.webhook_manager.remove_webhook(webhook_id)
    return {"status": "deleted" if success else "not_found", "webhook_id": webhook_id}


@router.post("/api/webhooks/{webhook_id}/deploy", tags=["webhooks"], response_model=DeployResponse)
async def deploy_webhook(webhook_id: str, req: DeployRequest):
    """Manually trigger deployment."""
    result = await _state.webhook_manager.execute_deploy(
        session_id=req.session_id,
        webhook_id=webhook_id,
    )
    
    return DeployResponse(
        status=result["status"],
        job_id=result.get("job_id"),
        message=result.get("message", ""),
    )


@router.get("/api/webhooks/{webhook_id}/deployments", tags=["webhooks"])
async def webhook_deployments(webhook_id: str):
    """List deployment history."""
    deployments = _state.webhook_manager.get_deployments(webhook_id)
    return {
        "deployments": deployments,
        "count": len(deployments),
    }


# ---------------------------------------------------------------------------
# Global Search & Replace
# ---------------------------------------------------------------------------


@router.post("/api/search/global", tags=["code"], response_model=GlobalSearchResponse)
async def global_search(req: GlobalSearchRequest):
    """Search across all project files."""
    matches = await _state.search_replace.search(
        session_id=req.session_id,
        path=req.path,
        query=req.query,
        file_pattern=req.file_pattern,
        use_regex=req.use_regex,
        case_sensitive=req.case_sensitive,
        context_lines=req.context_lines,
    )
    
    files_affected = list(set(m.path for m in matches))
    
    return GlobalSearchResponse(
        query=req.query,
        matches=[
            SearchMatchItem(
                path=m.path,
                line=m.line,
                column=m.column,
                content=m.content,
            )
            for m in matches
        ],
        total_count=len(matches),
        files_affected=files_affected,
    )


@router.post("/api/replace/global", tags=["code"], response_model=GlobalReplaceResponse)
async def global_replace(req: GlobalReplaceRequest):
    """Replace across all project files."""
    results = await _state.search_replace.replace(
        session_id=req.session_id,
        path=req.path,
        search_query=req.search,
        replace_with=req.replace,
        file_pattern=req.file_pattern,
        use_regex=req.use_regex,
        case_sensitive=req.case_sensitive,
        dry_run=req.dry_run,
    )
    
    total_replacements = sum(r.replacements_count for r in results)
    files_modified = sum(1 for r in results if r.replacements_count > 0)
    
    git_commit = None
    if not req.dry_run and req.auto_commit and req.context_id and files_modified > 0:
        commit_result = await _state.context_manager.commit_changes(
            req.context_id,
            f"Global replace: '{req.search}' -> '{req.replace}'",
            [r.path for r in results if r.replacements_count > 0]
        )
        if commit_result.get("success"):
            git_commit = commit_result.get("hash")
    
    return GlobalReplaceResponse(
        search=req.search,
        replace=req.replace,
        results=[
            ReplaceResultItem(
                path=r.path,
                replacements_count=r.replacements_count,
                success=r.success,
                error=r.error,
            )
            for r in results
        ],
        total_replacements=total_replacements,
        files_modified=files_modified,
        dry_run=req.dry_run,
        git_commit=git_commit,
    )


# ---------------------------------------------------------------------------
# Code Intelligence
# ---------------------------------------------------------------------------


@router.post("/api/code/search", tags=["code"], response_model=CodeSearchResponse)
async def code_search(req: CodeSearchRequest):
    """Search for code pattern in project."""
    results = await _state.code_intelligence.search_code(
        session_id=req.session_id,
        path=req.path,
        query=req.query,
        language=req.language,
        context_lines=req.context_lines,
    )
    
    return CodeSearchResponse(
        query=req.query,
        results=[
            CodeSearchResultItem(
                path=r.path,
                line=r.line,
                column=r.column,
                content=r.content,
            )
            for r in results
        ],
        count=len(results),
    )


@router.post("/api/code/insert", tags=["code"], response_model=CodeInsertResponse)
async def code_insert(req: CodeInsertRequest):
    """Intelligently insert code based on natural language instruction."""
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))
    
    suggestion = await _state.code_intelligence.find_insertion_point(
        session_id=ctx.session_id,
        path=req.path,
        instruction=req.instruction,
        language=req.language,
    )
    
    if not suggestion:
        raise HTTPException(status_code=400, detail=_err(400, "Could not find insertion point"))
    
    try:
        result = await _state.file_editor.edit_file(
            ctx.session_id,
            req.path,
            [{"type": "insert_after", "after": suggestion.insert_after, "text": suggestion.code}],
        )
        
        git_commit = None
        if req.auto_commit and result.get("success"):
            commit_result = await _state.context_manager.commit_changes(
                req.context_id,
                f"AI: {req.instruction}",
                [req.path]
            )
            if commit_result.get("success"):
                git_commit = commit_result.get("hash")
        
        return CodeInsertResponse(
            success=result.get("success", False),
            path=req.path,
            suggestion=CodeInsertSuggestion(
                insert_after=suggestion.insert_after,
                code=suggestion.code,
                explanation=suggestion.explanation,
                line_number=suggestion.line_number,
            ),
            applied=result.get("success", False),
            git_commit=git_commit,
        )
    except Exception as exc:
        logger.error("Code insertion failed: %s", exc)
        return CodeInsertResponse(
            success=False,
            path=req.path,
            suggestion=CodeInsertSuggestion(
                insert_after=suggestion.insert_after,
                code=suggestion.code,
                explanation=suggestion.explanation,
                line_number=suggestion.line_number,
            ),
            applied=False,
        )


@router.post("/api/code/generate", tags=["code"], response_model=CodeGenerateResponse)
async def code_generate(req: CodeGenerateRequest):
    """Generate code based on natural language instruction."""
    code = await _state.code_intelligence.generate_code(
        session_id="",
        instruction=req.instruction,
        language=req.language,
    )
    
    return CodeGenerateResponse(
        code=code,
        language=req.language,
        explanation=f"Generated code for: {req.instruction}",
    )


@router.post("/api/code/complete", tags=["code"], response_model=CodeCompleteResponse)
async def code_complete(req: CodeCompleteRequest):
    """Suggest code completion."""
    completion = await _state.code_intelligence.suggest_completion(
        session_id=req.session_id,
        path=req.path,
        partial_code=req.partial_code,
        language=req.language,
    )
    
    return CodeCompleteResponse(
        completion=completion,
        context=req.partial_code[-100:] if len(req.partial_code) > 100 else req.partial_code,
    )


# ---------------------------------------------------------------------------
# Analytics & File Tree
# ---------------------------------------------------------------------------


@router.post("/api/analytics", tags=["code"], response_model=ProjectAnalyticsResponse)
async def run_analytics(req: ProjectAnalyticsRequest):
    """Analyze project and return metrics."""
    metrics_data = await _state.analytics.analyze_project(
        session_id=req.session_id,
        path=req.path,
    )
    
    return ProjectAnalyticsResponse(
        project_path=metrics_data["project_path"],
        files=FileStats(**metrics_data["files"]),
        code=CodeStats(**metrics_data["code"]),
        git=GitStats(**metrics_data["git"]),
        tests=TestStats(**metrics_data["tests"]),
        dependencies=DependencyStats(**metrics_data["dependencies"]),
    )


@router.get("/api/project/tree", tags=["code"])
async def get_file_tree(
    session_id: str = Query(...),
    path: str = Query(default="."),
    max_depth: int = Query(default=3, ge=1, le=10),
):
    """Simple project tree — list files and directories.

    Returns flat list with type, path, size for quick introspection.
    """
    cmd = f"cd '{path}' && find . -maxdepth {max_depth} -not -path '*/\\.*' -not -path '*/node_modules/*' -not -path '*/__pycache__/*' -not -path '*/venv/*' -printf '%y|%p|%s\\n' 2>/dev/null || echo 'ERROR'"
    result = await _state.manager.execute(session_id, cmd, timeout=30)

    if result["exit_code"] != 0 or "ERROR" in result["stdout"]:
        raise HTTPException(status_code=500, detail=_err(500, f"Cannot read directory: {result['stderr']}"))

    items = []
    for line in result["stdout"].strip().split("\n"):
        if not line or line == "ERROR":
            continue
        parts = line.split("|", 3)
        if len(parts) < 3:
            continue

        ftype, fpath, fsize = parts
        fpath = fpath.lstrip("./")
        if not fpath:
            continue

        items.append({
            "type": "directory" if ftype == "d" else "file",
            "path": fpath,
            "size": int(fsize) if fsize and ftype == "f" else None,
        })

    return {"items": items, "count": len(items)}


@router.post("/api/tree", tags=["files"], response_model=FileTreeResponse)
async def get_file_tree_v2(req: FileTreeRequest):
    """Get directory tree structure."""
    tree = await _state.file_tree.get_tree(
        session_id=req.session_id,
        path=req.path,
        depth=req.depth,
        show_hidden=req.show_hidden,
        max_files=req.max_files,
    )
    
    def count_files(node) -> tuple[int, int]:
        files = 0
        dirs = 0
        if node.type == "file":
            files = 1
        elif node.type == "directory":
            dirs = 1
            for child in node.children:
                f, d = count_files(child)
                files += f
                dirs += d
        return files, dirs
    
    total_files, total_dirs = count_files(tree)
    
    return FileTreeResponse(
        root=_state.file_tree.node_to_dict(tree),
        total_files=total_files,
        total_directories=total_dirs,
    )


# ---------------------------------------------------------------------------
# Batch Execute
# ---------------------------------------------------------------------------


@router.post("/api/batch/execute", tags=["files"], response_model=BatchExecuteResponse)
async def batch_execute(req: BatchExecuteRequest, request: Request):
    """Execute multiple file operations in a single transaction."""
    import uuid
    
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))

    operations = []
    for op in req.operations:
        op_dict = {
            "type": op.type,
            "path": op.path,
            "continue_on_error": op.continue_on_error,
        }
        if op.operations:
            op_dict["operations"] = [o.model_dump() for o in op.operations]
        if op.content:
            op_dict["content"] = op.content
        if op.new_path:
            op_dict["new_path"] = op.new_path
        if op.dest_path:
            op_dict["dest_path"] = op.dest_path
        if op.command:
            op_dict["command"] = op.command
        operations.append(op_dict)

    result = await _state.batch_manager.execute_batch(
        session_id=ctx.session_id,
        context_id=req.context_id,
        operations=operations,
        auto_commit=req.auto_commit,
        commit_message=req.commit_message,
        run_validation=req.run_validation,
        transaction_id=str(uuid.uuid4())[:8],
    )

    return BatchExecuteResponse(
        transaction_id=result.transaction_id,
        overall_success=result.overall_success,
        summary=result.summary,
        total_duration=result.total_duration,
        operations=[
            BatchOperationResultResponse(
                operation=op.operation,
                path=op.path,
                success=op.success,
                output=op.output,
                error=op.error,
                duration=op.duration,
                lines_changed=op.lines_changed,
            )
            for op in result.operations
        ],
        git_commit=result.git_commit,
        validation_result=result.validation_result,
    )


# ---------------------------------------------------------------------------
# Known Hosts
# ---------------------------------------------------------------------------


@router.get("/api/known-hosts", tags=["known-hosts"])
async def list_known_hosts():
    entries = await _state.host_key_store.list_keys()
    return {"hosts": entries}


@router.delete("/api/known-hosts/{host}", tags=["known-hosts"])
async def delete_known_host(host: str):
    count = await _state.host_key_store.delete_host(host)
    if count == 0:
        raise HTTPException(status_code=404, detail=f"No known hosts found for {host}")
    return {"deleted": count}


@router.delete("/api/known-hosts", tags=["known-hosts"])
async def clear_known_hosts():
    count = await _state.host_key_store.delete_all()
    return {"deleted": count}
