"""Code intelligence and generation routes."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from app import state as _state
from app.auth_middleware import AuthIdentity, require_master_key
from app.models import (
    CodeCompleteRequest,
    CodeCompleteResponse,
    CodeGenerateRequest,
    CodeGenerateResponse,
    CodeInsertRequest,
    CodeInsertResponse,
    CodeInsertSuggestion,
    CodeSearchRequest,
    CodeSearchResponse,
    CodeSearchResultItem,
)
from app.routers.workspace import assert_workspace_writable
from app.state import _err

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/code/search", tags=["code"], response_model=CodeSearchResponse)
async def code_search(
    req: CodeSearchRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Search for code pattern in project."""
    results = await _state.code_intelligence.search_code(
        session_id=req.session_id,
        path=req.path,
        query=req.query,
        language=req.language,
        context_lines=req.context_lines,
    )

    return CodeSearchResponse(
        query=req.query,
        results=[
            CodeSearchResultItem(
                path=r.path,
                line=r.line,
                column=r.column,
                content=r.content,
            )
            for r in results
        ],
        count=len(results),
    )


@router.post("/api/code/insert", tags=["code"], response_model=CodeInsertResponse)
async def code_insert(
    req: CodeInsertRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Intelligently insert code based on natural language instruction."""
    assert_workspace_writable(
        actor_type=_identity.token_type,
        actor_name=_identity.name or "",
        actor_fingerprint=_identity.fingerprint[:12],
        route="POST /api/code/insert",
    )
    ctx = await _state.context_manager.get_context(req.context_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=_err(404, "Context not found"))

    suggestion = await _state.code_intelligence.find_insertion_point(
        session_id=ctx.session_id,
        path=req.path,
        instruction=req.instruction,
        language=req.language,
    )

    if not suggestion:
        raise HTTPException(status_code=400, detail=_err(400, "Could not find insertion point"))

    try:
        result = await _state.file_editor.edit_file(
            ctx.session_id,
            req.path,
            [{"type": "insert_after", "after": suggestion.insert_after, "text": suggestion.code}],
        )

        git_commit = None
        if req.auto_commit and result.get("success"):
            commit_result = await _state.context_manager.commit_changes(
                req.context_id, f"AI: {req.instruction}", [req.path]
            )
            if commit_result.get("success"):
                git_commit = commit_result.get("hash")

        return CodeInsertResponse(
            success=result.get("success", False),
            path=req.path,
            suggestion=CodeInsertSuggestion(
                insert_after=suggestion.insert_after,
                code=suggestion.code,
                explanation=suggestion.explanation,
                line_number=suggestion.line_number,
            ),
            applied=result.get("success", False),
            git_commit=git_commit,
        )
    except Exception as exc:
        logger.error("Code insertion failed: %s", exc)
        return CodeInsertResponse(
            success=False,
            path=req.path,
            suggestion=CodeInsertSuggestion(
                insert_after=suggestion.insert_after,
                code=suggestion.code,
                explanation=suggestion.explanation,
                line_number=suggestion.line_number,
            ),
            applied=False,
        )


@router.post("/api/code/generate", tags=["code"], response_model=CodeGenerateResponse)
async def code_generate(
    req: CodeGenerateRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Generate code based on natural language instruction."""
    code = await _state.code_intelligence.generate_code(
        session_id="",
        instruction=req.instruction,
        language=req.language,
    )

    return CodeGenerateResponse(
        code=code,
        language=req.language,
        explanation=f"Generated code for: {req.instruction}",
    )


@router.post("/api/code/complete", tags=["code"], response_model=CodeCompleteResponse)
async def code_complete(
    req: CodeCompleteRequest, _identity: AuthIdentity = Depends(require_master_key)
):
    """Suggest code completion."""
    completion = await _state.code_intelligence.suggest_completion(
        session_id=req.session_id,
        path=req.path,
        partial_code=req.partial_code,
        language=req.language,
    )

    return CodeCompleteResponse(
        completion=completion,
        context=req.partial_code[-100:] if len(req.partial_code) > 100 else req.partial_code,
    )
