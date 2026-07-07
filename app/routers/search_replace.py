"""Global search and replace routes."""

from fastapi import APIRouter, Depends

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.models import (
    GlobalReplaceRequest,
    GlobalReplaceResponse,
    GlobalSearchRequest,
    GlobalSearchResponse,
    ReplaceResultItem,
    SearchMatchItem,
)

router = APIRouter()


@router.post("/api/search/global", tags=["code"], response_model=GlobalSearchResponse)
async def global_search(
    req: GlobalSearchRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Search across all project files."""
    matches = await _state.search_replace.search(
        session_id=req.session_id,
        path=req.path,
        query=req.query,
        file_pattern=req.file_pattern,
        use_regex=req.use_regex,
        case_sensitive=req.case_sensitive,
        context_lines=req.context_lines,
    )

    files_affected = list(set(m.path for m in matches))

    return GlobalSearchResponse(
        query=req.query,
        matches=[
            SearchMatchItem(
                path=m.path,
                line=m.line,
                column=m.column,
                content=m.content,
            )
            for m in matches
        ],
        total_count=len(matches),
        files_affected=files_affected,
    )


@router.post("/api/replace/global", tags=["code"], response_model=GlobalReplaceResponse)
async def global_replace(
    req: GlobalReplaceRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Replace across all project files."""
    results = await _state.search_replace.replace(
        session_id=req.session_id,
        path=req.path,
        search_query=req.search,
        replace_with=req.replace,
        file_pattern=req.file_pattern,
        use_regex=req.use_regex,
        case_sensitive=req.case_sensitive,
        dry_run=req.dry_run,
    )

    total_replacements = sum(r.replacements_count for r in results)
    files_modified = sum(1 for r in results if r.replacements_count > 0)

    git_commit = None
    if not req.dry_run and req.auto_commit and req.context_id and files_modified > 0:
        commit_result = await _state.context_manager.commit_changes(
            req.context_id,
            f"Global replace: '{req.search}' -> '{req.replace}'",
            [r.path for r in results if r.replacements_count > 0],
        )
        if commit_result.get("success"):
            git_commit = commit_result.get("hash")

    return GlobalReplaceResponse(
        search=req.search,
        replace=req.replace,
        results=[
            ReplaceResultItem(
                path=r.path,
                replacements_count=r.replacements_count,
                success=r.success,
                error=r.error,
            )
            for r in results
        ],
        total_replacements=total_replacements,
        files_modified=files_modified,
        dry_run=req.dry_run,
        git_commit=git_commit,
    )
