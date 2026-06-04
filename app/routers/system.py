"""System, server, snapshot, webhook, search, code intelligence, analytics, tree, and batch routes."""

import asyncio
import base64
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from app import state as _state
from app.api_help import build_api_help
from app.auth_middleware import (
    AuthIdentity,
    is_agent_token_valid,
    require_any_auth,
    require_master_key,
)
from app.config import settings
from app.metrics import metrics
from app.models import (
    AddServerRequest,
    BatchExecuteRequest,
    BatchExecuteResponse,
    BatchOperationResultResponse,
    CapabilitiesResponse,
    CodeCompleteRequest,
    CodeCompleteResponse,
    CodeGenerateRequest,
    CodeGenerateResponse,
    CodeInsertRequest,
    CodeInsertResponse,
    CodeInsertSuggestion,
    CodeSearchRequest,
    CodeSearchResponse,
    CodeSearchResultItem,
    CodeStats,
    ConnectServerRequest,
    CreateSnapshotRequest,
    CreateWebhookRequest,
    DependencyStats,
    DeployRequest,
    DeployResponse,
    FileStats,
    FileTreeNode,
    FileTreeRequest,
    FileTreeResponse,
    GitStats,
    GlobalReplaceRequest,
    GlobalReplaceResponse,
    GlobalSearchRequest,
    GlobalSearchResponse,
    HealthResponse,
    KnownHostAddRequest,
    KnownHostCheckResponse,
    KnownHostLookupResponse,
    ProjectAnalyticsRequest,
    ProjectAnalyticsResponse,
    ReplaceResultItem,
    RestoreSnapshotRequest,
    SearchMatchItem,
    ServerConnectResponse,
    ServerInfo,
    ServerListResponse,
    SnapshotActionResponse,
    SnapshotInfo,
    SnapshotListResponse,
    TestStats,
    WebhookConfigResponse,
    WebhookListResponse,
)
from app.security import validate_target_host
from app.server_manager import ServerStatus
from app.state import _err
from app.version import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[2]

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
        # Redis/Postgres are optional integrations for this alpha release,
        # so the service can be ready even when they are not configured.
        ready=True,
    )


@router.get("/api/capabilities", tags=["system"], response_model=CapabilitiesResponse)
async def get_capabilities():
    """Return API capabilities and environment information.

    Unauthenticated — used by agents to discover server settings.
    """
    servers = _state.server_manager.list_servers() if _state.server_manager else []
    server_count = len(servers)
    hint = ""
    if server_count == 0:
        hint = "No servers configured. Create one via POST /api/servers or connect directly with POST /api/ssh/connect"
    return CapabilitiesResponse(
        version=APP_VERSION,
        auth_mode="api_key" if settings.api_auth_enabled else "none",
        session_timeout=settings.session_timeout,
        cleanup_interval=settings.cleanup_interval,
        ssh_default_timeout=settings.ssh_default_timeout,
        max_sessions_per_ip=settings.max_sessions_per_ip,
        rate_limit_requests=settings.rate_limit_requests,
        rate_limit_window=settings.rate_limit_window,
        server_count=server_count,
        agent_token_enabled=bool(await is_agent_token_valid(settings, settings.agent_token, _state.agent_token_store)),
        agent_token_ttl=settings.agent_token_ttl,
        hint=hint,
    )


@router.get("/api/config", tags=["system"])
async def get_config(_identity: AuthIdentity = Depends(require_master_key)):
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
async def api_help(request: Request, _identity: AuthIdentity = Depends(require_any_auth)):
    """API reference: auth requirements, quick-start examples, and all endpoints.

    Accessible with any valid API key (master key or agent token).
    """
    return build_api_help(request)


@router.get("/metrics", tags=["system"], response_class=PlainTextResponse)
async def prometheus_metrics(_identity: AuthIdentity = Depends(require_master_key)):
    """Prometheus metrics endpoint."""
    return Response(content=metrics.get_metrics(), media_type="text/plain")


@router.get("/api/sdk/download", tags=["system"], response_class=PlainTextResponse)
async def download_sdk(_identity: AuthIdentity = Depends(require_master_key)):
    """Download Python SDK.

    Note: auth is handled by the global middleware.
    """
    sdk_path = PROJECT_ROOT / "sdk" / "ssh_gateway.py"
    if not sdk_path.exists():
        raise HTTPException(status_code=404, detail=_err(404, "SDK not found"))
    content = sdk_path.read_text()
    return Response(
        content=content,
        media_type="text/x-python",
        headers={
            "Content-Disposition": "attachment; filename=ssh_gateway.py"
        }
    )


