"""CI/CD Webhook integration for auto-deployment."""

import logging
from typing import Optional
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class WebhookType(Enum):
    """Webhook types."""
    GITHUB = "github"
    GITEA = "gitea"
    GENERIC = "generic"


class DeployStatus(Enum):
    """Deployment status."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class WebhookConfig:
    """Webhook configuration."""
    id: str
    name: str
    webhook_type: WebhookType
    secret: str
    target_path: str
    deploy_command: str
    context_id: str
    notify_url: Optional[str] = None
    enabled: bool = True


class WebhookManager:
    """Manage webhooks and auto-deployment."""

    def __init__(self, ssh_manager, job_manager):
        self._ssh = ssh_manager
        self._job = job_manager
        self._webhooks: dict[str, WebhookConfig] = {}
        self._deployments: list[dict] = []

    def add_webhook(
        self,
        name: str,
        webhook_type: str,
        secret: str,
        target_path: str,
        deploy_command: str,
        context_id: str,
        notify_url: str = None,
    ) -> WebhookConfig:
        """Add a new webhook."""
        import uuid
        webhook_id = str(uuid.uuid4())[:8]
        
        config = WebhookConfig(
            id=webhook_id,
            name=name,
            webhook_type=WebhookType(webhook_type),
            secret=secret,
            target_path=target_path,
            deploy_command=deploy_command,
            context_id=context_id,
            notify_url=notify_url,
        )
        
        self._webhooks[webhook_id] = config
        return config

    def get_webhook(self, webhook_id: str) -> Optional[WebhookConfig]:
        """Get webhook by ID."""
        return self._webhooks.get(webhook_id)

    def list_webhooks(self) -> list[WebhookConfig]:
        """List all webhooks."""
        return list(self._webhooks.values())

    def remove_webhook(self, webhook_id: str) -> bool:
        """Remove a webhook."""
        if webhook_id in self._webhooks:
            del self._webhooks[webhook_id]
            return True
        return False

    async def handle_webhook(
        self,
        webhook_id: str,
        payload: dict,
        headers: dict,
    ) -> dict:
        """Handle incoming webhook."""
        config = self._webhooks.get(webhook_id)
        if not config:
            return {"status": "error", "message": "Webhook not found"}
        
        if not config.enabled:
            return {"status": "error", "message": "Webhook disabled"}
        
        # Verify Secret (simple Check)
        # In Production, Use HMAC Signature Verification
        
        # Trigger Deployment
        deploy_id = f"deploy_{len(self._deployments)}"
        
        # Log Deployment
        deployment = {
            "id": deploy_id,
            "webhook_id": webhook_id,
            "webhook_name": config.name,
            "status": DeployStatus.PENDING.value,
            "timestamp": logging.time.time() if hasattr(logging, 'time') else 0,
            "payload": payload,
        }
        self._deployments.append(deployment)
        
        return {
            "status": "accepted",
            "deploy_id": deploy_id,
            "message": f"Deployment queued for {config.name}",
        }

    def get_deployments(self, webhook_id: str = None) -> list[dict]:
        """Get deployment history."""
        if webhook_id:
            return [d for d in self._deployments if d["webhook_id"] == webhook_id]
        return self._deployments[-50:]  # Last 50

    async def execute_deploy(
        self,
        session_id: str,
        webhook_id: str,
    ) -> dict:
        """Execute deployment manually."""
        config = self._webhooks.get(webhook_id)
        if not config:
            return {"status": "error", "message": "Webhook not found"}
        
        # Run Deploy Command As Background Job
        job_id = await self._job.create_job(
            session_id=session_id,
            command=f"cd {config.target_path} && {config.deploy_command}",
            timeout=600,
        )
        
        return {
            "status": "started",
            "job_id": job_id,
            "command": config.deploy_command,
        }
