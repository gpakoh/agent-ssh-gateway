"""FastAPI entry point for Web SSH Gateway."""

import json
import logging
import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Header, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse

from app.config import settings
from app.models import (
    ConnectRequest,
    ConnectResponse,
    ExecuteRequest,
    ExecuteResponse,
    DisconnectRequest,
    DisconnectResponse,
    SessionsResponse,
    SessionInfo,
    HealthResponse,
    ErrorResponse,
    JobRunRequest,
    JobRunResponse,
    JobStatusResponse,
    JobResultResponse,
    JobListResponse,
    FileEditRequest,
    FileEditResponse,
    FileReadRequest,
    FileReadResponse,
    PatchApplyRequest,
    PatchApplyResponse,
    ContextCreateRequest,
    ContextResponse,
    ContextListResponse,
    GitInfoResponse,
    GitInitRequest,
    GitCommitRequest,
    GitActionResponse,
    FileEditWithContextRequest,
    FileEditWithContextResponse,
    ValidateRequest,
    ValidationReportResponse,
    ValidationStepResult,
    SmartContextStateResponse,
    TabStateResponse,
    OpenFileRequest,
    CloseFileRequest,
    UpdateCursorRequest,
    AddCommandRequest,
    AddSearchRequest,
    AddBookmarkRequest,
    RemoveBookmarkRequest,
    BatchExecuteRequest,
    BatchExecuteResponse,
    BatchOperationResultResponse,
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
    CreateBackupRequest,
    RestoreBackupRequest,
    RecoveryActionResponse,
    BackupInfo,
    ListBackupsResponse,
    TemplateListResponse,
    TemplateInfo,
    TemplateGetRequest,
    TemplateRenderRequest,
    TemplateRenderResponse,
    DiffResponse,
    DiffLine,
    ProjectAnalyticsRequest,
    ProjectAnalyticsResponse,
    FileStats,
    CodeStats,
    GitStats,
    TestStats,
    DependencyStats,
    GlobalSearchRequest,
    GlobalSearchResponse,
    SearchMatchItem,
    GlobalReplaceRequest,
    GlobalReplaceResponse,
    ReplaceResultItem,
    FileTreeRequest,
    FileTreeResponse,
    FileTreeNode,
    ServerInfo,
    ServerListResponse,
    AddServerRequest,
    ConnectServerRequest,
    ServerConnectResponse,
    PTYCreateRequest,
    PTYInputRequest,
    PTYOutputResponse,
    PTYCloseRequest,
    SnapshotInfo,
    CreateSnapshotRequest,
    RestoreSnapshotRequest,
    SnapshotListResponse,
    SnapshotActionResponse,
    WebhookConfigResponse,
    WebhookListResponse,
    CreateWebhookRequest,
    DeployRequest,
    DeployResponse,
    DeploymentInfo,
)
from app.ssh_manager import (
    SSHSessionManager,
    SSHManagerError,
    ConnectionError,
    AuthenticationError,
    SessionNotFoundError,
    TimeoutError,
    ExecutionError,
)
from app.job_manager import JobManager
from app.file_editor import FileEditor
from app.context_manager import ContextManager
from app.git_manager import GitStatus
from app.batch_operations import BatchOperationsManager
from app.code_intelligence import CodeIntelligence
from app.template_library import TemplateLibrary
from app.diff_generator import DiffGenerator
from app.project_analytics import ProjectAnalytics
from app.search_replace import GlobalSearchReplace
from app.file_tree import FileTreeExplorer
from app.server_manager import ServerManager, ServerStatus
from app.snapshot_manager import SnapshotManager
from app.webhook_manager import WebhookManager, WebhookType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

manager: SSHSessionManager
job_manager: JobManager
file_editor: FileEditor
context_manager: ContextManager
batch_manager: BatchOperationsManager
code_intelligence: CodeIntelligence
search_replace: GlobalSearchReplace
file_tree: FileTreeExplorer
server_manager: ServerManager
snapshot_manager: SnapshotManager
webhook_manager: WebhookManager
analytics: ProjectAnalytics


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    global manager, job_manager, file_editor, context_manager, batch_manager, code_intelligence, search_replace, file_tree, server_manager, snapshot_manager, webhook_manager, analytics
    manager = SSHSessionManager(
        session_timeout=settings.session_timeout,
        cleanup_interval=settings.cleanup_interval,
    )
    await manager.start_cleanup_task()

    job_manager = JobManager(ssh_manager=manager)
    await job_manager.start_cleanup_task()

    file_editor = FileEditor(ssh_manager=manager)
    
    context_manager = ContextManager(ssh_manager=manager)
    await context_manager.start_cleanup_task()
    
    batch_manager = BatchOperationsManager(
        ssh_manager=manager,
        file_editor=file_editor,
        context_manager=context_manager,
    )
    
    code_intelligence = CodeIntelligence(
        ssh_manager=manager,
        file_editor=file_editor,
    )
    
    search_replace = GlobalSearchReplace(
        ssh_manager=manager,
        file_editor=file_editor,
    )
    
    file_tree = FileTreeExplorer(ssh_manager=manager)
    
    server_manager = ServerManager()
    
    snapshot_manager = SnapshotManager(
        ssh_manager=manager,
        context_manager=context_manager,
    )
    
    webhook_manager = WebhookManager(
        ssh_manager=manager,
        job_manager=job_manager,
    )
    
    analytics = ProjectAnalytics(ssh_manager=manager)

    logger.info("Web SSH Gateway started on %s:%d", settings.uvicorn_host, settings.uvicorn_port)
    yield
    await context_manager.stop_cleanup_task()
    await job_manager.stop_cleanup_task()
    await manager.stop_cleanup_task()
    await manager.close_all()
    logger.info("Web SSH Gateway shutdown complete")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Web SSH Gateway",
    description="Execute SSH commands through a web browser",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(SSHManagerError)
