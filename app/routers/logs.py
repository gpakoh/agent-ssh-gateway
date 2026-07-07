"""Remote log reading via SSH — systemd journal and Docker container logs."""

import logging
import shlex

from fastapi import APIRouter, Depends, HTTPException, Query

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.security import sanitize_command
from app.state import _err

logger = logging.getLogger(__name__)

router = APIRouter(tags=["logs"])


def _check_session(session_id: str) -> None:
    if session_id not in _state.manager._sessions:
        raise HTTPException(status_code=404, detail=_err(404, f"Session not found: {session_id}"))


@router.get("/api/logs/journal")
async def journal_logs(
    _identity: AuthIdentity = Depends(require_master_key),
    session_id: str = Query(..., description="Active SSH session ID"),
    unit: str = Query(None, description="systemd unit name (e.g. nginx, sshd)"),
    lines: int = Query(50, ge=1, le=5000, description="Number of lines to fetch"),
    priority: str = Query(
        None, description="Priority filter: emerg, alert, crit, err, warning, notice, info, debug"
    ),
    since: str = Query(None, description="Time range: '1h', '30m', '2025-01-01'"),
):
    """Read systemd journal logs from a remote server."""
    _check_session(session_id)

    cmd = ["journalctl", "--no-pager", "-n", str(lines)]
    if unit:
        cmd.extend(["-u", shlex.quote(unit)])
    if priority:
        cmd.extend(["-p", shlex.quote(priority)])
    if since:
        cmd.extend(["--since", shlex.quote(since)])

    command = " ".join(cmd)
    try:
        command = sanitize_command(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc

    result = await _state.manager.execute(session_id=session_id, command=command, timeout=30)
    return result


@router.get("/api/logs/docker")
async def docker_logs(
    _identity: AuthIdentity = Depends(require_master_key),
    session_id: str = Query(..., description="Active SSH session ID"),
    container: str = Query(..., description="Container name or ID"),
    lines: int = Query(100, ge=1, le=5000, description="Number of lines to fetch"),
    since: str = Query(None, description="Time range: '5m', '1h', '2025-01-01T00:00:00'"),
    timestamps: bool = Query(False, description="Show timestamps"),
):
    """Read Docker container logs from a remote server."""
    _check_session(session_id)

    cmd = ["docker", "logs", shlex.quote(container), "--tail", str(lines)]
    if timestamps:
        cmd.append("--timestamps")
    if since:
        cmd.extend(["--since", shlex.quote(since)])

    command = " ".join(cmd)
    try:
        command = sanitize_command(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc))) from exc

    result = await _state.manager.execute(session_id=session_id, command=command, timeout=30)
    return result
