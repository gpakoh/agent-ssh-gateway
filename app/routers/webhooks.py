"""Webhook management and deployment routes."""

from fastapi import APIRouter, Depends

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.models import (
    CreateWebhookRequest,
    DeployRequest,
    DeployResponse,
    WebhookConfigResponse,
    WebhookListResponse,
)

router = APIRouter()


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
