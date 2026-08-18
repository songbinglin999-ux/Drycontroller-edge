"""
运维控制相关 API。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import ipaddress
from typing import Any
from pathlib import Path

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# 协议 schema 已移到仓库根 common/schemas/cloud_to_edge.py（P1-8 phase B），
# 与 cloud-ops 仓库的同名文件保持 byte-identical 同步，CI hash 校验保障一致性。
# 详见 Drycontroller-cloudserver/docs/architecture/shared-schemas.md。
from common.schemas.cloud_to_edge import (
    HostReleaseStartRequest,
    ManualActionRequest,
    MonitorConfigUpdate,
    OtaStartRequest,
    PlcConfigApplyRequest,
    RegistryCredentialRefreshRequest,
    SelfHealToggleRequest,
    ServiceOverride,
)

from app.core.security import require_admin_api_key
from app.core.config import settings
from app.core.device_identity import DeviceIdentityUnavailable, load_runtime_identity
from app.services.monitor_config import MonitorConfigService, get_monitor_config_service
from app.services.monitor_ops import MonitorOpsClient, get_monitor_ops_client
from app.services.host_release_ops import HostReleaseOpsClient, get_host_release_ops_client
from app.services.health import get_system_readiness
from app.services.ota_ops import OtaOpsClient, get_ota_ops_client
from app.services.registry_credentials import (
    install_registry_credential,
    normalize_registry_credential,
    registry_credential_check,
)
from app.services.registry_auth_writer import (
    RegistryAuthWriterClient,
    get_registry_auth_writer_client,
)
from app.services.audit import api_key_actor, write_audit
from app.services.config_version import record_config_version
from app.services.ota_history import record_ota_operation
from app.services.preflight_history import record_preflight_run
from common.tailscale_status import validate_tailscale_status

# 运维接口必须走管理员鉴权：这些接口包含重启/修改自愈等高危动作
# 注意：这里需要拿到实际 api_key 以便代理调用 edge-monitor 的 /ops/*
router = APIRouter()
PLC_CONFIG_RUNTIME_PATH = Path(os.getenv("PLC_CONFIG_RUNTIME_PATH", "/app/configs-runtime/plc.yaml"))


class PreflightRequest(BaseModel):
    """
    云端触发的边端只读出厂检查。

    该接口不执行宿主机 shell 脚本；它在 edge-api 容器内做等价的远程可观测检查，
    保证 cloud-ops 可以无人值守触发并拿到结构化结果。
    """

    strict: bool = Field(False, description="true 时 degraded 也视为失败")
    check_ota: bool = Field(False, description="true 时要求 root-owned OTA helper 可用")


@router.get("/ready")
async def authenticated_readiness(_: str = Depends(require_admin_api_key)):
    report = await get_system_readiness()
    healthy = all(dependency.get("status") == "healthy" for dependency in report.values())
    return JSONResponse(
        status_code=200 if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "success": healthy,
            "status": "ready" if healthy else "degraded",
            "dependencies": report,
            "timestamp": int(time.time()),
        },
    )


def _service_override_payload(o: ServiceOverride) -> dict:
    """
    把共享 schema ServiceOverride 转换为 edge-monitor 内部 monitor_config 的
    payload 形状。这是 edge-api 私有的实现细节（不是协议契约的一部分），
    所以不放进 common/schemas/。
    """
    payload: dict = {}
    if o.self_heal_enabled is not None:
        payload.setdefault("self_heal", {})
        payload["self_heal"]["enabled"] = o.self_heal_enabled
    if o.failure_threshold is not None:
        payload.setdefault("self_heal", {})
        payload["self_heal"]["failure_threshold"] = o.failure_threshold
    if o.max_restart_per_hour is not None:
        payload.setdefault("self_heal", {})
        payload["self_heal"]["max_restart_per_hour"] = o.max_restart_per_hour
    payload["name"] = o.name
    return payload


def _atomic_write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{int(time.time())}")
    with tmp.open("w", encoding="utf-8") as fp:
        yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def _write_bytes_atomic(path: Path, data: bytes, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{int(time.time() * 1000)}")
    try:
        with tmp.open("wb") as fp:
            fp.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _restore_or_remove(path: Path, old_data: bytes | None, mode: int = 0o640) -> None:
    if old_data is None:
        path.unlink(missing_ok=True)
        return
    _write_bytes_atomic(path, old_data, mode=mode)


async def _call_license_verify() -> dict:
    url = f"{settings.LICENSE_SERVICE_BASE.rstrip('/')}/verify"
    try:
        async with httpx.AsyncClient(timeout=8) as hc:
            resp = await hc.get(url)
        body = resp.json() if resp.content else {}
        return {"url": url, "status_code": resp.status_code, "body": body}
    except (httpx.HTTPError, ValueError) as exc:
        return {"url": url, "status_code": 0, "error": str(exc)}


def _license_verify_ok(result: dict) -> bool:
    body = result.get("body")
    return int(result.get("status_code") or 0) == 200 and isinstance(body, dict) and body.get("valid") is True


def _preflight_check(checks: list[dict[str, Any]], check_id: str, status: str, reason: str, hint: str = "", detail: dict[str, Any] | None = None) -> None:
    checks.append(
        {
            "id": check_id,
            "status": status,
            "reason": reason,
            "hint": hint,
            "detail": detail or {},
        }
    )


def _preflight_file_check(checks: list[dict[str, Any]], check_id: str, path: Path, *, hint: str) -> None:
    if path.is_file():
        _preflight_check(checks, check_id, "pass", "ok", detail={"path": str(path)})
    else:
        _preflight_check(checks, check_id, "fail", "missing_file", hint=hint, detail={"path": str(path)})


def _preflight_device_identity(checks: list[dict[str, Any]]) -> None:
    path = Path(os.getenv("PREFLIGHT_DEVICE_JSON", "/var/lib/drycontroller/device.json"))
    try:
        data = load_runtime_identity(path, require_root_owner=os.geteuid() == 0)
    except DeviceIdentityUnavailable as exc:
        _preflight_check(
            checks,
            "device.identity",
            "fail",
            "identity_unavailable",
            hint="complete per-device identity activation or rotate the expired credential",
            detail={"path": str(path), "error": str(exc)},
        )
        return
    _preflight_check(
        checks,
        "device.identity",
        "pass",
        "active",
        detail={
            "path": str(path),
            "device_id": data.get("device_id"),
            "tenant_id": data.get("tenant_id"),
            "node_name": data.get("node_name"),
            "credential_expires_at": data.get("credential_expires_at"),
        },
    )


def _preflight_registry_credential(checks: list[dict[str, Any]]) -> None:
    result = registry_credential_check()
    _preflight_check(
        checks,
        "registry.credential",
        "pass" if result["ok"] else "fail",
        result["reason"],
        hint="run Cloud Ops registry credential refresh before online OTA" if not result["ok"] else "",
        detail=result["detail"],
    )


async def _preflight_tailnet(checks: list[dict[str, Any]], *, allow_degraded: bool) -> dict[str, Any]:
    try:
        identity = load_runtime_identity(require_root_owner=os.geteuid() == 0)
        expected_hostname = str(identity.get("node_name") or "")
        expected_node_id = str(identity.get("tailnet_node_id") or "")
    except DeviceIdentityUnavailable:
        expected_hostname = ""
        expected_node_id = ""
    try:
        observation = await get_host_release_ops_client().get_tailnet_status()
    except HTTPException:
        observation = {"ok": False, "reason": "tailnet_observer_unavailable", "detail": {}}
    result = validate_tailscale_status(
        observation,
        expected_version=(settings.TAILSCALE_EXPECTED_VERSION or "").strip(),
        expected_login_server=(settings.TS_LOGIN_SERVER or "").strip(),
        expected_hostname=expected_hostname,
        expected_node_id=expected_node_id,
        require_edge_api_ingress=True,
    )
    passed = result["ok"] is True
    _preflight_check(
        checks,
        "tailnet.identity",
        "pass" if passed else ("degraded" if allow_degraded else "fail"),
        str(result["reason"]),
        hint="restore the pinned tailscaled client and original Device-bound node identity",
        detail=result["detail"],
    )
    return result


async def _preflight_cloud_application_path(
    checks: list[dict[str, Any]],
    *,
    tailnet: dict[str, Any],
    allow_degraded: bool,
) -> None:
    cloud_ops_base_url = (settings.CLOUD_OPS_BASE_URL or "").strip()
    try:
        identity = load_runtime_identity(require_root_owner=os.geteuid() == 0)
    except DeviceIdentityUnavailable:
        identity = None
    detail = tailnet.get("detail") if isinstance(tailnet.get("detail"), dict) else {}
    addresses = detail.get("tailscale_ips") if isinstance(detail, dict) else None
    tailnet_address = ""
    for raw_address in addresses if isinstance(addresses, list) else []:
        try:
            address = ipaddress.ip_address(str(raw_address))
        except ValueError:
            continue
        if address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10"):
            tailnet_address = str(address)
            break
    if not cloud_ops_base_url or not identity or not tailnet_address or tailnet.get("ok") is not True:
        _preflight_check(
            checks,
            "tailnet.application_e2e",
            "degraded" if allow_degraded else "fail",
            "cloud_application_probe_prerequisite_missing",
            hint="restore the bound Tailnet identity, Cloud TLS route, and per-device credential",
            detail={
                "cloud_ops_base_url_present": bool(cloud_ops_base_url),
                "device_identity_present": bool(identity),
                "tailnet_address_present": bool(tailnet_address),
            },
        )
        return
    system_status = base64.b64encode(
        json.dumps({"preflight": "rb7i-application-e2e"}, separators=(",", ":")).encode()
    ).decode("ascii")
    request_body = {
        "name": identity["node_name"],
        "address": tailnet_address,
        "edge_api_port": 8000,
        "line_id": identity["line_id"],
        "system_status_b64": system_status,
        "meta": {"preflight": "rb7i-application-e2e"},
        "verify_cloud_to_edge_ready": True,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{cloud_ops_base_url.rstrip('/')}/api/v1/edge-status",
                json=request_body,
                headers={"X-Device-Credential": identity["runtime_credential"]},
            )
        response_body = response.json() if response.content else None
    except (httpx.HTTPError, ValueError) as exc:
        _preflight_check(
            checks,
            "tailnet.application_e2e",
            "degraded" if allow_degraded else "fail",
            "cloud_application_probe_unreachable",
            hint="verify Edge-to-Cloud TLS ingress and the Cloud signed readiness callback",
            detail={"error": str(exc)},
        )
        return
    callback = response_body.get("cloud_to_edge_ready") if isinstance(response_body, dict) else None
    callback_body = callback.get("json") if isinstance(callback, dict) else None
    passed = (
        response.status_code == 200
        and isinstance(response_body, dict)
        and response_body.get("success") is True
        and isinstance(callback, dict)
        and callback.get("status_code") == 200
        and isinstance(callback_body, dict)
        and callback_body.get("success") is True
    )
    _preflight_check(
        checks,
        "tailnet.application_e2e",
        "pass" if passed else ("degraded" if allow_degraded else "fail"),
        "ok" if passed else "cloud_application_probe_failed",
        hint="verify Edge-to-Cloud TLS ingress and the Cloud signed readiness callback" if not passed else "",
        detail={
            "cloud_status_code": response.status_code,
            "cloud_to_edge_status_code": callback.get("status_code") if isinstance(callback, dict) else None,
        },
    )


async def _preflight_http_check(
    checks: list[dict[str, Any]],
    check_id: str,
    url: str,
    *,
    degraded_ok: bool,
    allow_degraded: bool,
    timeout: float = 5.0,
) -> None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as hc:
            resp = await hc.get(url)
        body: Any
        try:
            body = resp.json() if resp.content else None
        except ValueError:
            body = resp.text[:800]
        detail = {"url": url, "status_code": resp.status_code, "body": body}
        if 200 <= resp.status_code < 300:
            _preflight_check(checks, check_id, "pass", "ok", detail=detail)
        elif resp.status_code == 503 and degraded_ok and allow_degraded:
            _preflight_check(checks, check_id, "degraded", "service_degraded", hint="accepted in factory mode; use strict for final acceptance", detail=detail)
        else:
            _preflight_check(checks, check_id, "fail", f"http_{resp.status_code}", hint="unexpected readiness status", detail=detail)
    except httpx.HTTPError as exc:
        _preflight_check(checks, check_id, "fail", "unreachable", hint="endpoint is not reachable", detail={"url": url, "error": str(exc)})


async def _preflight_ota_helper(checks: list[dict[str, Any]]) -> None:
    try:
        body = await get_ota_ops_client().ready()
    except HTTPException as exc:
        _preflight_check(
            checks,
            "ota_helper.ready",
            "fail",
            "unreachable",
            hint="install and start drycontroller-ota-helper.service",
            detail={"status_code": exc.status_code, "body": exc.detail},
        )
        return
    ready = body.get("ready") is True
    _preflight_check(
        checks,
        "ota_helper.ready",
        "pass" if ready else "fail",
        "ok" if ready else "not_ready",
        hint="drycontroller-ota-helper.service must report ready=true" if not ready else "",
        detail={"body": body},
    )


def _preflight_summary(checks: list[dict[str, Any]], *, strict: bool) -> dict[str, Any]:
    counts = {"pass": 0, "degraded": 0, "fail": 0}
    for check in checks:
        status_text = str(check.get("status") or "")
        if status_text in counts:
            counts[status_text] += 1
    status_text = "fail" if counts["fail"] or (strict and counts["degraded"]) else ("degraded" if counts["degraded"] else "pass")
    return {
        "success": status_text != "fail",
        "status": status_text,
        "summary": counts,
    }


@router.get("/alerts")
async def list_alerts(
    severity: str | None = None,
    service: str | None = None,
    state: str | None = "active",
    client: MonitorOpsClient = Depends(get_monitor_ops_client),
    api_key: str = Depends(require_admin_api_key),
):
    """
    代理 edge-monitor 的 /ops/alerts，供前端展示告警。
    """
    return await client.list_alerts(severity=severity, service=service, state=state, api_key=api_key)


# ==================== License 只读校验 ====================


@router.get("/license/verify")
async def verify_license(_: str = Depends(require_admin_api_key)):
    """
    代理 license-service /verify，供云端安装前后读取边端真实验签状态。
    """
    result = await _call_license_verify()
    return {
        "success": _license_verify_ok(result),
        "verify": result,
    }


@router.post("/preflight")
async def run_preflight(req: PreflightRequest, _: str = Depends(require_admin_api_key)):
    """
    云端触发的只读 preflight。

    本地工厂位仍可直接运行 scripts/preflight.sh；云端 job 使用本接口拿结构化结果。
    """
    checks: list[dict[str, Any]] = []
    allow_degraded = not req.strict

    _preflight_device_identity(checks)
    _preflight_file_check(
        checks,
        "license.file",
        Path(settings.LICENSE_FILE_PATH),
        hint="install signed license before shipping",
    )
    _preflight_file_check(
        checks,
        "license.trust_policy",
        Path(settings.LICENSE_TRUST_POLICY_PATH),
        hint="initialize the canonical license trust policy before shipping",
    )

    verify_result = await _call_license_verify()
    _preflight_check(
        checks,
        "license.verify",
        "pass" if _license_verify_ok(verify_result) else "fail",
        "ok" if _license_verify_ok(verify_result) else "license_verify_failed",
        hint="license-service /verify must return valid=true",
        detail=verify_result,
    )

    _preflight_check(checks, "edge_api.ready", "pass", "ok", detail={"source": "preflight endpoint reached"})
    await _preflight_http_check(
        checks,
        "edge_daq.ready",
        "http://edge-daq:8080/ready",
        degraded_ok=True,
        allow_degraded=allow_degraded,
    )
    await _preflight_http_check(
        checks,
        "edge_monitor.ready",
        f"{settings.MONITOR_API_BASE.rstrip('/')}/ready",
        degraded_ok=False,
        allow_degraded=allow_degraded,
    )
    if req.check_ota:
        await _preflight_ota_helper(checks)
        _preflight_registry_credential(checks)
    else:
        _preflight_check(
            checks,
            "ota_helper.ready",
            "degraded" if allow_degraded else "fail",
            "skipped",
            hint="root OTA helper check skipped; set check_ota=true for final OTA acceptance",
        )
        _preflight_check(
            checks,
            "registry.credential",
            "degraded" if allow_degraded else "fail",
            "skipped",
            hint="registry credential check skipped; set check_ota=true before online OTA",
        )

    tailnet = await _preflight_tailnet(checks, allow_degraded=allow_degraded)
    await _preflight_cloud_application_path(
        checks,
        tailnet=tailnet,
        allow_degraded=allow_degraded,
    )

    summary = _preflight_summary(checks, strict=req.strict)
    payload = {
        **summary,
        "checks": checks,
        "strict": req.strict,
        "check_ota": req.check_ota,
        "generated_at": int(time.time()),
    }
    await record_preflight_run(payload)
    if not payload["success"]:
        raise HTTPException(status_code=409, detail=payload)
    return payload


@router.get("/registry-credentials/status")
async def get_registry_credential_status(_: str = Depends(require_admin_api_key)):
    result = registry_credential_check()
    return {"success": result["ok"], "reason": result["reason"], "credential": result["detail"]}


@router.post("/registry-credentials/refresh")
async def refresh_registry_credential(
    req: RegistryCredentialRefreshRequest,
    api_key: str = Depends(require_admin_api_key),
    writer: RegistryAuthWriterClient = Depends(get_registry_auth_writer_client),
):
    payload = req.model_dump()
    try:
        normalized = normalize_registry_credential(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"success": False, "error": str(exc)}) from exc
    root_status = await writer.install(payload)
    compared = ("credential_id", "registry_host", "username", "secret_fingerprint", "version")
    if any(root_status.get(key) != normalized.get(key) for key in compared):
        raise HTTPException(
            status_code=502,
            detail={"error": "registry_auth_writer_binding_mismatch"},
        )
    status_payload = install_registry_credential(normalized)

    await write_audit(
        actor=api_key_actor(api_key, role="admin"),
        action="registry_credential.refresh",
        resource_type="registry_credential",
        resource_id=req.credential_id,
        result="success",
        detail={
            "registry_host": status_payload["registry_host"],
            "username": status_payload["username"],
            "version": status_payload["version"],
            "secret_fingerprint": status_payload["secret_fingerprint"],
        },
    )
    return {"success": True, "credential": status_payload}


@router.get("/monitor-config")
async def get_monitor_config(
    service: MonitorConfigService = Depends(get_monitor_config_service),
    _: str = Depends(require_admin_api_key),
):
    try:
        return service.load()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="monitor config file not found")


@router.put("/monitor-config")
async def update_monitor_config(
    payload: MonitorConfigUpdate,
    service: MonitorConfigService = Depends(get_monitor_config_service),
    _: str = Depends(require_admin_api_key),
):
    updates = payload.model_dump(exclude_none=True)
    if "targets" in updates:
        updates["targets"] = [_service_override_payload(o) for o in payload.targets or []]
    patched = service.patch(updates)
    return {"success": True, "config": patched}


@router.post("/actions/restart")
async def manual_restart(
    req: ManualActionRequest,
    client: MonitorOpsClient = Depends(get_monitor_ops_client),
    api_key: str = Depends(require_admin_api_key),
):
    result = await client.restart_service(req.service, api_key=api_key)
    return result


@router.post("/actions/self-heal-toggle")
async def manual_toggle_self_heal(
    req: SelfHealToggleRequest,
    client: MonitorOpsClient = Depends(get_monitor_ops_client),
    api_key: str = Depends(require_admin_api_key),
):
    result = await client.toggle_self_heal(req.service, req.enabled, api_key=api_key)
    return result


# ==================== OTA（root-owned helper 执行，edge-api 只代理 operation ID） ====================


@router.post("/ota/start")
async def ota_start(
    req: OtaStartRequest,
    client: OtaOpsClient = Depends(get_ota_ops_client),
    _: str = Depends(require_admin_api_key),
):
    credential = registry_credential_check()
    if not credential["ok"]:
        raise HTTPException(
            status_code=409,
            detail={"success": False, "error": credential["reason"], "credential": credential["detail"]},
        )
    result = await client.start(req.operation_id)
    await record_ota_operation(result, request_payload={"operation_id": req.operation_id})
    return result


@router.get("/ota/operations/{operation_id}")
async def ota_get_operation(
    operation_id: str,
    client: OtaOpsClient = Depends(get_ota_ops_client),
    _: str = Depends(require_admin_api_key),
):
    result = await client.get_operation(operation_id)
    await record_ota_operation(result)
    return result


@router.get("/ota/operations/{operation_id}/logs")
async def ota_get_logs(
    operation_id: str,
    client: OtaOpsClient = Depends(get_ota_ops_client),
    _: str = Depends(require_admin_api_key),
):
    return await client.get_logs(operation_id)


# ==================== HostRelease（immutable runner，仅接收 operation ID） ====================


@router.post("/host-release/start")
async def host_release_start(
    req: HostReleaseStartRequest,
    client: HostReleaseOpsClient = Depends(get_host_release_ops_client),
    _: str = Depends(require_admin_api_key),
):
    credential = registry_credential_check()
    if not credential["ok"]:
        raise HTTPException(
            status_code=409,
            detail={"success": False, "error": credential["reason"], "credential": credential["detail"]},
        )
    return await client.start(req.operation_id)


@router.get("/host-release/operations/{operation_id}")
async def host_release_get_operation(
    operation_id: str,
    client: HostReleaseOpsClient = Depends(get_host_release_ops_client),
    _: str = Depends(require_admin_api_key),
):
    return await client.get_operation(operation_id)


@router.get("/host-release/operations/{operation_id}/logs")
async def host_release_get_logs(
    operation_id: str,
    client: HostReleaseOpsClient = Depends(get_host_release_ops_client),
    _: str = Depends(require_admin_api_key),
):
    return await client.get_logs(operation_id)


# ==================== PLC 通讯参数下发（不改网卡，仅改应用层） ====================


@router.post("/plc-config/apply")
async def apply_plc_config(
    req: PlcConfigApplyRequest,
    client: MonitorOpsClient = Depends(get_monitor_ops_client),
    api_key: str = Depends(require_admin_api_key),
):
    """
    云端下发 PLC 通讯参数：
    1) 写入 /app/configs-runtime/plc.yaml（bind mount 到宿主机 ./configs-runtime）
    2) 可选：通过 edge-monitor 重启 edge-daq 使其加载新配置
    3) 可选：读取 edge-daq /ready 返回（用于云端展示 PLC_OFFLINE 等原因）
    """
    if req.plc_host.strip() in {"127.0.0.1", "localhost"}:
        # 工业现场必坑：容器内 127.0.0.1 指向自身，必然连不上 PLC
        raise HTTPException(status_code=400, detail="PLC_HOST cannot be 127.0.0.1/localhost in docker; use real PLC IP")

    cfg_path = PLC_CONFIG_RUNTIME_PATH
    payload = {
        "host": req.plc_host.strip(),
        "port": int(req.plc_port),
        "slave_id": int(req.plc_slave_id),
        "connect_timeout": int(req.plc_connect_timeout),
        "updated_at": int(time.time()),
        "updated_by": "cloud",
    }
    previous_cfg = cfg_path.read_bytes() if cfg_path.exists() else None
    _atomic_write_yaml(cfg_path, payload)

    restart_result = None
    if req.restart_edge_daq:
        try:
            restart_result = await client.restart_service("edge-daq", api_key=api_key)
        except (httpx.HTTPError, RuntimeError, ValueError):
            _restore_or_remove(cfg_path, previous_cfg)
            raise

    await record_config_version(
        scope="plc",
        payload=payload,
        source="cloud",
        applied_by=api_key_actor(api_key, role="admin"),
        restart_required=not req.restart_edge_daq,
    )

    ready_result: dict | None = None
    if req.read_ready_after_restart:
        # 给容器一点启动时间（避免刚重启就探测）
        await asyncio.sleep(2)
        url = "http://edge-daq:8080/ready"
        try:
            async with httpx.AsyncClient(timeout=6) as hc:
                resp = await hc.get(url)
                ready_result = {
                    "url": url,
                    "status_code": resp.status_code,
                    "json": resp.json() if resp.content else None,
                    "text": (resp.text[:1000] if resp.text else None),
                }
        except (httpx.HTTPError, ValueError) as exc:
            ready_result = {"url": url, "status_code": 0, "error": str(exc)}

    if req.fail_on_not_ready and ready_result and int(ready_result.get("status_code") or 0) >= 400:
        raise HTTPException(status_code=409, detail={"success": False, "error": "edge_daq_not_ready", "ready": ready_result})

    return {
        "success": True,
        "config_path": str(cfg_path),
        "config": payload,
        "restart": restart_result,
        "edge_daq_ready": ready_result,
    }
