"""Project inspection routes: analytics and file tree."""

from fastapi import APIRouter, Depends

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.models import (
    CodeStats,
    DependencyStats,
    FileStats,
    FileTreeNode,
    FileTreeRequest,
    FileTreeResponse,
    GitStats,
    ProjectAnalyticsRequest,
    ProjectAnalyticsResponse,
    TestStats,
)

router = APIRouter()


@router.post("/api/analytics", tags=["code"], response_model=ProjectAnalyticsResponse)
async def run_analytics(
    req: ProjectAnalyticsRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Analyze project and return metrics."""
    metrics_data = await _state.analytics.analyze_project(
        session_id=req.session_id,
        path=req.path,
    )

    return ProjectAnalyticsResponse(
        project_path=metrics_data["project_path"],
        files=FileStats(**metrics_data["files"]),
        code=CodeStats(**metrics_data["code"]),
        git=GitStats(**metrics_data["git"]),
        tests=TestStats(**metrics_data["tests"]),
        dependencies=DependencyStats(**metrics_data["dependencies"]),
    )


@router.post("/api/tree", tags=["files"], response_model=FileTreeResponse)
async def get_file_tree_v2(
    req: FileTreeRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Get directory tree structure."""
    tree = await _state.file_tree.get_tree(
        session_id=req.session_id,
        path=req.path,
        depth=req.depth,
        show_hidden=req.show_hidden,
        max_files=req.max_files,
    )

    def count_files(node) -> tuple[int, int]:
        files = 0
        dirs = 0
        if node.type == "file":
            files = 1
        elif node.type == "directory":
            dirs = 1
            for child in node.children:
                f, d = count_files(child)
                files += f
                dirs += d
        return files, dirs

    total_files, total_dirs = count_files(tree)

    return FileTreeResponse(
        root=FileTreeNode(**_state.file_tree.node_to_dict(tree)),
        total_files=total_files,
        total_directories=total_dirs,
    )
