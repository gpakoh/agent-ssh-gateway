"""Admin endpoint for approving/denying per-command approval requests (ASK mode)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth_middleware import AuthIdentity, require_master_key
from app.policy_ask import approve_request, deny_request, get_request

router = APIRouter(tags=["admin-approval"])


class ApprovalDecisionRequest(BaseModel):
    approval_id: str
    decision: str  # "allow" | "deny"
    operator: str = "operator"


class ApprovalDecisionResponse(BaseModel):
    approval_id: str
    decision: str
    approved: bool
    command: str
    reason: str


@router.post(
    "/api/admin/approval/decision",
    response_model=ApprovalDecisionResponse,
)
async def set_approval_decision(
    req: ApprovalDecisionRequest,
    _identity: AuthIdentity = Depends(require_master_key),
) -> ApprovalDecisionResponse:
    if req.decision not in ("allow", "deny"):
        raise HTTPException(422, "decision must be 'allow' or 'deny'")

    approval_req = get_request(req.approval_id)
    if approval_req is None:
        raise HTTPException(404, "Approval request not found or expired")

    if req.decision == "allow":
        ok = approve_request(req.approval_id, operator=req.operator)
    else:
        ok = deny_request(req.approval_id, operator=req.operator)

    if not ok:
        raise HTTPException(409, "Approval request already decided or expired")

    return ApprovalDecisionResponse(
        approval_id=req.approval_id,
        decision=req.decision,
        approved=req.decision == "allow",
        command=approval_req.command,
        reason=approval_req.reason,
    )
