"""One grouped MCP capability for binary site assets.

No arbitrary URLs, local host paths, subprocesses or implicit attachment
access. All decoded bytes are supplied explicitly by the invoking MCP client.
"""

from __future__ import annotations

from typing import Any, Literal

from tool_results import tool_error, tool_success

from app.workspace.asset_transfer import AssetTransferStore
from app.workspace.assets import AssetConflict, AssetError
from app.workspace.policy import WorkspacePolicyError
from examples.mcp_server.mcp_infra._server_ref import server_attr
from examples.mcp_server.mcp_infra.tool_registry import instrumented, register_tool

_STORE = AssetTransferStore()


def site_asset_transfer(
    action: Literal["begin", "chunk", "status", "finish", "abort", "inspect", "read"],
    project_id: str,
    relative_path: str,
    upload_id: str = "",
    sha256: str = "",
    total_bytes: int = 0,
    previous_sha256: str | None = None,
    create_parents: bool = False,
    offset: int = 0,
    chunk_b64: str = "",
    chunk_sha256: str = "",
) -> dict[str, Any]:
    """Transfer original binary media to a registered candidate, in safe chunks.

    A single tool in gateway_catalog(group="sites"). Begin declares exact
    candidate/path, byte length and full SHA-256; chunk sends <=64 KiB as
    RFC4648 base64 with explicit offset and per-chunk SHA-256. Use status to
    resume an uncertain chunk; finish verifies all bytes and delegates atomic
    candidate-only write/CAS to the existing workspace assets contract.
    Inspect/read provide small, hash-fenced retrieval for verification.
    Abort discards staged bytes. Staging expires after 15 minutes or a server
    restart; it never modifies a Git checkout before finish.

    Tool arguments must contain actual file bytes. This does NOT automatically
    resolve a ChatGPT conversation attachment or a sandbox file path.
    """
    tool = "site_asset_transfer"
    try:
        registry = server_attr("_get_workspace_registry")()
        if action == "begin":
            result = _STORE.begin(
                registry, project_id, relative_path, sha256, total_bytes,
                previous_sha256=previous_sha256, create_parents=create_parents,
            )
        elif action == "chunk":
            result = _STORE.chunk(
                upload_id, project_id, relative_path, offset, chunk_b64, chunk_sha256
            )
        elif action == "status":
            result = _STORE.status(upload_id, project_id, relative_path)
        elif action == "finish":
            result = _STORE.finish(registry, upload_id, project_id, relative_path)
        elif action == "abort":
            result = _STORE.abort(upload_id, project_id, relative_path)
        elif action == "inspect":
            result = _STORE.inspect(registry, project_id, relative_path)
        elif action == "read":
            result = _STORE.read(registry, project_id, relative_path, offset, sha256)
        else:
            raise AssetError("Unknown asset transfer action")
        return tool_success(tool=tool, result=result)
    except AssetConflict as exc:
        return tool_error(
            tool=tool, code="ASSET_CONFLICT", message=str(exc),
            retryable=False, source="gateway",
        )
    except (AssetError, WorkspacePolicyError, ValueError) as exc:
        return tool_error(
            tool=tool, code="ASSET_TRANSFER_DENIED", message=str(exc),
            retryable=False, source="gateway",
        )
    except OSError:
        return tool_error(
            tool=tool, code="ASSET_IO_ERROR",
            message="Site asset operation failed; inspect status before retrying",
            retryable=False, source="gateway",
        )


def register_all() -> None:
    register_tool("site_asset_transfer")(
        instrumented("site_asset_transfer")(site_asset_transfer)
    )
