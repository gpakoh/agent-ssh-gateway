"""Bulk operations for mass processing."""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class BulkOperationsManager:
    """Manager for bulk operations.
    
    Optimized for processing large numbers of files/commands
    with concurrency control.
    """
    
    def __init__(self, max_concurrency: int = 10):
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
    
    async def execute_bulk(
        self,
        items: list[Any],
        executor_func,
        max_concurrency: int | None = None,
    ) -> list[dict]:
        """Execute bulk operations with concurrency control.
        
        Args:
            items: List of items to process
            executor_func: Async function to execute for each item
            max_concurrency: Max concurrent operations (default: self._max_concurrency)
            
        Returns:
            List of results
        """
        concurrency = max_concurrency or self._max_concurrency
        semaphore = asyncio.Semaphore(concurrency)
        
        async def execute_with_limit(item):
            async with semaphore:
                try:
                    result = await executor_func(item)
                    return {"success": True, "item": item, "result": result}
                except Exception as exc:
                    logger.error("Bulk operation failed for %s: %s", item, exc)
                    return {"success": False, "item": item, "error": str(exc)}
        
        tasks = [execute_with_limit(item) for item in items]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        return [r if isinstance(r, dict) else {"success": False, "error": str(r)} for r in results]
    
    async def execute_batch_commands(
        self,
        session_id: str,
        commands: list[str],
        ssh_manager,
        max_concurrency: int = 5,
    ) -> list[dict]:
        """Execute multiple commands on same session.
        
        Args:
            session_id: SSH session ID
            commands: List of commands to execute
            ssh_manager: SSH session manager
            max_concurrency: Max concurrent commands
            
        Returns:
            List of command results
        """
        async def execute_cmd(command):
            return await ssh_manager.execute(session_id, command, timeout=120)
        
        return await self.execute_bulk(commands, execute_cmd, max_concurrency)
    
    async def read_files_bulk(
        self,
        session_id: str,
        paths: list[str],
        file_editor,
        max_concurrency: int = 10,
    ) -> dict[str, str]:
        """Read multiple files concurrently.
        
        Args:
            session_id: SSH session ID
            paths: List of file paths
            file_editor: File editor instance
            max_concurrency: Max concurrent reads
            
        Returns:
            Dict mapping path to content
        """
        results = {}
        
        async def read_file(path):
            try:
                content = await file_editor.read_file(session_id, path)
                return path, content
            except Exception as exc:
                logger.error("Failed to read %s: %s", path, exc)
                return path, None
        
        semaphore = asyncio.Semaphore(max_concurrency)
        
        async def read_with_limit(path):
            async with semaphore:
                return await read_file(path)
        
        tasks = [read_with_limit(path) for path in paths]
        file_results = await asyncio.gather(*tasks)
        
        for path, content in file_results:
            if content is not None:
                results[path] = content
        
        return results
    
    async def edit_files_bulk(
        self,
        session_id: str,
        edits: list[dict],
        file_editor,
        max_concurrency: int = 5,
    ) -> list[dict]:
        """Edit multiple files concurrently.
        
        Args:
            session_id: SSH session ID
            edits: List of edit operations [{"path": str, "operations": [...]}]
            file_editor: File editor instance
            max_concurrency: Max concurrent edits
            
        Returns:
            List of edit results
        """
        async def edit_file(edit):
            try:
                result = await file_editor.edit_file(
                    session_id,
                    edit["path"],
                    edit["operations"],
                )
                return {
                    "path": edit["path"],
                    "success": True,
                    "operations_applied": result.get("operations_applied", 0),
                    "changed": result.get("changed", False),
                }
            except Exception as exc:
                logger.error("Failed to edit %s: %s", edit["path"], exc)
                return {
                    "path": edit["path"],
                    "success": False,
                    "error": str(exc),
                }
        
        return await self.execute_bulk(edits, edit_file, max_concurrency)
