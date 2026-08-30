"""
授权服务
"""

import json
import os
from pathlib import Path

from fastapi import FastAPI, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
import structlog

from common.logging import setup_logging
from license.license_verifier import (
    load_active_license_trust,
    load_json,
    verify_license as verify_signed_license,
)

event_recorder = setup_logging("license-service")
logger = structlog.get_logger(__name__)

app = FastAPI(title="License Service")


def _license_enabled() -> bool:
    return os.getenv("LICENSE_CHECK_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def _license_file_path() -> Path:
    return Path(os.getenv("LICENSE_FILE_PATH", "/etc/drycontroller/license/license.json"))


def _license_trust_policy_path() -> Path:
    return Path(
        os.getenv(
            "LICENSE_TRUST_POLICY_PATH",
            "/etc/drycontroller/keys/license-trust/policy.json",
        )
    )


def _result(valid: bool, reason: str, license_payload=None):
    payload = {"success": True, "valid": valid, "reason": reason, "license": license_payload}
    event_recorder.record_event(
        "license_verified",
        tags={"component": "license-service"},
        fields={"valid": valid, "reason": reason},
    )
    return payload


@app.on_event("startup")
async def startup():
    logger.info("license_service_starting")
    event_recorder.record_event(
        "service_starting",
        tags={"component": "license-service"},
    )
    logger.info("license_service_started")
    event_recorder.record_event(
        "service_started",
        tags={"component": "license-service"},
    )


@app.on_event("shutdown")
async def shutdown():
    logger.info("license_service_stopping")
    event_recorder.record_event(
        "service_stopping",
        tags={"component": "license-service"},
    )
    logger.info("license_service_stopped")
    event_recorder.record_event(
        "service_stopped",
        tags={"component": "license-service"},
    )

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/ready")
async def ready():
    # 当前授权服务未强依赖外部资源，readiness 等价于存活。
    return {"ready": True}

@app.get("/metrics")
async def metrics():
    # Prometheus exposition format. Phase 1 only exposes prometheus_client's
    # default process / GC metrics; service-level Counter/Gauge can be added later.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/verify")
async def verify_license():
    if not _license_enabled():
        return _result(True, "license_disabled", None)

    license_path = _license_file_path()
    trust_policy_path = _license_trust_policy_path()
    if not license_path.is_file():
        return _result(False, "missing_license_file", None)
    if not trust_policy_path.is_file():
        return _result(False, "missing_trust_policy", None)

    try:
        payload = load_json(license_path)
        trust = load_active_license_trust(trust_policy_path)
        verify_signed_license(payload, trust)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("license_verify_failed", reason="signature_invalid", error=str(exc))
        return _result(False, "signature_invalid", None)

    return _result(False, "legacy_ineligible", None)
