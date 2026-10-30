"""MCP entry for isolated, SHA-verifiable site image conversion."""

from __future__ import annotations

from typing import Any, Literal

from tool_results import tool_error, tool_success

from app.workspace.assets import AssetConflict, AssetError
from app.workspace.media_pipeline import MediaUnavailable, convert_candidate_image
from app.workspace.policy import WorkspacePolicyError
from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.tool_registry import instrumented, register_tool


def site_media_pipeline(
    project_id: str,
    source_path: str,
    target_path: str,
    source_sha256: str,
    codec: Literal["avif", "webp", "png"] = "avif",
    quality: int = 95,
    max_width: int = 0,
    min_psnr: float = 36.0,
    previous_sha256: str | None = None,
) -> dict[str, Any]:
    """Encode a candidate image in an isolated media worker.

    Preserves the original asset; no caller URL, host paths, decoder in MCP,
    automatic production deployment or vectorization. A working private
    media worker and an operator-provisioned token are required. When they
    are absent, no file is changed.
    """
    tool = "site_media_pipeline"
    try:
        registry = server_attr("_get_workspace_registry")()
        result = convert_candidate_image(
            registry, project_id, source_path, target_path, source_sha256,
            codec=codec, quality=quality, max_width=max_width,
            min_psnr=min_psnr, previous_sha256=previous_sha256,
        )
        return tool_success(tool=tool, result=result)
    except AssetConflict as exc:
        return tool_error(tool=tool, code="ASSET_CONFLICT", message=str(exc), source="gateway")
    except MediaUnavailable as exc:
        return tool_error(
            tool=tool, code="MEDIA_WORKER_UNAVAILABLE", message=str(exc),
            retryable=exc.retryable, source="gateway",
        )
    except (AssetError, WorkspacePolicyError, ValueError) as exc:
        return tool_error(
            tool=tool, code="MEDIA_CONVERSION_DENIED", message=str(exc),
            retryable=False, source="gateway",
        )
    except OSError:
        return tool_error(
            tool=tool, code="MEDIA_IO_ERROR",
            message="Media asset operation failed; inspect candidate state before retrying",
            source="gateway",
        )


def register_all() -> None:
    register_tool("site_media_pipeline")(
        instrumented("site_media_pipeline")(site_media_pipeline)
    )
