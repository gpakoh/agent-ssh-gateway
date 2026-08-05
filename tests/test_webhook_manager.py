"""Tests for app.webhook_manager — the CI/CD deploy-trigger webhook system.

No test coverage existed for this module before this file. Two real bugs
found and fixed here:
  1. execute_deploy() (the only reachable trigger — POST /api/webhooks/{id}/deploy)
     never recorded anything into _deployments, so GET /api/webhooks/{id}/deployments
     always returned an empty list for every deployment ever triggered.
  2. `cd {config.target_path} && ...` interpolated target_path into a shell
     command unescaped.

handle_webhook() (an incoming-webhook receiver with a stub "verify secret"
comment that never actually checked anything) was removed entirely — it was
never wired to any router endpoint, and api_help.py only ever documented
the explicit POST /api/webhooks/{id}/deploy trigger flow.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.webhook_manager import WebhookManager


def _manager():
    job_manager = AsyncMock()
    job_manager.create_job = AsyncMock(return_value="job-123")
    return WebhookManager(ssh_manager=AsyncMock(), job_manager=job_manager), job_manager


class TestAddWebhook:
    def test_creates_without_secret_param(self):
        manager, _ = _manager()
        config = manager.add_webhook(
            name="deploy-prod",
            webhook_type="github",
            target_path="/srv/app",
            deploy_command="git pull && systemctl restart app",
            context_id="ctx1",
        )
        assert config.name == "deploy-prod"
        assert not hasattr(config, "secret")


class TestExecuteDeploy:
    @pytest.mark.asyncio
    async def test_records_deployment_history(self):
        """Regression: execute_deploy() never appended to _deployments."""
        manager, job_manager = _manager()
        config = manager.add_webhook(
            name="deploy-prod",
            webhook_type="generic",
            target_path="/srv/app",
            deploy_command="./deploy.sh",
            context_id="ctx1",
        )

        result = await manager.execute_deploy(session_id="s1", webhook_id=config.id)

        assert result["status"] == "started"
        assert result["job_id"] == "job-123"

        history = manager.get_deployments(config.id)
        assert len(history) == 1
        assert history[0]["webhook_id"] == config.id
        assert history[0]["job_id"] == "job-123"
        assert history[0]["status"] == "running"

    @pytest.mark.asyncio
    async def test_target_path_with_single_quote_does_not_break_out(self):
        """Proven empirically: build the exact command string, run it through
        a real shell in a scratch directory, confirm the injected command's
        side effect (a marker file) never happened.
        """
        manager, job_manager = _manager()

        with tempfile.TemporaryDirectory() as tmpdir:
            marker = Path(tmpdir) / "pwned"
            config = manager.add_webhook(
                name="deploy-prod",
                webhook_type="generic",
                target_path=f"{tmpdir}'; touch {marker}; echo '",
                deploy_command="true",
                context_id="ctx1",
            )

            await manager.execute_deploy(session_id="s1", webhook_id=config.id)

            command = job_manager.create_job.call_args.kwargs["command"]
            subprocess.run(["sh", "-c", command], check=False)

            assert not marker.exists(), (
                f"target_path broke out of shell quoting and ran an injected "
                f"command: {command!r}"
            )

    @pytest.mark.asyncio
    async def test_unknown_webhook_returns_error(self):
        manager, _ = _manager()
        result = await manager.execute_deploy(session_id="s1", webhook_id="nonexistent")
        assert result["status"] == "error"


class TestHandleWebhookRemoved:
    def test_no_longer_exists(self):
        manager, _ = _manager()
        assert not hasattr(manager, "handle_webhook")