async def ssh_exception_handler(request, exc: SSHManagerError):
    """Convert SSH manager exceptions to HTTP responses."""
    status_map = {
        ConnectionError: 502,
        AuthenticationError: 401,
        SessionNotFoundError: 404,
        TimeoutError: 504,
        ExecutionError: 500,
    }
    status_code = status_map.get(type(exc), 500)
    raise HTTPException(status_code=status_code, detail=str(exc))


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return HealthResponse(status="ok")


@app.post("/api/ssh/connect", response_model=ConnectResponse)
async def ssh_connect(req: ConnectRequest):
    """Create a new SSH session."""
    session_id = await manager.create_session(
        host=req.host,
        port=req.port,
        username=req.username,
        password=req.password,
        private_key=req.private_key,
        key_passphrase=req.key_passphrase,
    )
    return ConnectResponse(session_id=session_id)


@app.post("/api/ssh/execute", response_model=ExecuteResponse)
async def ssh_execute(req: ExecuteRequest):
    """Execute a command on an existing SSH session."""
    result = await manager.execute(
        session_id=req.session_id,
        command=req.command,
        timeout=req.timeout,
    )
    return ExecuteResponse(**result)


@app.post("/api/ssh/disconnect", response_model=DisconnectResponse)
async def ssh_disconnect(req: DisconnectRequest):
    """Close an SSH session."""
    await manager.disconnect(req.session_id)
    return DisconnectResponse()


@app.get("/api/ssh/sessions", response_model=SessionsResponse)
async def ssh_sessions():
    """List all active SSH sessions."""
    records = await manager.list_sessions()
    sessions = [
        SessionInfo(
            session_id=r.session_id,
            host=r.host,
            port=r.port,
            username=r.username,
            connected_at=time_to_iso(r.connected_at),
            last_activity=time_to_iso(r.last_activity),
        )
        for r in records
    ]
    return SessionsResponse(sessions=sessions, count=len(sessions))


# ---------------------------------------------------------------------------
# PTY (Interactive Terminal)
# ---------------------------------------------------------------------------

# Store PTY sessions
_pty_sessions: dict[str, dict] = {}

