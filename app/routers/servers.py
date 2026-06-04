"""Server inventory and connection routes."""

from fastapi import APIRouter, Depends, HTTPException

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.config import settings
from app.models import (
    AddServerRequest,
    ConnectServerRequest,
    ServerConnectResponse,
    ServerInfo,
    ServerListResponse,
)
from app.security import validate_target_host
from app.server_manager import ServerStatus
from app.state import _err

router = APIRouter()


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
async def connect_server(
    server_id: str,
    req: ConnectServerRequest,
    _identity: AuthIdentity = Depends(require_master_key),
):
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
