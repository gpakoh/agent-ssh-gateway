"""Snapshot system for project state capture and restore."""

import json
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """Project snapshot."""
    id: str
    name: str
    context_id: str
    created_at: float
    files: list[str] = field(default_factory=list)
    description: str = ""
    git_commit_before: str | None = None
    git_commit_after: str | None = None
    size_bytes: int = 0


class SnapshotManager:
    """Manage project snapshots."""

    SNAPSHOTS_DIR = ".ssh-gateway-snapshots"

    def __init__(self, ssh_manager, context_manager):
        self._ssh = ssh_manager
        self._context = context_manager

    async def create_snapshot(
        self,
        session_id: str,
        context_id: str,
        name: str,
        description: str = "",
    ) -> Snapshot:
        """Create a snapshot of current project state."""
        ctx = await self._context.get_context(context_id)
        if not ctx:
            raise ValueError(f"Context {context_id} not found")

        snapshot_id = f"snap_{int(time.time())}"
        snapshot_dir = f"{ctx.path}/{self.SNAPSHOTS_DIR}/{snapshot_id}"

        # Create snapshot directory
        await self._ssh.execute(session_id, f"mkdir -p '{snapshot_dir}'", timeout=10)

        # Get list of tracked files (modified + staged)
        result = await self._ssh.execute(
            session_id,
            f"cd {ctx.path} && git status --short 2>/dev/null | awk '{{print $2}}'",
            timeout=10
        )
        modified_files = [f.strip() for f in result["stdout"].strip().split("\n") if f.strip()]

        # Get current git commit
        commit_result = await self._ssh.execute(
            session_id,
            f"cd {ctx.path} && git rev-parse HEAD 2>/dev/null || echo 'none'",
            timeout=10
        )
        git_commit_before = commit_result["stdout"].strip()

        # Copy modified files to snapshot
        for file_path in modified_files:
            dest_dir = f"{snapshot_dir}/{file_path.rsplit('/', 1)[0] if '/' in file_path else ''}"
            await self._ssh.execute(session_id, f"mkdir -p '{dest_dir}'", timeout=5)
            await self._ssh.execute(
                session_id,
                f"cp '{ctx.path}/{file_path}' '{snapshot_dir}/{file_path}'",
                timeout=5
            )

        # Create snapshot metadata
        snapshot = Snapshot(
            id=snapshot_id,
            name=name,
            context_id=context_id,
            created_at=time.time(),
            files=modified_files,
            description=description,
            git_commit_before=git_commit_before if git_commit_before != "none" else None,
        )

        # Save metadata
        meta_content = json.dumps({
            "id": snapshot.id,
            "name": snapshot.name,
            "context_id": snapshot.context_id,
            "created_at": snapshot.created_at,
            "files": snapshot.files,
            "description": snapshot.description,
            "git_commit_before": snapshot.git_commit_before,
        }, indent=2)

        await self._ssh.execute(
            session_id,
            f"cat > '{snapshot_dir}/.snapshot-meta.json' << 'EOF'\n{meta_content}\nEOF",
            timeout=10
        )

        # Calculate size
        size_result = await self._ssh.execute(
            session_id,
            f"du -sb '{snapshot_dir}' | cut -f1",
            timeout=10
        )
        snapshot.size_bytes = int(size_result["stdout"].strip() or 0)

        logger.info("Snapshot %s created for context %s", snapshot_id, context_id)
        return snapshot

    async def restore_snapshot(
        self,
        session_id: str,
        context_id: str,
        snapshot_id: str,
    ) -> dict:
        """Restore project from snapshot."""
        ctx = await self._context.get_context(context_id)
        if not ctx:
            raise ValueError(f"Context {context_id} not found")

        snapshot_dir = f"{ctx.path}/{self.SNAPSHOTS_DIR}/{snapshot_id}"

        # Check if snapshot exists
        check_result = await self._ssh.execute(
            session_id,
            f"test -d '{snapshot_dir}' && echo 'exists' || echo 'not_found'",
            timeout=5
        )

        if check_result["stdout"].strip() != "exists":
            raise ValueError(f"Snapshot {snapshot_id} not found")

        # Read metadata
        meta_result = await self._ssh.execute(
            session_id,
            f"cat '{snapshot_dir}/.snapshot-meta.json'",
            timeout=5
        )

        try:
            metadata = json.loads(meta_result["stdout"])
        except json.JSONDecodeError:
            raise ValueError("Invalid snapshot metadata") from None

        # Restore files
        restored_files = []
        for file_path in metadata.get("files", []):
            src = f"{snapshot_dir}/{file_path}"
            dest = f"{ctx.path}/{file_path}"
            
            # Ensure destination directory exists
            dest_dir = dest.rsplit("/", 1)[0] if "/" in dest else ctx.path
            await self._ssh.execute(session_id, f"mkdir -p '{dest_dir}'", timeout=5)
            
            # Copy file
            result = await self._ssh.execute(
                session_id,
                f"cp '{src}' '{dest}'",
                timeout=5
            )
            
            if result["exit_code"] == 0:
                restored_files.append(file_path)

        logger.info("Snapshot %s restored: %d files", snapshot_id, len(restored_files))
        return {
            "success": True,
            "snapshot_id": snapshot_id,
            "restored_files": restored_files,
            "total_files": len(metadata.get("files", [])),
        }

    async def list_snapshots(
        self,
        session_id: str,
        context_id: str,
    ) -> list[Snapshot]:
        """List all snapshots for context."""
        ctx = await self._context.get_context(context_id)
        if not ctx:
            raise ValueError(f"Context {context_id} not found")

        snapshots_dir = f"{ctx.path}/{self.SNAPSHOTS_DIR}"
        
        # Check if snapshots directory exists
        check_result = await self._ssh.execute(
            session_id,
            f"test -d '{snapshots_dir}' && echo 'exists' || echo 'not_found'",
            timeout=5
        )
        
        if check_result["stdout"].strip() != "exists":
            return []

        # List snapshots
        result = await self._ssh.execute(
            session_id,
            f"ls -1 '{snapshots_dir}'",
            timeout=10
        )

        snapshots = []
        for line in result["stdout"].strip().split("\n"):
            snapshot_id = line.strip()
            if not snapshot_id:
                continue

            # Read metadata
            meta_result = await self._ssh.execute(
                session_id,
                f"cat '{snapshots_dir}/{snapshot_id}/.snapshot-meta.json' 2>/dev/null || echo '{{}}'",
                timeout=5
            )

            try:
                metadata = json.loads(meta_result["stdout"])
                snapshot = Snapshot(
                    id=metadata.get("id", snapshot_id),
                    name=metadata.get("name", snapshot_id),
                    context_id=metadata.get("context_id", context_id),
                    created_at=metadata.get("created_at", 0),
                    files=metadata.get("files", []),
                    description=metadata.get("description", ""),
                    git_commit_before=metadata.get("git_commit_before"),
                )
                snapshots.append(snapshot)
            except json.JSONDecodeError:
                continue

        # Sort by created_at descending
        snapshots.sort(key=lambda s: s.created_at, reverse=True)
        return snapshots

    async def delete_snapshot(
        self,
        session_id: str,
        context_id: str,
        snapshot_id: str,
    ) -> bool:
        """Delete a snapshot."""
        ctx = await self._context.get_context(context_id)
        if not ctx:
            raise ValueError(f"Context {context_id} not found")

        snapshot_dir = f"{ctx.path}/{self.SNAPSHOTS_DIR}/{snapshot_id}"
        
        result = await self._ssh.execute(
            session_id,
            f"rm -rf '{snapshot_dir}'",
            timeout=10
        )

        return result["exit_code"] == 0
