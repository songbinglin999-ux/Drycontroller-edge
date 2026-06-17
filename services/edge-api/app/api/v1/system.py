"""
系统管理 API
"""

# pyright: reportMissingImports=false

import json
import time
from typing import Optional
from pathlib import Path
from urllib.parse import quote

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.config import settings
from app.core.security import require_admin_api_key, require_readonly_http_source
from app.services.system_status import (
    SystemStatusService,
    get_system_status_service,
)
from app.services.health import get_system_readiness

router = APIRouter()
logger = structlog.get_logger(__name__)


def _status_level(status_value: str | None) -> str:
    status_text = (status_value or "").lower()
    if status_text in {"healthy", "ok", "pass", "ready", "running"}:
        return "ok"
    if status_text in {"degraded", "warning", "unknown"}:
        return "degraded"
    return "critical"


def _overall_level(items: list[dict]) -> str:
    levels = {str(item.get("status") or "unknown") for item in items}
    if "critical" in levels:
        return "critical"
    if "degraded" in levels:
        return "degraded"
    return "ok"


def _read_activation_state(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return str(data.get("state") or "").strip()


def _bootstrap_summary(
    sentinel_path: Path | None = None,
    activation_state_path: Path | None = None,
) -> dict:
    sentinel_path = sentinel_path or Path("/var/lib/drycontroller/.bootstrapped")
    activation_state_path = activation_state_path or Path("/var/lib/drycontroller/activation-state.json")
    if sentinel_path.is_file():
        return {
            "key": "bootstrap",
            "label": "装机",
            "status": "ok",
            "message": "已完成",
        }

    if _read_activation_state(activation_state_path) == "formal_license_active":
        return {
            "key": "bootstrap",
            "label": "装机",
            "status": "ok",
            "message": "正式授权已激活",
        }

    return {
        "key": "bootstrap",
        "label": "装机",
        "status": "degraded",
        "message": "未检测到完成标记",
    }


async def _license_summary() -> dict:
    url = f"{settings.LICENSE_SERVICE_BASE.rstrip('/')}/verify"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(url)
        body = resp.json() if resp.content else {}
        valid = bool(body.get("valid"))
        return {
            "key": "license",
            "label": "授权",
            "status": "ok" if resp.status_code == 200 and valid else "critical",
            "message": "有效" if valid else str(body.get("reason") or f"http {resp.status_code}"),
            "detail": body,
        }
    except (httpx.HTTPError, ValueError) as exc:  # pragma: no cover - depends on sibling service
        return {
            "key": "license",
            "label": "授权",
            "status": "critical",
            "message": "授权服务不可达",
            "detail": {"error": str(exc), "url": url},
        }


async def _cloud_summary() -> dict:
    base_url = (settings.CLOUD_OPS_BASE_URL or "").strip()
    if not base_url:
        return {
            "key": "cloud",
            "label": "云端",
            "status": "degraded",
            "message": "未配置云端地址，本地可继续运行",
        }
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/health")
        return {
            "key": "cloud",
            "label": "云端",
            "status": "ok" if resp.status_code < 400 else "degraded",
            "message": "在线" if resp.status_code < 400 else f"http {resp.status_code}",
            "detail": {"url": base_url, "status_code": resp.status_code},
        }
    except httpx.HTTPError as exc:  # pragma: no cover - depends on cloud connectivity
        return {
            "key": "cloud",
            "label": "云端",
            "status": "degraded",
            "message": "暂时离线，不影响本地运行",
            "detail": {"url": base_url, "error": str(exc)},
        }


def _plc_summary(status_payload: dict | None) -> dict:
    services = status_payload.get("services") if isinstance(status_payload, dict) else None
    edge_daq = None
    if isinstance(services, dict):
        edge_daq = services.get("edge-daq") or services.get("edge_daq")
    elif isinstance(services, list):
        edge_daq = next((item for item in services if item.get("name") in {"edge-daq", "edge_daq"}), None)

    if not isinstance(edge_daq, dict):
        return {
            "key": "plc",
            "label": "PLC",
            "status": "degraded",
            "message": "等待采集服务上报",
        }

    level = _status_level(str(edge_daq.get("status") or edge_daq.get("container_state") or "unknown"))
    checks = edge_daq.get("checks") if isinstance(edge_daq.get("checks"), dict) else {}
    plc_check = checks.get("plc") or checks.get("modbus") or checks.get("ready") or {}
    hint = plc_check.get("hint") or plc_check.get("detail") or edge_daq.get("detail")
    if level == "ok":
        message = "已连接"
    elif hint:
        message = str(hint)
    else:
        message = "连接异常，请检查 PLC 电源、网线和 IP 地址"
    return {
        "key": "plc",
        "label": "PLC",
        "status": level,
        "message": message,
        "detail": {"service": edge_daq},
    }


@router.get("/status", dependencies=[Depends(require_readonly_http_source)])
async def get_system_status(
    line: Optional[str] = Query(None, description="可选：按产线过滤，如 L1"),
    service: SystemStatusService = Depends(get_system_status_service),
):
    """
    获取系统状态（订阅 edge-monitor）
    """
    payload = service.get_status_payload(line)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No system status available yet",
        )
    return payload


