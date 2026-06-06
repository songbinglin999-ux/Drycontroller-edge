"""
Factory activation API.

Exposes activation state. Factory activation writes are owned by the host
`drycontroller-activation.service` path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.security import require_admin_api_key
from app.services.activation import get_activation_state


router = APIRouter()


@router.get("/activation/status")
async def activation_status(_: str = Depends(require_admin_api_key)):
    """Return current activation state and metadata."""
    return get_activation_state()
