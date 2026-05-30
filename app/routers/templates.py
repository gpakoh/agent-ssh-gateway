"""Predefined command templates for SSH execution."""

import logging

from fastapi import APIRouter, HTTPException

from app import state as _state
from app.state import _err
from app.models import CommandTemplate, TemplateRunRequest, TemplateRunResponse
from app.security import sanitize_command

logger = logging.getLogger(__name__)

router = APIRouter(tags=["templates"])

TEMPLATES: list[CommandTemplate] = [
    CommandTemplate(
        id="deploy",
        name="Deploy service",
        description="Restart and check service status",
        command="systemctl restart {service} && systemctl status {service}",
        params=[{"name": "service", "type": "string", "required": True, "desc": "Systemd unit name"}],
    ),
    CommandTemplate(
        id="healthcheck",
        name="Service health",
        description="Check if service is active",
        command="systemctl is-active --quiet {service} && echo 'active' || echo 'inactive'",
        params=[{"name": "service", "type": "string", "required": True, "desc": "Systemd unit name"}],
    ),
    CommandTemplate(
        id="disk-usage",
        name="Disk usage",
        description="Show disk usage for a path",
        command="df -h {path}",
        params=[{"name": "path", "type": "string", "required": False, "desc": "Mount point (default: /)"}],
    ),
    CommandTemplate(
        id="memory",
        name="Memory status",
        description="Show free memory",
        command="free -h",
        params=[],
    ),
    CommandTemplate(
        id="docker-ps",
        name="Docker processes",
        description="List running containers",
        command="docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'",
        params=[],
    ),
    CommandTemplate(
        id="docker-stats",
        name="Docker stats",
        description="Live container resource usage",
        command="docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'",
        params=[],
    ),
    CommandTemplate(
        id="nginx-reload",
        name="Reload nginx",
        description="Test config and reload nginx",
        command="nginx -t && systemctl reload nginx",
        params=[],
    ),
    CommandTemplate(
        id="uptime",
        name="System uptime",
        description="Show system uptime and load",
        command="uptime",
        params=[],
    ),
    CommandTemplate(
        id="journal",
        name="Journal logs",
        description="Recent system logs (shortcut)",
        command="journalctl -n 30 --no-pager",
        params=[],
    ),
]


@router.get("/api/command-templates", response_model=list[CommandTemplate])
async def list_command_templates():
    """List all predefined command templates."""
    return TEMPLATES


@router.post("/api/templates/run", response_model=TemplateRunResponse)
async def run_template(req: TemplateRunRequest):
    """Execute a command template with parameter substitution."""
    template = next((t for t in TEMPLATES if t.id == req.template), None)
    if not template:
        raise HTTPException(status_code=404, detail=_err(404, f"Template not found: {req.template}"))

    command = template.command
    for key, val in req.params.items():
        command = command.replace(f"{{{key}}}", val)

    try:
        command = sanitize_command(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_err(400, str(exc)))

    result = await _state.manager.execute(session_id=req.session_id, command=command, timeout=30)
    return TemplateRunResponse(**result)