@router.get("/metrics", dependencies=[Depends(require_readonly_http_source)])
async def get_system_metrics(
    line: Optional[str] = Query(None, description="可选：按产线过滤"),
    service: SystemStatusService = Depends(get_system_status_service),
):
    """
    获取系统性能指标
    """
    payload = service.get_metrics_payload(line)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No system metrics available yet",
        )
    return payload


@router.get("/local-summary", dependencies=[Depends(require_readonly_http_source)])
async def get_local_status_summary(
    line: Optional[str] = Query(None, description="可选：按产线过滤，如 L1"),
    service: SystemStatusService = Depends(get_system_status_service),
):
    """
    面向现场大屏的只读状态摘要。

    该接口刻意避免暴露容器、日志、配置等维护细节；维护动作仍走 /api/v1/ops/*
    并要求管理员 API Key。
    """
    readiness = await get_system_readiness()
    status_payload = service.get_status_payload(line)

    items = [
        {
            "key": "edge_api",
            "label": "系统",
            "status": "ok" if all(v.get("status") == "healthy" for v in readiness.values()) else "degraded",
            "message": "本机服务正常" if all(v.get("status") == "healthy" for v in readiness.values()) else "部分服务降级",
            "detail": readiness,
        },
        _plc_summary(status_payload),
        await _license_summary(),
        await _cloud_summary(),
        {
            "key": "tailnet",
            "label": "远程通道",
            "status": "ok" if (settings.TS_LOGIN_SERVER or "").strip() else "degraded",
            "message": "已配置" if (settings.TS_LOGIN_SERVER or "").strip() else "未配置",
            "detail": {
                "ts_login_server_present": bool((settings.TS_LOGIN_SERVER or "").strip()),
                "ts_hostname": (settings.TS_HOSTNAME or "").strip() or None,
            },
        },
        _bootstrap_summary(),
    ]
    level = _overall_level(items)
    title = {
        "ok": "系统正常",
        "degraded": "部分降级",
        "critical": "需要处理",
    }[level]
    next_action = {
        "ok": "无需处理",
        "degraded": "可继续生产，建议联系维护人员查看详情",
        "critical": "请通知维护人员处理，不要自行修改系统配置",
    }[level]
    return {
        "success": True,
        "level": level,
        "title": title,
        "items": items,
        "next_action": next_action,
        "line_id": line,
        "generated_at": int(time.time()),
    }


@router.get("/metrics/trend", dependencies=[Depends(require_readonly_http_source)])
async def get_system_metric_trend(
    field: str = Query("cpu_percent", description="指标字段，例如 cpu_percent"),
    period: str = Query("6h", description="时间范围，例如 6h/12h/1d"),
    window: str = Query("5m", description="聚合窗口，例如 1m/5m/1h"),
    line: Optional[str] = Query(None, description="可选：按产线过滤"),
    service: SystemStatusService = Depends(get_system_status_service),
):
    """
    获取系统指标趋势（来自 Influx 聚合）
    """
    try:
        return await service.get_metric_trend(
            field=field,
            period=period,
            window=window,
            line=line,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/container/logs")
async def get_container_logs(
    container: str = Query(..., description="容器名称"),
    lines: int = Query(200, description="日志行数", ge=1, le=2000),
    api_key: str = Depends(require_admin_api_key),
):
    """
    获取容器日志（代理 edge-monitor）
    """
    monitor_base = settings.MONITOR_API_BASE.rstrip("/")
    # container 作为 path segment 必须做 URL 编码，避免特殊字符导致路径错误/潜在路径穿越
    url = f"{monitor_base}/ops/logs/{quote(container, safe='')}"
    
    headers = {settings.API_KEY_HEADER: api_key} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params={"lines": lines}, headers=headers)
    except httpx.RequestError as exc:
        logger.error("monitor_log_proxy_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="edge-monitor unreachable",
        )
    
    if response.status_code >= 400:
        detail = None
        if response.headers.get("content-type", "").startswith("application/json"):
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
        if not detail:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail or "Failed to fetch logs")
    
    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="edge-monitor returned invalid payload",
        )
    if "success" not in payload:
        payload = {"success": True, "data": payload}
    return payload