@app.post("/api/ssh/pty/{session_id}/create")
async def pty_create(session_id: str, req: PTYCreateRequest):
    """Create PTY session."""
    import uuid
    import paramiko
    
    record = await manager.get_session(session_id)
    if not record:
        raise HTTPException(status_code=404, detail="Session not found")
    
    pty_id = str(uuid.uuid4())
    _pty_sessions[pty_id] = {
        "session_id": session_id,
        "client": record.client,
        "channel": None,
        "term": req.term,
        "rows": req.rows,
        "cols": req.cols,
    }
    
    # Create interactive channel
    try:
        transport = record.client.get_transport()
        channel = transport.open_session()
        channel.get_pty(term=req.term, width=req.cols, height=req.rows)
        channel.invoke_shell()
        
        _pty_sessions[pty_id]["channel"] = channel
        
        return {
            "status": "created",
            "pty_id": pty_id,
            "message": "PTY session created",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PTY creation failed: {exc}")


@app.post("/api/ssh/pty/{session_id}/input")
async def pty_input(session_id: str, req: PTYInputRequest):
    """Send input to PTY."""
    # Find PTY by session_id
    pty_info = None
    pty_id = None
    for pid, info in _pty_sessions.items():
        if info["session_id"] == session_id:
            pty_info = info
            pty_id = pid
            break
    
    if not pty_info or not pty_info.get("channel"):
        raise HTTPException(status_code=404, detail="PTY session not found")
    
    try:
        channel = pty_info["channel"]
        channel.send(req.data)
        
        return {"status": "sent", "pty_id": pty_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Input failed: {exc}")


@app.get("/api/ssh/pty/{session_id}/output")
async def pty_output(session_id: str):
    """Get PTY output."""
    pty_info = None
    for info in _pty_sessions.values():
        if info["session_id"] == session_id:
            pty_info = info
            break
    
    if not pty_info or not pty_info.get("channel"):
        raise HTTPException(status_code=404, detail="PTY session not found")
    
    try:
        channel = pty_info["channel"]
        output = ""
        
        # Read available output (non-blocking)
        import select
        if channel.recv_ready():
            data = channel.recv(4096).decode("utf-8", errors="replace")
            output += data
        
        return PTYOutputResponse(
            output=output,
            eof=channel.eof_received or channel.closed,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Output read failed: {exc}")


@app.post("/api/ssh/pty/{session_id}/close")
async def pty_close(session_id: str):
    """Close PTY session."""
    pty_id_to_remove = None
    for pid, info in list(_pty_sessions.items()):
        if info["session_id"] == session_id:
            if info.get("channel"):
                try:
                    info["channel"].close()
                except Exception:
                    pass
            pty_id_to_remove = pid
            break
    
    if pty_id_to_remove:
        del _pty_sessions[pty_id_to_remove]
        return {"status": "closed", "session_id": session_id}
    
    raise HTTPException(status_code=404, detail="PTY session not found")


# ---------------------------------------------------------------------------
# Heartbeat / Keepalive
# ---------------------------------------------------------------------------

@app.post("/api/ssh/heartbeat")
async def ssh_heartbeat(req: DisconnectRequest):
    """Refresh session timeout by touching it."""
    record = await manager.get_session(req.session_id)
    if not record:
        raise SessionNotFoundError(f"Session {req.session_id} not found")
    record.touch()
    return {"status": "ok", "session_id": req.session_id, "idle_time": record.idle_time}


# ---------------------------------------------------------------------------
# Background Jobs API
# ---------------------------------------------------------------------------

@app.post("/api/jobs/run", response_model=JobRunResponse)
async def jobs_run(req: JobRunRequest):
    """Start a background job on an SSH session."""
    job_id = await job_manager.create_job(
        session_id=req.session_id,
        command=req.command,
    )
    return JobRunResponse(job_id=job_id)


@app.get("/api/jobs/{job_id}/status", response_model=JobStatusResponse)
async def jobs_status(job_id: str):
    """Get job status."""
    status = await job_manager.get_job_status(job_id)
    return JobStatusResponse(**status)


@app.get("/api/jobs/{job_id}/result", response_model=JobResultResponse)
async def jobs_result(job_id: str):
    """Get full job result."""
    result = await job_manager.get_job_result(job_id)
    return JobResultResponse(**result)


@app.get("/api/jobs", response_model=JobListResponse)
async def jobs_list(
    session_id: Optional[str] = None,
    status: Optional[str] = None,
):
    """List background jobs."""
    jobs = await job_manager.list_jobs(session_id=session_id, status=status)
    return JobListResponse(
        jobs=[JobResultResponse(**j.to_dict()) for j in jobs],
        count=len(jobs),
    )


@app.post("/api/jobs/{job_id}/cancel")
async def jobs_cancel(job_id: str):
    """Cancel a running job."""
    await job_manager.cancel_job(job_id)
    return {"status": "cancelled", "job_id": job_id}


# ---------------------------------------------------------------------------
# Job Stream (SSE)
# ---------------------------------------------------------------------------

@app.get("/api/jobs/{job_id}/stream")
async def jobs_stream(job_id: str):
    """Stream job output via Server-Sent Events."""
    job = await job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    queue: asyncio.Queue = asyncio.Queue()
    job.add_listener(queue)

    async def event_generator():
        try:
            # Send initial status
            yield f"data: {json.dumps({'type': 'status', 'status': job.status})}\n\n"

            # Send buffered output if job already completed
            if job.stdout:
                yield f"data: {json.dumps({'type': 'stdout', 'data': job.stdout})}\n\n"
            if job.stderr:
                yield f"data: {json.dumps({'type': 'stderr', 'data': job.stderr})}\n\n"

            # Stream new events
            while job.status in ("pending", "running"):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive comment
                    yield ":keepalive\n\n"
                    continue

            # Send final status
            yield f"data: {json.dumps({'type': 'status', 'status': job.status, 'exit_code': job.exit_code})}\n\n"
        finally:
            job.remove_listener(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# File Edit API
# ---------------------------------------------------------------------------

@app.post("/api/file/read", response_model=FileReadResponse)
async def file_read(req: FileReadRequest):
    """Read a file from a remote server."""
    content = await file_editor.read_file(req.session_id, req.path)
    return FileReadResponse(path=req.path, content=content)


@app.patch("/api/file/edit", response_model=FileEditResponse)
async def file_edit(req: FileEditRequest):
    """Edit a remote file using patch operations."""
    result = await file_editor.edit_file(
        req.session_id,
        req.path,
        [op.model_dump() for op in req.operations],
    )
    return FileEditResponse(**result)


@app.post("/api/file/patch", response_model=PatchApplyResponse)
async def file_patch(req: PatchApplyRequest):
    """Apply a unified diff patch."""
    result = await file_editor.apply_patch(
        req.session_id,
        req.patch,
        req.strip,
    )
    return PatchApplyResponse(**result)


# ---------------------------------------------------------------------------
# Raw File API
# ---------------------------------------------------------------------------

@app.get("/api/file/raw")
async def file_raw(
    session_id: str = Query(...),
    path: str = Query(...),
    offset: int = Query(0, ge=0),
    limit: int = Query(0, ge=0),
    range_header: Optional[str] = Header(None, alias="range"),
):
    """Read a remote file and return raw content as text/plain.
    
    Supports Range header (bytes=start-end) or offset/limit query params.
    """
    content = await file_editor.read_file(session_id, path)
    
    # Handle Range header
    if range_header and range_header.startswith("bytes="):
        try:
            range_str = range_header[6:]  # Remove "bytes="
            start, end = range_str.split("-")
            start = int(start) if start else 0
            end = int(end) if end else len(content)
            content = content[start:end]
            return Response(
                content=content,
                media_type="text/plain",
                status_code=206,
                headers={
                    "Content-Range": f"bytes {start}-{end-1}/{len(content)}",
                    "Accept-Ranges": "bytes",
                },
            )
        except (ValueError, IndexError):
            pass
    
    # Handle offset/limit
    if offset > 0 or limit > 0:
        start = offset
        end = offset + limit if limit > 0 else len(content)
        content = content[start:end]
    
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Accept-Ranges": "bytes"},
    )


# ---------------------------------------------------------------------------
# Batch File Read API
# ---------------------------------------------------------------------------

class BatchReadRequest(BaseModel):
    """Request to read multiple files."""
    session_id: str = Field(..., min_length=1)
    paths: list[str] = Field(..., min_length=1, max_length=20)


class BatchReadResponse(BaseModel):
    """Response with multiple file contents."""
    files: dict[str, str]
    errors: dict[str, str]


@app.post("/api/batch/read", response_model=BatchReadResponse)
async def batch_read(req: BatchReadRequest):
    """Read multiple files in a single request."""
    files = {}
    errors = {}
    
    for path in req.paths:
        try:
            content = await file_editor.read_file(req.session_id, path)
            files[path] = content
        except Exception as exc:
            errors[path] = str(exc)
    
    return BatchReadResponse(files=files, errors=errors)


# ---------------------------------------------------------------------------
# WebSocket streaming
# ---------------------------------------------------------------------------

@app.websocket("/api/ssh/execute/stream")
async def ssh_execute_stream(websocket: WebSocket):
    """Execute a command and stream output via WebSocket."""
    await websocket.accept()
    try:
        # First message must contain session_id and command
        data = await websocket.receive_json()
        session_id = data.get("session_id", "")
        command = data.get("command", "")

        if not session_id or not command:
            await websocket.send_json({"type": "error", "data": "session_id and command are required"})
            await websocket.close()
            return

        async for msg_type, msg_data in manager.execute_stream(session_id, command):
            await websocket.send_json({"type": msg_type, "data": msg_data})

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
        try:
            await websocket.send_json({"type": "error", "data": str(exc)})
        except Exception:
            pass
        await websocket.close()


# ---------------------------------------------------------------------------
# File Watch WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/api/file/watch")
async def file_watch_stream(websocket: WebSocket):
    """Watch file changes in real-time via WebSocket.
    
    Usage:
    1. Connect to /api/file/watch
    2. Send: {"session_id": "...", "path": "/var/log/app.log", "tail": true}
    3. Receive file updates as they happen
    """
    await websocket.accept()
    session_id = None
    watch_task = None
    
    try:
        # Get initial config
        data = await websocket.receive_json()
        session_id = data.get("session_id", "")
        path = data.get("path", "")
        tail = data.get("tail", True)
        interval = data.get("interval", 1.0)  # polling interval
        
        if not session_id or not path:
            await websocket.send_json({"type": "error", "data": "session_id and path required"})
            await websocket.close()
            return
        
        # Verify session
        record = await manager.get_session(session_id)
        if not record:
            await websocket.send_json({"type": "error", "data": "Session not found"})
            await websocket.close()
            return
        
        last_content = ""
        last_size = 0
        
        await websocket.send_json({
            "type": "status",
            "data": f"Watching {path} (tail={tail})"
        })
        
        while True:
            try:
                # Check for client commands
                try:
                    msg = await asyncio.wait_for(websocket.receive_json(), timeout=interval)
                    if msg.get("action") == "stop":
                        break
                except asyncio.TimeoutError:
                    pass
                
                # Read file
                result = await manager.execute(
                    session_id,
                    f"cat '{path}' 2>/dev/null || echo '__FILE_NOT_FOUND__'",
                    timeout=10
                )
                
                if "__FILE_NOT_FOUND__" in result["stdout"]:
                    await websocket.send_json({
                        "type": "error",
                        "data": f"File not found: {path}"
                    })
                    await asyncio.sleep(interval)
                    continue
                
                content = result["stdout"]
                
                if tail:
                    # Only send new content
                    if len(content) > last_size:
                        new_content = content[last_size:]
                        lines = new_content.strip().split("\n")
                        for line in lines:
                            if line:
                                await websocket.send_json({
                                    "type": "line",
                                    "data": line,
                                    "timestamp": time.time()
                                })
                        last_size = len(content)
                else:
                    # Send full content if changed
                    if content != last_content:
                        await websocket.send_json({
                            "type": "content",
                            "data": content,
                            "timestamp": time.time()
                        })
                        last_content = content
                
            except Exception as exc:
                logger.error("File watch error: %s", exc)
                await websocket.send_json({"type": "error", "data": str(exc)})
                await asyncio.sleep(interval)
                
    except WebSocketDisconnect:
        logger.info("File watch client disconnected")
    except Exception as exc:
        logger.error("File watch error: %s", exc)
    finally:
        if watch_task:
            watch_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Context Management API
# ---------------------------------------------------------------------------

@app.post("/api/context/create", response_model=ContextResponse)
async def context_create(req: ContextCreateRequest):
    """Create a new development context with git awareness."""
    ctx = await context_manager.create_context(
        session_id=req.session_id,
        name=req.name,
        path=req.path,
        branch=req.branch,
        auto_commit=req.auto_commit,
        auto_validate=req.auto_validate,
    )

    git_info = ctx.git_info
    message = git_info.message if git_info else "Context created"

    # Add suggestion if git not initialized
    if git_info and git_info.status == GitStatus.NOT_INITIALIZED:
        message += "\n💡 Tip: Use POST /api/git/init to initialize git repository."

    resp = _context_to_response(ctx)
    resp.message = message
    return resp


@app.get("/api/context/list", response_model=ContextListResponse)
async def context_list(session_id: Optional[str] = None):
    """List all active contexts."""
    contexts = []
    for ctx_id, ctx in context_manager._contexts.items():
        if ctx and (not session_id or ctx.session_id == session_id):
            resp = _context_to_response(ctx)
            resp.message = f"Idle for {ctx.idle_time:.0f}s"
            contexts.append(resp)

    return ContextListResponse(contexts=contexts, count=len(contexts))


@app.get("/api/context/{context_id}", response_model=ContextResponse)
async def context_get(context_id: str):
    """Get context details."""
    ctx = await context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=f"Context {context_id} not found")

    resp = _context_to_response(ctx)
    resp.message = "Context active"
    return resp



@app.delete("/api/context/bookmark")
async def context_remove_bookmark(
    context_id: str = Query(...),
    path: str = Query(...),
    line: int = Query(...),
):
    """Remove bookmark."""
    success = await context_manager.remove_bookmark(context_id, path, line)
    return {"status": "removed" if success else "not_found", "path": path, "line": line}
@app.delete("/api/context/{context_id}")
async def context_delete(context_id: str):
    """Delete a context."""
    success = await context_manager.delete_context(context_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Context {context_id} not found")
    return {"status": "deleted", "context_id": context_id}


# ---------------------------------------------------------------------------
# Git API
# ---------------------------------------------------------------------------

@app.post("/api/git/init", response_model=GitActionResponse)
async def git_init(req: GitInitRequest):
    """Initialize git repository for context."""
    result = await context_manager.init_git(req.context_id, req.remote_url)
    return GitActionResponse(**result)


@app.post("/api/git/commit", response_model=GitActionResponse)
async def git_commit(req: GitCommitRequest):
    """Create a git commit for context."""
    result = await context_manager.commit_changes(
        req.context_id,
        req.message,
        req.files
    )
    return GitActionResponse(**result)


@app.post("/api/git/backup", response_model=GitActionResponse)
async def git_backup(context_id: str, backup_name: str = "auto_backup"):
    """Create a git stash backup."""
    result = await context_manager.create_backup(context_id, backup_name)
    return GitActionResponse(**result)


@app.post("/api/git/restore", response_model=GitActionResponse)
async def git_restore(context_id: str):
    """Restore from stash."""
    result = await context_manager.restore_backup(context_id)
    return GitActionResponse(**result)


@app.get("/api/git/diff")
async def git_diff(context_id: str):
    """Get git diff for context."""
    ctx = await context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=f"Context {context_id} not found")

    from app.git_manager import GitManager
    git = GitManager(manager)
    diff = await git.diff(ctx.session_id, ctx.path)
    return {"context_id": context_id, "diff": diff}


@app.post("/api/git/status")
async def git_status(context_id: str):
    """Refresh git status for context."""
    git_info = await context_manager.update_git_status(context_id)
    return GitInfoResponse(
        status=git_info.status.value,
        branch=git_info.branch,
        has_changes=git_info.has_changes,
        last_commit=git_info.last_commit,
        remote_url=git_info.remote_url,
        message=git_info.message,
        can_commit=git_info.can_commit,
    )


# ---------------------------------------------------------------------------
# Error Recovery API
# ---------------------------------------------------------------------------

@app.post("/api/recovery/backup", response_model=RecoveryActionResponse)
async def recovery_create_backup(req: CreateBackupRequest):
    """Create a backup before making changes."""
    ctx = await context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    
    # Create git stash as backup
    result = await context_manager.create_backup(req.context_id, req.name)
    
    return RecoveryActionResponse(
        success=result.get("success", False),
        message=result.get("message", ""),
        backup_id=req.name,
    )


@app.post("/api/recovery/restore", response_model=RecoveryActionResponse)
async def recovery_restore_backup(req: RestoreBackupRequest):
    """Restore from backup."""
    ctx = await context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    
    # Restore git stash
    result = await context_manager.restore_backup(req.context_id)
    
    return RecoveryActionResponse(
        success=result.get("success", False),
        message=result.get("message", ""),
        restored_files=["all_stashed_files"],
    )


@app.get("/api/recovery/backups")
async def recovery_list_backups(context_id: str):
    """List available backups."""
    ctx = await context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    
    # List git stashes
    result = await manager.execute(
        ctx.session_id,
        f"cd {ctx.path} && git stash list",
        timeout=10
    )
    
    backups = []
    for line in result["stdout"].strip().split("\n"):
        if line:
            # Parse: stash@{0}: On branch: message
            parts = line.split(": ", 1)
            if len(parts) >= 2:
                stash_id = parts[0].strip()
                message = parts[1].strip()
                backups.append(BackupInfo(
                    id=stash_id,
                    name=message,
                    created_at=time.time(),
                ))
    
    return ListBackupsResponse(backups=backups, count=len(backups))


# ---------------------------------------------------------------------------
# Context-aware File Operations
# ---------------------------------------------------------------------------

@app.post("/api/context/file/read", response_model=FileReadResponse)
async def context_file_read(req: FileReadRequest):
    """Read a file using context (session_id extracted from context)."""
    ctx = await context_manager.get_context(req.session_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")

    content = await file_editor.read_file(ctx.session_id, req.path)
    await context_manager.add_file_to_context(req.session_id, req.path)
    return FileReadResponse(path=req.path, content=content)


@app.patch("/api/context/file/edit", response_model=FileEditWithContextResponse)
async def context_file_edit(req: FileEditWithContextRequest):
    """Edit a file with context awareness (auto-commit, validation)."""
    ctx = await context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")

    # Create automatic backup before editing (if git is initialized)
    if ctx.git_info and ctx.git_info.status.value != "not_initialized":
        try:
            await context_manager.create_backup(
                req.context_id,
                f"before_edit_{req.path.replace('/', '_')}"
            )
        except Exception as exc:
            logger.warning("Auto-backup failed: %s", exc)

    # Perform edit
    result = await file_editor.edit_file(
        ctx.session_id,
        req.path,
        [op.model_dump() for op in req.operations],
    )

    await context_manager.record_edit(req.context_id, req.path, "edit")
    await context_manager.add_file_to_context(req.context_id, req.path)

    response = FileEditWithContextResponse(
        success=result.get("success", True),
        path=req.path,
        operations_applied=result.get("operations_applied", 0),
        changed=result.get("changed", False),
    )

    # Generate diff if file was changed
    if result.get("changed", False):
        try:
            # Read old content from git
            git_result = await manager.execute(
                ctx.session_id,
                f"cd {ctx.path} && git show HEAD:{req.path} 2>/dev/null || echo ''",
                timeout=10
            )
            old_content = git_result["stdout"]
            
            # Read new content
            new_content = await file_editor.read_file(ctx.session_id, req.path)
            
            # Generate diff
            unified_diff = DiffGenerator.generate_unified_diff(
                old_content, new_content, req.path, req.path
            )
            inline_diff = DiffGenerator.generate_inline_diff(old_content, new_content)
            changes = DiffGenerator.count_changes(unified_diff)
            
            response.diff = DiffResponse(
                unified_diff=unified_diff,
                inline_diff=[DiffLine(**line) for line in inline_diff],
                changes=changes,
                old_path=req.path,
                new_path=req.path,
            )
        except Exception as exc:
            logger.warning("Diff generation failed: %s", exc)

    # Auto-commit if enabled
    if ctx.auto_commit and result.get("changed", False):
        commit_msg = req.commit_message or f"Update {req.path}"
        commit_result = await context_manager.commit_changes(
            req.context_id,
            commit_msg,
            [req.path]
        )
        if commit_result["success"]:
            response.git_commit = commit_result.get("hash")

    # Validation if requested or auto_validate enabled
    if req.run_validation or ctx.auto_validate:
        try:
            report = await context_manager.validate_context(req.context_id)
            response.validation_result = ValidationReportResponse(
                overall_status=report.overall_status.value,
                summary=report.summary,
                total_duration=report.total_duration,
                can_commit=report.can_commit,
                steps=[
                    ValidationStepResult(
                        name=step.name,
                        status=step.status.value,
                        output=step.output,
                        errors=step.errors,
                        warnings=step.warnings,
                        duration=step.duration,
                    )
                    for step in report.steps
                ]
            )
            
            # If validation failed and auto_commit is on, rollback commit
            if not report.can_commit and ctx.auto_commit:
                response.warning = "⚠️ Валидация не пройдена, коммит отменён"
                response.git_commit = None
                # TODO: Actually revert the commit if needed
        except Exception as exc:
            logger.error("Validation error: %s", exc)
            response.validation_result = ValidationReportResponse(
                overall_status="error",
                summary=f"Ошибка валидации: {exc}",
                total_duration=0,
                can_commit=False,
                steps=[]
            )

    # Warning if git not initialized
    if ctx.git_info and ctx.git_info.status == GitStatus.NOT_INITIALIZED:
        response.warning = "⚠️ Проект не в Git. Используйте POST /api/git/init для инициализации."

    return response


# ---------------------------------------------------------------------------
# Validation API
# ---------------------------------------------------------------------------

@app.post("/api/validate", response_model=ValidationReportResponse)
async def validate_context(req: ValidateRequest):
    """Run validation pipeline (mypy + pytest) for context."""
    try:
        report = await context_manager.validate_context(
            req.context_id,
            run_mypy=req.run_mypy,
            run_tests=req.run_tests,
        )
        
        return ValidationReportResponse(
            overall_status=report.overall_status.value,
            summary=report.summary,
            total_duration=report.total_duration,
            can_commit=report.can_commit,
            steps=[
                ValidationStepResult(
                    name=step.name,
                    status=step.status.value,
                    output=step.output,
                    errors=step.errors,
                    warnings=step.warnings,
                    duration=step.duration,
                )
                for step in report.steps
            ]
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error("Validation error: %s", exc)
        raise HTTPException(status_code=500, detail=f"Validation failed: {exc}")


# ---------------------------------------------------------------------------
# Template Library API
# ---------------------------------------------------------------------------

@app.get("/api/templates", response_model=TemplateListResponse)
async def list_templates():
    """List all available code templates."""
    templates = TemplateLibrary.list_templates()
    return TemplateListResponse(
        templates=[TemplateInfo(**t) for t in templates],
        count=len(templates)
    )


@app.get("/api/templates/{template_id}")
async def get_template(template_id: str):
    """Get template details."""
    template = TemplateLibrary.get_template(template_id)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
    return template


@app.post("/api/templates/render", response_model=TemplateRenderResponse)
async def render_template(req: TemplateRenderRequest):
    """Render template and save to file."""
    ctx = await context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    
    try:
        code = TemplateLibrary.render_template(req.template_id, req.params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    
    if not code:
        raise HTTPException(status_code=404, detail=f"Template {req.template_id} not found")
    
    # Create file with rendered code
    result = await manager.execute(
        ctx.session_id,
        f"cat > '{req.target_path}' << 'TEMPLATE_EOF'\n{code}\nTEMPLATE_EOF",
        timeout=10
    )
    
    if result["exit_code"] != 0:
        raise HTTPException(status_code=500, detail=f"Failed to create file: {result['stderr']}")
    
    # Auto-commit if enabled
    git_commit = None
    if req.auto_commit:
        commit_result = await context_manager.commit_changes(
            req.context_id,
            f"Add {req.template_id} template",
            [req.target_path]
        )
        if commit_result.get("success"):
            git_commit = commit_result.get("hash")
    
    return TemplateRenderResponse(
        success=True,
        template_id=req.template_id,
        target_path=req.target_path,
        code=code,
        git_commit=git_commit,
    )


# ---------------------------------------------------------------------------
# Global Search & Replace API
# ---------------------------------------------------------------------------

@app.post("/api/search/global", response_model=GlobalSearchResponse)
async def global_search(req: GlobalSearchRequest):
    """Search across all project files."""
    matches = await search_replace.search(
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


@app.post("/api/replace/global", response_model=GlobalReplaceResponse)
async def global_replace(req: GlobalReplaceRequest):
    """Replace across all project files."""
    results = await search_replace.replace(
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
        commit_result = await context_manager.commit_changes(
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
# File Tree Explorer API
# ---------------------------------------------------------------------------

@app.post("/api/tree", response_model=FileTreeResponse)
async def get_file_tree(req: FileTreeRequest):
    """Get directory tree structure."""
    tree = await file_tree.get_tree(
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
        root=file_tree.node_to_dict(tree),
        total_files=total_files,
        total_directories=total_dirs,
    )


# ---------------------------------------------------------------------------
# Server Management API
# ---------------------------------------------------------------------------

@app.get("/api/servers", response_model=ServerListResponse)
async def list_servers():
    """List all configured servers."""
    servers = server_manager.list_servers()
    return ServerListResponse(
        servers=[ServerInfo(**server_manager.to_dict(s)) for s in servers],
        count=len(servers),
    )


@app.post("/api/servers")
async def add_server(req: AddServerRequest):
    """Add a new server."""
    # Check if server ID already exists
    existing = server_manager.get_server(req.id)
    if existing:
        raise HTTPException(status_code=409, detail=f"Server with ID '{req.id}' already exists")
    
    server = server_manager.add_server(
        server_id=req.id,
        name=req.name,
        host=req.host,
        port=req.port,
        username=req.username,
        description=req.description,
        tags=req.tags,
    )
    return server_manager.to_dict(server)


@app.delete("/api/servers/{server_id}")
async def remove_server(server_id: str):
    """Remove a server."""
    success = server_manager.remove_server(server_id)
    return {"status": "removed" if success else "not_found", "server_id": server_id}


@app.post("/api/servers/{server_id}/connect", response_model=ServerConnectResponse)
async def connect_server(server_id: str, req: ConnectServerRequest):
    """Connect to a server and return session."""
    server = server_manager.get_server(server_id)
    if not server:
        raise HTTPException(status_code=404, detail=f"Server {server_id} not found")
    
    try:
        session_id = await manager.create_session(
            host=server.host,
            port=server.port,
            username=server.username,
            password=req.password,
            private_key=req.private_key,
        )
        
        server_manager.update_server_status(
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
        server_manager.update_server_status(server_id, ServerStatus.ERROR)
        raise HTTPException(status_code=502, detail=f"Connection failed: {exc}")


# ---------------------------------------------------------------------------
# Snapshot System API
# ---------------------------------------------------------------------------

@app.post("/api/snapshots", response_model=SnapshotActionResponse)
async def create_snapshot(req: CreateSnapshotRequest):
    """Create a snapshot of current project state."""
    ctx = await context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    
    try:
        snapshot = await snapshot_manager.create_snapshot(
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
        raise HTTPException(status_code=500, detail=f"Snapshot creation failed: {exc}")


@app.get("/api/snapshots")
async def list_snapshots(context_id: str):
    """List all snapshots for context."""
    ctx = await context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    
    snapshots = await snapshot_manager.list_snapshots(ctx.session_id, context_id)
    
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


@app.post("/api/snapshots/restore", response_model=SnapshotActionResponse)
async def restore_snapshot(req: RestoreSnapshotRequest):
    """Restore project from snapshot."""
    ctx = await context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    
    try:
        result = await snapshot_manager.restore_snapshot(
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
        raise HTTPException(status_code=500, detail=f"Restore failed: {exc}")


@app.delete("/api/snapshots/{snapshot_id}")
async def delete_snapshot(snapshot_id: str, context_id: str):
    """Delete a snapshot."""
    ctx = await context_manager.get_context(context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    
    success = await snapshot_manager.delete_snapshot(
        session_id=ctx.session_id,
        context_id=context_id,
        snapshot_id=snapshot_id,
    )
    
    return {"status": "deleted" if success else "not_found", "snapshot_id": snapshot_id}


# ---------------------------------------------------------------------------
# CI/CD Webhook API
# ---------------------------------------------------------------------------

@app.post("/api/webhooks")
async def create_webhook(req: CreateWebhookRequest):
    """Create a new webhook for auto-deployment."""
    config = webhook_manager.add_webhook(
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


@app.get("/api/webhooks", response_model=WebhookListResponse)
async def list_webhooks():
    """List all webhooks."""
    configs = webhook_manager.list_webhooks()
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


@app.delete("/api/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: str):
    """Delete a webhook."""
    success = webhook_manager.remove_webhook(webhook_id)
    return {"status": "deleted" if success else "not_found", "webhook_id": webhook_id}


@app.post("/api/webhooks/{webhook_id}/deploy", response_model=DeployResponse)
async def trigger_deploy(webhook_id: str, req: DeployRequest):
    """Manually trigger deployment."""
    result = await webhook_manager.execute_deploy(
        session_id=req.session_id,
        webhook_id=webhook_id,
    )
    
    return DeployResponse(
        status=result["status"],
        job_id=result.get("job_id"),
        message=result.get("message", ""),
    )


@app.get("/api/webhooks/{webhook_id}/deployments")
async def list_deployments(webhook_id: str):
    """List deployment history."""
    deployments = webhook_manager.get_deployments(webhook_id)
    return {
        "deployments": deployments,
        "count": len(deployments),
    }


# ---------------------------------------------------------------------------
# Smart Context API
# ---------------------------------------------------------------------------

@app.post("/api/context/file/open")
async def context_file_open(req: OpenFileRequest):
    """Open file in smart context (creates tab)."""
    await context_manager.add_file_to_context(req.context_id, req.path)
    return {"status": "opened", "path": req.path}


@app.post("/api/context/file/close")
async def context_file_close(req: CloseFileRequest):
    """Close file in smart context (closes tab)."""
    success = await context_manager.close_file(req.context_id, req.path)
    return {"status": "closed" if success else "not_found", "path": req.path}


@app.post("/api/context/cursor")
async def context_update_cursor(req: UpdateCursorRequest):
    """Update cursor position in file."""
    await context_manager.update_cursor(req.context_id, req.path, req.line, req.column)
    return {"status": "updated", "path": req.path, "line": req.line, "column": req.column}


@app.post("/api/context/command")
async def context_add_command(req: AddCommandRequest):
    """Add command to history."""
    result = await context_manager.add_command(req.context_id, req.command, req.directory)
    return {"status": "added", "command": result}


@app.post("/api/context/search")
async def context_add_search(req: AddSearchRequest):
    """Add search query to history."""
    result = await context_manager.add_search(req.context_id, req.query, req.path, req.replace_with)
    return {"status": "added", "search": result}


@app.post("/api/context/bookmark")
async def context_add_bookmark(req: AddBookmarkRequest):
    """Add bookmark."""
    result = await context_manager.add_bookmark(req.context_id, req.path, req.line, req.note)
    return {"status": "added", "bookmark": result}



@app.get("/api/context/{context_id}/state")
async def context_get_state(context_id: str):
    """Get smart context state."""
    state = await context_manager.get_smart_state(context_id)
    if not state:
        raise HTTPException(status_code=404, detail=f"Context {context_id} not found")
    return state


# ---------------------------------------------------------------------------
# Project Analytics API
# ---------------------------------------------------------------------------

@app.post("/api/analytics", response_model=ProjectAnalyticsResponse)
async def get_project_analytics(req: ProjectAnalyticsRequest):
    """Analyze project and return metrics."""
    metrics = await analytics.analyze_project(
        session_id=req.session_id,
        path=req.path,
    )
    
    return ProjectAnalyticsResponse(
        project_path=metrics["project_path"],
        files=FileStats(**metrics["files"]),
        code=CodeStats(**metrics["code"]),
        git=GitStats(**metrics["git"]),
        tests=TestStats(**metrics["tests"]),
        dependencies=DependencyStats(**metrics["dependencies"]),
    )


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
# Batch Operations API
# ---------------------------------------------------------------------------

@app.post("/api/batch/execute", response_model=BatchExecuteResponse)
async def batch_execute(req: BatchExecuteRequest):
    """Execute multiple file operations in a single transaction."""
    import uuid
    
    ctx = await context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")

    # Convert Pydantic models to dicts for batch manager
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

    result = await batch_manager.execute_batch(
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
# Code Intelligence API
# ---------------------------------------------------------------------------

@app.post("/api/code/search", response_model=CodeSearchResponse)
async def code_search(req: CodeSearchRequest):
    """Search for code pattern in project."""
    results = await code_intelligence.search_code(
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


@app.post("/api/code/insert", response_model=CodeInsertResponse)
async def code_insert(req: CodeInsertRequest):
    """Intelligently insert code based on natural language instruction."""
    ctx = await context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail="Context not found")
    
    # Find insertion point
    suggestion = await code_intelligence.find_insertion_point(
        session_id=ctx.session_id,
        path=req.path,
        instruction=req.instruction,
        language=req.language,
    )
    
    if not suggestion:
        raise HTTPException(status_code=400, detail="Could not find insertion point")
    
    # Apply the insertion
    try:
        result = await file_editor.edit_file(
            ctx.session_id,
            req.path,
            [{"type": "insert_after", "after": suggestion.insert_after, "text": suggestion.code}],
        )
        
        git_commit = None
        if req.auto_commit and result.get("success"):
            commit_result = await context_manager.commit_changes(
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


@app.post("/api/code/generate", response_model=CodeGenerateResponse)
async def code_generate(req: CodeGenerateRequest):
    """Generate code based on natural language instruction."""
    code = await code_intelligence.generate_code(
        session_id="",  # Not needed for generation
        instruction=req.instruction,
        language=req.language,
    )
    
    return CodeGenerateResponse(
        code=code,
        language=req.language,
        explanation=f"Generated code for: {req.instruction}",
    )


@app.post("/api/code/complete", response_model=CodeCompleteResponse)
async def code_complete(req: CodeCompleteRequest):
    """Suggest code completion."""
    completion = await code_intelligence.suggest_completion(
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
# Static files
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
async def root():
    """Serve the main page."""
    return FileResponse("app/static/index.html")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from datetime import datetime, timezone


def time_to_iso(timestamp: float) -> str:
    """Convert Unix timestamp to ISO format."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _context_to_response(ctx) -> ContextResponse:
    """Helper to convert Context to ContextResponse."""
    git_info = ctx.git_info
    smart_state = ctx.smart_state.to_dict()
    return ContextResponse(
        context_id=ctx.context_id,
        name=ctx.name,
        path=ctx.path,
        session_id=ctx.session_id,
        branch=ctx.branch,
        git=GitInfoResponse(
            status=git_info.status.value if git_info else "unknown",
            branch=git_info.branch if git_info else None,
            has_changes=git_info.has_changes if git_info else False,
            last_commit=git_info.last_commit if git_info else None,
            remote_url=git_info.remote_url if git_info else None,
            message=git_info.message if git_info else "",
            can_commit=git_info.can_commit if git_info else False,
        ),
        auto_commit=ctx.auto_commit,
        auto_validate=ctx.auto_validate,
        files_opened=ctx.files_opened,
        smart_state=SmartContextStateResponse(
            tabs=[TabStateResponse(**tab) for tab in smart_state["tabs"]],
            active_tab=smart_state.get("active_tab"),
            command_history=smart_state.get("command_history", []),
            search_history=smart_state.get("search_history", []),
            bookmarks=smart_state.get("bookmarks", []),
            last_edited_file=smart_state.get("last_edited_file"),
            clipboard_size=smart_state.get("clipboard_size", 0),
        ),
        created_at=ctx.created_at,
        message="",
    )
