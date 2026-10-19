"""Guard fixed CRM provisioner packaging and importability before production."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP_DOCKERFILE = ROOT / "docker" / "Dockerfile.mcp-server"
CRM_HELPER = ROOT / "scripts" / "site_audit_crm_provisioner.py"
IMAGE_IMPORT_SMOKE = (
    "RUN /app/.venv/bin/python "
    "/app/scripts/site_audit_crm_provisioner.py --help > /dev/null"
)


def test_exact_mcp_image_build_smokes_fixed_crm_helper_after_copy() -> None:
    dockerfile = MCP_DOCKERFILE.read_text(encoding="utf-8")
    copy = "COPY --chown=appuser:appuser scripts/ ./scripts/"
    assert dockerfile.count(copy) == 1
    assert dockerfile.count(IMAGE_IMPORT_SMOKE) == 1
    assert dockerfile.index(copy) < dockerfile.index(IMAGE_IMPORT_SMOKE)
    assert dockerfile.index(IMAGE_IMPORT_SMOKE) < dockerfile.index("USER appuser")
    assert CRM_HELPER.is_file()


def test_fixed_crm_helper_help_imports_without_secrets_or_network() -> None:
    # argparse --help exits before inspect/provision, without reading .env or
    # making HTTP calls. Spawn the same entrypoint as the Docker build gate.
    clean_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(CRM_HELPER), "--help"],
        cwd=ROOT,
        env=clean_env,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, "CRM helper cannot import/parse in frozen test env"
    assert "--mode" in proc.stdout
    assert "--expected-env-sha256" in proc.stdout
    assert "Traceback" not in proc.stderr
