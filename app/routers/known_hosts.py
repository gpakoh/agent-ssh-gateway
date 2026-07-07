"""Known-hosts management routes."""

import asyncio
import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.models import (
    KnownHostAddRequest,
    KnownHostCheckResponse,
    KnownHostLookupResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


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
        "ssh-keyscan",
        "-T",
        "5",
        "-t",
        "rsa,ecdsa,ed25519",
        "-p",
        str(req.port),
        req.host,
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
