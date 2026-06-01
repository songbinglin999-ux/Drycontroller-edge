"""
Factory activation API.

Exposes activation state and manual trigger for the factory license -> bootstrap tailnet -> Cloud Ops flow.
"""

from __future__ import annotations

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.security import require_admin_api_key
from app.services.activation import get_activation_state, run_activation


router = APIRouter()
logger = structlog.get_logger(__name__)


class ActivationTriggerRequest(BaseModel):
    requested_tenant_id: Optional[str] = Field(None, max_length=64)
    requested_line_id: Optional[str] = Field(None, max_length=32)


@router.get("/activation/status")
async def activation_status(_: str = Depends(require_admin_api_key)):
    """Return current activation state and metadata."""
    return get_activation_state()


@router.post("/activation/trigger")
async def activation_trigger(
    req: ActivationTriggerRequest,
    _: str = Depends(require_admin_api_key),
):
    """Trigger the activation flow manually (or retry on failure)."""
    result = await run_activation(
        requested_tenant_id=req.requested_tenant_id,
        requested_line_id=req.requested_line_id,
    )
    if not result.get("success"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=result,
        )
    return result
