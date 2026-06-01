"""File tree explorer for IDE-like directory navigation."""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class FileNode:
    """Node in file tree."""
    name: str
    path: str
    type: str  # file, directory, symlink
    size: int = 0
    children: list = field(default_factory=list)
    is_expanded: bool = False
    is_git_ignored: bool = False
    permissions: str = ""
    modified_at: str = ""


class FileTreeExplorer:
    """Explore directory structure."""

    def __init__(self, ssh_manager):
        self._ssh = ssh_manager

    async def get_tree(
        self,
        session_id: str,
        path: str,
        depth: int = 2,
        show_hidden: bool = False,
        show_git_ignored: bool = False,
        max_files: int = 100,
    ) -> FileNode:
        """Get directory tree."""
        # Get directory info
        ls_cmd = f"ls -la '{path}'"
        result = await self._ssh.execute(session_id, ls_cmd, timeout=15)
        
        if result["exit_code"] != 0:
            raise Exception(f"Cannot read directory: {result['stderr']}")
        
        root_name = path.split("/")[-1] or path
        root = FileNode(
            name=root_name,
            path=path,
            type="directory",
            is_expanded=True,
        )
        
        # Parse ls output
        lines = result["stdout"].strip().split("\n")
        file_count = 0
        
        for line in lines[1:]:  # Skip total line
            if not line.strip():
                continue
            
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            
            permissions = parts[0]
            name = parts[8]
            
            # Skip . and ..
            if name in (".", ".."):
                continue
            
            # Skip hidden files
            if not show_hidden and name.startswith("."):
                continue
            
            # Check max files limit
            file_count += 1
            if file_count > max_files:
                logger.warning("Max files limit reached: %s", path)
                break
            
            full_path = f"{path}/{name}"
            
            if permissions.startswith("d"):
                # Directory
                dir_node = FileNode(
                    name=name,
                    path=full_path,
                    type="directory",
                    permissions=permissions,
                )
                
                # Recursively get children if depth > 0
                if depth > 0:
                    try:
                        children = await self._get_directory_children(
                            session_id, full_path, depth - 1, show_hidden, max_files
                        )
                        dir_node.children = children
                    except Exception as exc:
                        logger.warning("Cannot read subdirectory %s: %s", full_path, exc)
                
                root.children.append(dir_node)
            else:
                # File
                size = int(parts[4]) if parts[4].isdigit() else 0
                file_node = FileNode(
                    name=name,
                    path=full_path,
                    type="file",
                    size=size,
                    permissions=permissions,
                )
                root.children.append(file_node)
        
        # Sort: directories first, then files
        root.children.sort(key=lambda x: (0 if x.type == "directory" else 1, x.name.lower()))
        
        return root

    async def _get_directory_children(
        self,
        session_id: str,
        path: str,
        depth: int,
        show_hidden: bool,
        max_files: int = 100,
    ) -> list[FileNode]:
        """Get children of a directory."""
        children: list[FileNode] = []
        file_count = 0
        
        ls_cmd = f"ls -la '{path}'"
        result = await self._ssh.execute(session_id, ls_cmd, timeout=10)
        
        if result["exit_code"] != 0:
            return children
        
        lines = result["stdout"].strip().split("\n")
        for line in lines[1:]:
            if not line.strip():
                continue
            
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            
            permissions = parts[0]
            name = parts[8]
            
            if name in (".", ".."):
                continue
            
            if not show_hidden and name.startswith("."):
                continue
            
            file_count += 1
            if file_count > max_files:
                break
            
            full_path = f"{path}/{name}"
            
            if permissions.startswith("d"):
                dir_node = FileNode(
                    name=name,
                    path=full_path,
                    type="directory",
                    permissions=permissions,
                )
                
                if depth > 0:
                    try:
                        sub_children = await self._get_directory_children(
                            session_id, full_path, depth - 1, show_hidden, max_files
                        )
                        dir_node.children = sub_children
                    except Exception:
                        pass
                
                children.append(dir_node)
            else:
                size = int(parts[4]) if parts[4].isdigit() else 0
                children.append(FileNode(
                    name=name,
                    path=full_path,
                    type="file",
                    size=size,
                    permissions=permissions,
                ))
        
        children.sort(key=lambda x: (0 if x.type == "directory" else 1, x.name.lower()))
        return children

    def node_to_dict(self, node: FileNode) -> dict:
        """Convert node to dictionary."""
        result = {
            "name": node.name,
            "path": node.path,
            "type": node.type,
            "size": node.size,
            "is_expanded": node.is_expanded,
            "permissions": node.permissions,
            "modified_at": node.modified_at,
        }
        
        if node.children:
            result["children"] = [self.node_to_dict(child) for child in node.children]
        
        return result