@router.get("/api/circuit-breaker/stats", tags=["system"])
async def circuit_breaker_stats(_identity: AuthIdentity = Depends(require_master_key)):
    """Get circuit breaker statistics."""
    return await _state.circuit_breakers.get_all_stats()


@router.get("/", tags=["system"], response_class=HTMLResponse)
async def root():
    """Serve the web terminal UI.

    Protected by global auth middleware — requires a valid X-API-Key header
    (master key or agent token) when API auth is enabled.

    See GET /api/help for the REST API reference.
    """
    return FileResponse("app/static/index.html")


# ---------------------------------------------------------------------------
# Server Management
# ---------------------------------------------------------------------------


@router.get("/api/servers", tags=["servers"], response_model=ServerListResponse)
async def list_servers(_identity: AuthIdentity = Depends(require_master_key)):
    """List all configured servers."""
    servers = _state.server_manager.list_servers()
    return ServerListResponse(
        servers=[ServerInfo(**_state.server_manager.to_dict(s)) for s in servers],
        count=len(servers),
    )


@router.post("/api/servers", tags=["servers"])
async def add_server(req: AddServerRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
async def delete_server(server_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Remove a server."""
    if not _state.server_manager.get_server(server_id):
        raise HTTPException(status_code=404, detail=_err(404, f"Server {server_id} not found"))
    _state.server_manager.remove_server(server_id)
    return {"status": "removed", "server_id": server_id}


@router.post("/api/servers/{server_id}/connect", tags=["servers"], response_model=ServerConnectResponse)
async def connect_server(server_id: str, req: ConnectServerRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
        raise HTTPException(status_code=403, detail=_err(403, str(exc))) from exc

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
        raise HTTPException(status_code=502, detail=_err(502, f"Connection failed: {exc}")) from exc


# ---------------------------------------------------------------------------
# Snapshot System
# ---------------------------------------------------------------------------


@router.post("/api/snapshots", tags=["snapshots"], response_model=SnapshotActionResponse)
async def create_snapshot(req: CreateSnapshotRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
        raise HTTPException(status_code=500, detail=_err(500, f"Snapshot creation failed: {exc}")) from exc


@router.get("/api/snapshots", tags=["snapshots"])
async def list_snapshots(context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
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
async def restore_snapshot(req: RestoreSnapshotRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
        raise HTTPException(status_code=500, detail=_err(500, f"Restore failed: {exc}")) from exc


@router.delete("/api/snapshots/{snapshot_id}", tags=["snapshots"])
async def delete_snapshot(snapshot_id: str, context_id: str, _identity: AuthIdentity = Depends(require_master_key)):
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
async def create_webhook(req: CreateWebhookRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
async def list_webhooks(_identity: AuthIdentity = Depends(require_master_key)):
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
async def delete_webhook(webhook_id: str, _identity: AuthIdentity = Depends(require_master_key)):
    """Delete a webhook."""
    success = _state.webhook_manager.remove_webhook(webhook_id)
    return {"status": "deleted" if success else "not_found", "webhook_id": webhook_id}


@router.post("/api/webhooks/{webhook_id}/deploy", tags=["webhooks"], response_model=DeployResponse)
async def deploy_webhook(webhook_id: str, req: DeployRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
async def webhook_deployments(webhook_id: str, _identity: AuthIdentity = Depends(require_master_key)):
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
async def global_search(req: GlobalSearchRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
async def global_replace(req: GlobalReplaceRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
async def code_search(req: CodeSearchRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
async def code_insert(req: CodeInsertRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
async def code_generate(req: CodeGenerateRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
async def code_complete(req: CodeCompleteRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
async def run_analytics(req: ProjectAnalyticsRequest, _identity: AuthIdentity = Depends(require_master_key)):
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


@router.post("/api/tree", tags=["files"], response_model=FileTreeResponse)
async def get_file_tree_v2(req: FileTreeRequest, _identity: AuthIdentity = Depends(require_master_key)):
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
        root=FileTreeNode(**_state.file_tree.node_to_dict(tree)),
        total_files=total_files,
        total_directories=total_dirs,
    )


# ---------------------------------------------------------------------------
# Batch Execute
# ---------------------------------------------------------------------------


@router.post("/api/batch/execute", tags=["files"], response_model=BatchExecuteResponse)
async def batch_execute(req: BatchExecuteRequest, request: Request, _identity: AuthIdentity = Depends(require_master_key)):
    """Execute multiple file operations in a single transaction."""
    
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
async def list_known_hosts(_identity: AuthIdentity = Depends(require_master_key)):
    entries = await _state.host_key_store.list_keys()
    return {"hosts": entries}


@router.get("/api/known-hosts/check", tags=["known-hosts"])
async def check_known_host(
    host: str = Query(..., min_length=1),
    port: int = Query(22, ge=1, le=65535),
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Preflight trust check — returns 'known' or 'unknown'. Never returns 'changed'.

    IMPORTANT: lookups are by (host,port) pair — not by host alone.
    'changed' cannot be detected without a real SSH handshake.
    """
    entry = await _state.host_key_store.get_host(host, port)
    return KnownHostCheckResponse(
        status="known" if entry else "unknown",
        host=host,
        port=port,
    )


@router.get("/api/known-hosts/{host}", tags=["known-hosts"])
async def lookup_known_host(
    host: str,
    port: int = Query(22, ge=1, le=65535),
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Lookup a single host:port entry. Returns 404 if not found.

    IMPORTANT: lookups are by (host,port) pair — not by host alone.
    """
    entry = await _state.host_key_store.get_host(host, port)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Host {host}:{port} not found in known-hosts")
    return KnownHostLookupResponse(**entry)


@router.delete("/api/known-hosts/{host}", tags=["known-hosts"])
async def delete_known_host(
    host: str,
    port: int = Query(22, ge=1, le=65535),
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Delete a specific host:port entry from known hosts.

    IMPORTANT: deletes by (host,port) pair — use port=22 if not specified.
    """
    count = await _state.host_key_store.delete_host(host, port)
    if count == 0:
        raise HTTPException(status_code=404, detail=f"No known hosts found for {host}:{port}")
    return {"deleted": count, "host": host, "port": port}


@router.delete("/api/known-hosts", tags=["known-hosts"])
async def clear_known_hosts(_identity: AuthIdentity = Depends(require_master_key)):
    count = await _state.host_key_store.delete_all()
    return {"deleted": count}


@router.post("/api/known-hosts", tags=["known-hosts"])
async def add_known_host(
    req: KnownHostAddRequest,
    _identity: AuthIdentity = Depends(require_master_key),
):
    """Add a host:port to known-hosts by fetching its key via ssh-keyscan.

    The gateway must have network access to the target host.
    Supports RSA, ECDSA, and Ed25519 key types.
    """
    proc = await asyncio.create_subprocess_exec(
        "ssh-keyscan", "-T", "5", "-t", "rsa,ecdsa,ed25519",
        "-p", str(req.port), req.host,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail=f"ssh-keyscan failed for {req.host}:{req.port}: {stderr.decode().strip()}",
        )
    output = stdout.decode().strip()
    if not output:
        raise HTTPException(
            status_code=502,
            detail=f"ssh-keyscan returned no keys for {req.host}:{req.port}",
        )
    import paramiko
    added = 0
    errors = []
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and not parts[0].startswith("#"):
            try:
                pkey = paramiko.RSAKey(data=base64.b64decode(parts[2]))
                await _state.host_key_store.store(req.host, req.port, pkey)
                added += 1
            except paramiko.SSHException:
                try:
                    pkey = paramiko.Ed25519Key(data=base64.b64decode(parts[2]))
                    await _state.host_key_store.store(req.host, req.port, pkey)
                    added += 1
                except paramiko.SSHException:
                    try:
                        pkey = paramiko.ECDSAKey(data=base64.b64decode(parts[2]))
                        await _state.host_key_store.store(req.host, req.port, pkey)
                        added += 1
                    except Exception as e:
                        errors.append(str(e)[:100])
            except Exception as e:
                errors.append(str(e)[:100])
    if not added:
        raise HTTPException(
            status_code=502,
            detail=f"Could not parse any host key from {req.host}:{req.port}: {'; '.join(errors)}",
        )
    return {
        "status": "added",
        "host": req.host,
        "port": req.port,
        "keys_added": added,
    }
