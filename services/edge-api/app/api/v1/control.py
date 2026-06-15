"""Control advisory recommendation API."""

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.security import require_control_api_key
from app.services.audit import api_key_actor
from app.services.control_orchestrator import ControlOrchestrator, get_control_orchestrator
from app.services.control_core.models import ALLOWED_CONTROL_SOFTPOINTS, SCHEMA_VERSION

router = APIRouter(dependencies=[Depends(require_control_api_key)])
logger = structlog.get_logger(__name__)


@router.get("/recommendations")
async def list_control_recommendations(
    line: Optional[str] = Query(None, description="可选：按产线过滤"),
    lookback: str = Query("5m", description="分析窗口，如 5m/10m"),
    orchestrator: ControlOrchestrator = Depends(get_control_orchestrator),
):
    try:
        return await orchestrator.get_recommendations(line=line, lookback=lookback)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


class RecommendationConfirmRequest(BaseModel):
    line: Optional[str] = Field(None, description="产线标识，默认 L1")
    timeout_sec: float = Field(3.0, ge=0.2, le=30.0, description="等待 ACK 超时（秒）")


class ManualRecommendationConfirmRequest(BaseModel):
    line: Optional[str] = Field(None, description="产线标识，默认 L1")
    target_softpoint: str = Field(..., description="控制软点，如 VFM3_FREQ_SP / VF3_FREQ_SP")
    value: float = Field(..., description="人工确认后的目标值")
    request_id: Optional[str] = Field(None, description="幂等 ID（可选）")
    operator_id: Optional[str] = Field(None, description="操作员 ID（用于审计）")
    reason: Optional[str] = Field(None, description="操作原因（用于审计/HMI 展示）")
    timeout_sec: float = Field(3.0, ge=0.2, le=30.0, description="等待 ACK 超时（秒）")


@router.post("/recommendations/manual-confirm")
async def confirm_manual_control_recommendation(
    req: ManualRecommendationConfirmRequest,
    api_key: str = Depends(require_control_api_key),
    orchestrator: ControlOrchestrator = Depends(get_control_orchestrator),
):
    resp = await orchestrator.confirm_manual_action(
        line=req.line,
        target_softpoint=req.target_softpoint,
        value=req.value,
        actor=api_key_actor(api_key, role="control"),
        operator_id=req.operator_id,
        request_id=req.request_id,
        reason=req.reason,
        timeout_sec=req.timeout_sec,
    )
    if not resp.get("success") and resp.get("error") == "manual_command_not_actionable":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=resp)
    return resp


@router.post("/recommendations/{recommendation_id}/confirm")
async def confirm_control_recommendation(
    recommendation_id: str,
    req: Optional[RecommendationConfirmRequest] = None,
    api_key: str = Depends(require_control_api_key),
    orchestrator: ControlOrchestrator = Depends(get_control_orchestrator),
):
    body = req or RecommendationConfirmRequest()
    resp = await orchestrator.confirm_recommendation(
        recommendation_id=recommendation_id,
        line=body.line,
        actor=api_key_actor(api_key, role="control"),
        timeout_sec=body.timeout_sec,
    )
    if not resp.get("success"):
        error = resp.get("error")
        if error == "recommendation_not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error)
        if error in {"recommendation_not_actionable", "recommendation_expired"}:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=resp)
        return resp
    return resp


class RecommendationDismissRequest(BaseModel):
    reason: Optional[str] = Field(None, description="操作员忽略原因（可选）")


@router.post("/recommendations/{recommendation_id}/dismiss")
async def dismiss_control_recommendation(
    recommendation_id: str,
    req: Optional[RecommendationDismissRequest] = None,
    api_key: str = Depends(require_control_api_key),
    orchestrator: ControlOrchestrator = Depends(get_control_orchestrator),
):
    return await orchestrator.dismiss_recommendation(
        recommendation_id=recommendation_id,
        actor=api_key_actor(api_key, role="control"),
        reason=req.reason if req else None,
    )


@router.get("/status")
async def get_control_status():
    return {
        "success": True,
        "data": {
            "mode": "manual",
            "auto_control_enabled": False,
            "emergency_stop_active": False,
            "controllable_tags": sorted(ALLOWED_CONTROL_SOFTPOINTS),
            "control_softpoints_status": "advisory_allowlist_only",
            "suggestion_schema": SCHEMA_VERSION,
            "actionability": "human_confirmed_only",
            "requires_confirmation": True,
            "command_entrypoint": "/api/v1/control/recommendations/manual-confirm",
        },
    }
