"""Control advisory recommendation API."""

from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.security import require_control_api_key, require_readonly_http_source
from app.services.audit import api_key_actor
from app.services.control_orchestrator import ControlOrchestrator, get_control_orchestrator
from app.services.control_state import ControlStateService, get_control_state_service

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/recommendations")
async def list_control_recommendations(
    line: Optional[str] = Query(None, description="可选：按产线过滤"),
    lookback: str = Query("5m", description="分析窗口，如 5m/10m"),
    _: str = Depends(require_control_api_key),
    orchestrator: ControlOrchestrator = Depends(get_control_orchestrator),
):
    try:
        return await orchestrator.get_recommendations(line=line, lookback=lookback)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


class ManualSetRequest(BaseModel):
    line: Optional[str] = Field(None, description="产线标识，默认 L1")
    target_softpoint: str = Field(..., description="控制软点，如 VFM3_FREQ_SP / VF3_FREQ_SP")
    value: float = Field(..., description="MANUAL 模式下的目标值")
    request_id: Optional[str] = Field(None, description="幂等 ID（可选）")
    operator_id: Optional[str] = Field(None, description="操作员 ID（用于审计）")
    reason: Optional[str] = Field(None, description="操作原因（用于审计/HMI 展示）")
    timeout_sec: float = Field(3.0, ge=0.2, le=30.0, description="等待 ACK 超时（秒）")


class ControlModeRequest(BaseModel):
    line: Optional[str] = Field(None, description="产线标识，默认 L1")
    mode: str = Field(..., description="MANUAL 或 AUTO；AUTO_PAUSED 不是操作员可选模式")
    operator_id: Optional[str] = Field(None, description="操作员 ID（用于审计）")
    reason: Optional[str] = Field(None, description="授权/切换原因（用于审计）")


class AutoDispatchRequest(BaseModel):
    line: Optional[str] = Field(None, description="产线标识，默认 L1")
    lookback: str = Field("5m", description="分析窗口，如 5m/10m")
    timeout_sec: float = Field(3.0, ge=0.2, le=30.0, description="等待 ACK 超时（秒）")


@router.post("/manual/set")
async def set_manual_control(
    req: ManualSetRequest,
    api_key: str = Depends(require_control_api_key),
    orchestrator: ControlOrchestrator = Depends(get_control_orchestrator),
    state_service: ControlStateService = Depends(get_control_state_service),
):
    actor = api_key_actor(api_key, role="control")
    manual_state = await state_service.note_manual_intervention(
        line=req.line,
        actor=actor,
        target_softpoint=req.target_softpoint,
        value=req.value,
        reason=req.reason,
    )
    resp = await orchestrator.send_manual_setpoint(
        line=req.line,
        target_softpoint=req.target_softpoint,
        value=req.value,
        actor=actor,
        operator_id=req.operator_id,
        request_id=req.request_id,
        reason=req.reason,
        timeout_sec=req.timeout_sec,
        auto_reauthorization_required=(
            manual_state.get("previous_mode") in {"AUTO", "AUTO_PAUSED"}
        ),
    )
    await state_service.record_command_result(result=resp, command_scope="manual")
    if not resp.get("success") and resp.get("error") == "manual_command_not_actionable":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=resp)
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


@router.get("/status", dependencies=[Depends(require_readonly_http_source)])
async def get_control_status(
    line: Optional[str] = Query(None, description="可选：按产线过滤"),
    state_service: ControlStateService = Depends(get_control_state_service),
):
    return {"success": True, "data": await state_service.get_status(line=line)}


@router.post("/mode")
async def set_control_mode(
    req: ControlModeRequest,
    api_key: str = Depends(require_control_api_key),
    state_service: ControlStateService = Depends(get_control_state_service),
):
    actor = api_key_actor(api_key, role="control")
    requested_mode = req.mode.strip().upper()
    if requested_mode == "AUTO_PAUSED":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AUTO_PAUSED is not operator-selectable; authorize AUTO again after clearing the cause.",
        )
    if requested_mode == "AUTO":
        resp = await state_service.authorize_auto(line=req.line, actor=actor, reason=req.reason)
        if not resp.get("success"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=resp)
        return resp
    if requested_mode == "MANUAL":
        return await state_service.switch_manual(line=req.line, actor=actor, reason=req.reason)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mode must be MANUAL or AUTO")


@router.post("/auto/dispatch")
async def dispatch_auto_control(
    req: Optional[AutoDispatchRequest] = None,
    api_key: str = Depends(require_control_api_key),
    state_service: ControlStateService = Depends(get_control_state_service),
):
    body = req or AutoDispatchRequest()
    return await state_service.dispatch_auto(
        line=body.line,
        actor=api_key_actor(api_key, role="control"),
        lookback=body.lookback,
        timeout_sec=body.timeout_sec,
    )
