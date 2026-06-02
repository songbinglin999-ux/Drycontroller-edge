"""
Factory activation agent.

Handles the bootstrap tailnet -> Cloud Ops -> formal license/key flow.
Runs as a background service on first boot, or can be triggered manually.

State machine:
  factory_installed -> bootstrap_tailnet_joined -> pending_ops_approval -> cloud_activated -> formal_tailnet_joined -> formal_license_active
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import shlex
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

from app.core.config import settings


logger = structlog.get_logger(__name__)


class ActivationState(str, Enum):
    FACTORY_INSTALLED = "factory_installed"
    BOOTSTRAP_TAILNET_JOINED = "bootstrap_tailnet_joined"
    PENDING_OPS_APPROVAL = "pending_ops_approval"
    CLOUD_ACTIVATED = "cloud_activated"
    FORMAL_TAILNET_JOINED = "formal_tailnet_joined"
    FORMAL_LICENSE_ACTIVE = "formal_license_active"


# Paths
_STATE_FILE = Path("/var/lib/drycontroller/activation-state.json")
_FACTORY_LICENSE_PATH = Path("/etc/drycontroller/license/license.json")
_FACTORY_LICENSE_ARCHIVE_PATH = Path("/var/lib/drycontroller/factory-license.json")
_FACTORY_LICENSE_PUB_PATH = Path("/etc/drycontroller/keys/license.pub")
_PRODUCTION_LICENSE_PATH = Path("/etc/drycontroller/license/license.json")
_PRODUCTION_LICENSE_PUB_PATH = Path("/etc/drycontroller/keys/license.pub")
_DEVICE_JSON_PATH = Path("/var/lib/drycontroller/device.json")
_TAILNET_ENV_PATH = Path(os.getenv("DRYCONTROLLER_TAILNET_ENV", "/etc/drycontroller/tailnet.env"))
_BOOTSTRAP_SENTINEL = Path("/var/lib/drycontroller/.bootstrapped")
_TAILSCALE_CONFIG_DIR = Path("/var/lib/tailscale")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _license_fingerprint(license_path: Path, machine_id: str) -> str:
    """Server-compatible activation fingerprint: factory license + machine_id."""
    if not license_path.is_file():
        return ""
    payload = _read_json(license_path)
    if not payload or not machine_id:
        return ""
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    h = hashlib.sha256()
    h.update(raw)
    h.update(b"\0")
    h.update(machine_id.encode("utf-8"))
    return h.hexdigest()


def _factory_license_source_path() -> Path:
    return _FACTORY_LICENSE_ARCHIVE_PATH if _FACTORY_LICENSE_ARCHIVE_PATH.is_file() else _FACTORY_LICENSE_PATH


def _ensure_factory_license_archive() -> None:
    if _FACTORY_LICENSE_ARCHIVE_PATH.is_file():
        return
    payload = _read_json(_FACTORY_LICENSE_PATH)
    if not payload or not payload.get("signature"):
        raise RuntimeError("factory license is not a signed license JSON")
    if payload.get("schema") != "drycontroller.factory-license.v1":
        raise RuntimeError("license JSON is not a factory license")
    _write_json(_FACTORY_LICENSE_ARCHIVE_PATH, payload)
    os.chmod(_FACTORY_LICENSE_ARCHIVE_PATH, 0o600)


def _machine_id() -> str:
    """Read /etc/machine-id."""
    try:
        return Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _read_state() -> dict[str, Any]:
    return _read_json(_STATE_FILE)


def _write_state(data: dict[str, Any]) -> None:
    _write_json(_STATE_FILE, data)


def get_activation_state() -> dict[str, Any]:
    """Return current activation state + metadata."""
    state_data = _read_state()
    current_state = state_data.get("state", ActivationState.FACTORY_INSTALLED.value)

    factory_license_source = _factory_license_source_path()
    factory_license_exists = factory_license_source.is_file()
    factory_pub_exists = _FACTORY_LICENSE_PUB_PATH.is_file()
    device_json_exists = _DEVICE_JSON_PATH.is_file()
    tailscale_state_exists = (_TAILSCALE_CONFIG_DIR / "tailscaled.state").is_file()

    return {
        "state": current_state,
        "factory_license_exists": factory_license_exists,
        "factory_pub_exists": factory_pub_exists,
        "device_json_exists": device_json_exists,
        "tailscale_state_exists": tailscale_state_exists,
        "machine_id": _machine_id(),
        "factory_license_fingerprint": _license_fingerprint(factory_license_source, _machine_id()) if factory_license_exists else None,
        "cloud_ops_base_url": settings.CLOUD_OPS_BASE_URL or None,
        "ts_login_server": settings.TS_LOGIN_SERVER or None,
        **{k: v for k, v in state_data.items() if k != "state"},
    }


def _run_cmd(argv: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    logger.info("running_cmd", argv=argv)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


async def _wait_for_tailnet(timeout_sec: int = 120) -> bool:
    """Wait until tailscale is up and has an IP."""
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=timeout_sec)
    while dt.datetime.now(dt.timezone.utc) < deadline:
        result = _run_cmd(["tailscale", "status", "--json"], timeout=5)
        if result.returncode == 0:
            try:
                status = json.loads(result.stdout)
                if status.get("BackendState") == "Running" and status.get("Self", {}).get("TailscaleIPs"):
                    logger.info("tailnet_ready", ips=status["Self"]["TailscaleIPs"])
                    return True
            except Exception:
                pass
        await asyncio.sleep(3)
    logger.error("tailnet_timeout", timeout_sec=timeout_sec)
    return False


async def _call_cloud_activation(
    *,
    factory_license: dict[str, Any],
    factory_license_fingerprint: str,
    machine_id: str,
    bootstrap_ts_node: str,
    bootstrap_ts_ip: Optional[str],
    requested_tenant_id: Optional[str],
    requested_line_id: Optional[str],
) -> dict[str, Any]:
    """Call Cloud Ops factory activation endpoint over tailnet."""
    cloud_url = settings.CLOUD_OPS_BASE_URL.rstrip("/")
    url = f"{cloud_url}/api/v1/factory-activation"

    payload = {
        "factory_license": factory_license,
        "factory_license_fingerprint": factory_license_fingerprint,
        "machine_id": machine_id,
        "bootstrap_ts_node": bootstrap_ts_node,
        "bootstrap_ts_ip": bootstrap_ts_ip,
        "requested_tenant_id": requested_tenant_id,
        "requested_line_id": requested_line_id,
    }

    logger.info("calling_cloud_activation", url=url, fingerprint=factory_license_fingerprint[:16])
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def _reissue_ts_key(
    fingerprint: str, factory_license: dict[str, Any], machine_id: str
) -> Optional[dict[str, Any]]:
    """Request a new TS auth key from Cloud Ops (when previous one was consumed)."""
    cloud_url = settings.CLOUD_OPS_BASE_URL.rstrip("/")
    url = f"{cloud_url}/api/v1/factory-activation/{fingerprint}/reissue-ts-key"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json={"factory_license": factory_license, "machine_id": machine_id})
            if resp.status_code == 200:
                return resp.json()
            logger.warning("reissue_ts_key_failed", status=resp.status_code, body=resp.text[:200])
            return None
    except Exception as exc:
        logger.warning("reissue_ts_key_error", error=str(exc))
        return None


async def _confirm_formal_tailnet(
    fingerprint: str,
    factory_license: dict[str, Any],
    machine_id: str,
) -> bool:
    cloud_url = settings.CLOUD_OPS_BASE_URL.rstrip("/")
    url = f"{cloud_url}/api/v1/factory-activation/{fingerprint}/confirm-formal-tailnet"
    ts_ip = _run_cmd(["tailscale", "ip", "-4"], timeout=5).stdout.splitlines()
    payload = {
        "factory_license": factory_license,
        "machine_id": machine_id,
        "formal_ts_ip": ts_ip[0].strip() if ts_ip else None,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            logger.info("formal_tailnet_confirmed_with_cloud", fingerprint=fingerprint[:16])
            return True
    except Exception as exc:
        logger.warning("formal_tailnet_confirm_failed", error=str(exc))
        return False


def _production_trust_anchor() -> Optional[bytes]:
    """Pinned production-license trust anchor (base64 LICENSE_TRUSTED_PUBLIC_KEY),
    consistent with edge-api's license-install path (ops.py `_trusted_license_public_key`).
    Returns None when no anchor is pinned, in which case _install_license falls back to
    the activation response key (logged as a downgrade)."""
    import base64 as _b64
    configured = str(settings.LICENSE_TRUSTED_PUBLIC_KEY or "").strip()
    if not configured:
        return None
    try:
        return _b64.b64decode(configured, validate=True)
    except Exception as exc:
        raise RuntimeError(f"invalid LICENSE_TRUSTED_PUBLIC_KEY: {exc}") from exc


def _install_license(license_payload: dict[str, Any], public_key: str) -> None:
    """Verify the production license against the pinned trust anchor, then install it.

    When LICENSE_TRUSTED_PUBLIC_KEY is pinned, the activation response's public key is
    NOT trusted as the verification key — only cross-checked. With no anchor pinned,
    verification falls back to the response key (logged as a downgrade); production
    deploys must pin the anchor so a forged activation response cannot self-sign."""
    import base64 as _b64
    from common.license_verifier import verify_signature

    response_pub = public_key.strip()
    anchor = _production_trust_anchor()
    if anchor is not None:
        verify_bytes = anchor
        try:
            if response_pub and _b64.b64decode(response_pub, validate=True) != anchor:
                logger.warning("license_pubkey_response_differs_from_anchor_using_anchor")
        except Exception:
            logger.warning("license_pubkey_response_undecodable_using_anchor")
    else:
        if not response_pub:
            raise RuntimeError("no license public key: trust anchor unpinned and response key empty")
        logger.warning("license_pubkey_unpinned_trusting_activation_response")
        verify_bytes = _b64.b64decode(response_pub, validate=True)

    try:
        verify_signature(license_payload, verify_bytes)
    except Exception as exc:
        logger.error("license_signature_verification_failed", error=str(exc))
        raise RuntimeError(f"license signature verification failed: {exc}") from exc

    _ensure_factory_license_archive()
    _write_json(_PRODUCTION_LICENSE_PATH, license_payload)
    _PRODUCTION_LICENSE_PUB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Persist the key actually used for verification so edge-api enforcement reads the
    # same anchor: the pinned key when present, else the response key (legacy behaviour).
    persisted_pub = str(settings.LICENSE_TRUSTED_PUBLIC_KEY or "").strip() if anchor is not None else response_pub
    _PRODUCTION_LICENSE_PUB_PATH.write_text(persisted_pub + "\n", encoding="utf-8")
    logger.info("license_installed", path=str(_PRODUCTION_LICENSE_PATH), pinned=anchor is not None)


def _run_formal_tailscale_auth(authkey: str, hostname: str) -> bool:
    """Run tailscale up --reset with the formal auth key."""
    login_server = settings.TS_LOGIN_SERVER
    argv = [
        "tailscale", "up",
        "--reset",
        f"--authkey={authkey}",
        f"--hostname={hostname}",
    ]
    if login_server:
        argv.append(f"--login-server={login_server}")
    result = _run_cmd(argv, timeout=60)
    if result.returncode == 0:
        logger.info("formal_tailscale_auth_success", hostname=hostname)
        return True
    logger.error(
        "formal_tailscale_auth_failed",
        hostname=hostname,
        returncode=result.returncode,
        stdout=result.stdout[:500],
        stderr=result.stderr[:500],
    )
    return False


def _save_device_identity(data: dict[str, Any]) -> None:
    """Save device identity to /var/lib/drycontroller/device.json."""
    _write_json(_DEVICE_JSON_PATH, data)
    logger.info("device_identity_saved", path=str(_DEVICE_JSON_PATH))


def _save_runtime_credentials(response: dict[str, Any]) -> None:
    node_token = str(response.get("node_enroll_token") or response.get("enroll_token") or "").strip()
    if not node_token:
        return

    _TAILNET_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = _TAILNET_ENV_PATH.read_text(encoding="utf-8").splitlines() if _TAILNET_ENV_PATH.exists() else []
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("CLOUD_OPS_ENROLL_TOKEN="):
            if not replaced:
                out.append(f"CLOUD_OPS_ENROLL_TOKEN={shlex.quote(node_token)}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"CLOUD_OPS_ENROLL_TOKEN={shlex.quote(node_token)}")
    _TAILNET_ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(_TAILNET_ENV_PATH, 0o600)
    logger.info("runtime_credentials_saved", path=str(_TAILNET_ENV_PATH))


def _cleanup_auth_keys() -> None:
    """Remove factory and formal auth keys from local config."""
    # Clear any stored auth keys in .env files
    env_paths = [
        Path("/etc/drycontroller/.env"),
        Path("/app/.env"),
    ]
    for env_path in env_paths:
        if not env_path.is_file():
            continue
        lines = env_path.read_text(encoding="utf-8").splitlines()
        cleaned = []
        for line in lines:
            key = line.split("=", 1)[0].strip().upper()
            if key in {"TS_AUTHKEY", "FACTORY_TS_AUTHKEY", "FORMAL_TS_AUTHKEY"}:
                logger.info("removing_auth_key_from_env", path=str(env_path), key=key)
                continue
            cleaned.append(line)
        env_path.write_text("\n".join(cleaned) + "\n", encoding="utf-8")

    logger.info("auth_keys_cleaned")


async def run_activation(
    *,
    requested_tenant_id: Optional[str] = None,
    requested_line_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Run the full activation flow.

    Can be called on first boot or manually triggered.
    Idempotent: if already activated, returns current state.
    """
    state_data = _read_state()
    current_state = state_data.get("state", ActivationState.FACTORY_INSTALLED.value)

    if current_state == ActivationState.FORMAL_LICENSE_ACTIVE.value:
        logger.info("already_activated")
        return {"success": True, "state": current_state, "message": "already_activated"}

    # Step 1: Verify factory license exists
    factory_license_path = _factory_license_source_path()
    if not factory_license_path.is_file():
        logger.error("factory_license_missing")
        return {"success": False, "state": current_state, "error": "factory_license_missing"}

    # Read factory license content (needed to send to cloud for signature validation)
    factory_license_content = _read_json(factory_license_path)
    if not factory_license_content:
        logger.error("factory_license_empty_or_invalid_json")
        return {"success": False, "state": current_state, "error": "factory_license_empty_or_invalid_json"}

    try:
        _ensure_factory_license_archive()
        factory_license_path = _FACTORY_LICENSE_ARCHIVE_PATH
        factory_license_content = _read_json(factory_license_path)
    except Exception as exc:
        logger.error("factory_license_archive_failed", error=str(exc))
        return {"success": False, "state": current_state, "error": "factory_license_archive_failed"}

    machine_id = _machine_id()
    if not machine_id:
        logger.error("machine_id_missing")
        return {"success": False, "state": current_state, "error": "machine_id_missing"}

    factory_fingerprint = _license_fingerprint(factory_license_path, machine_id)
    if not factory_fingerprint:
        logger.error("factory_fingerprint_empty")
        return {"success": False, "state": current_state, "error": "factory_fingerprint_empty"}

    # Persist server-compatible activation fingerprint for retries.
    if state_data.get("factory_license_fingerprint") != factory_fingerprint:
        state_data["factory_license_fingerprint"] = factory_fingerprint
        _write_state(state_data)

    # Step 2: Check if tailnet is already up (bootstrap or formal)
    tailnet_ready = await _wait_for_tailnet(timeout_sec=10)
    if not tailnet_ready:
        # Try to start tailscale with bootstrap credential
        logger.info("starting_bootstrap_tailnet")
        result = _run_cmd(["tailscale", "up", "--login-server", settings.TS_LOGIN_SERVER], timeout=30)
        if result.returncode != 0:
            logger.error("bootstrap_tailnet_failed", stderr=result.stderr[:500])
            state_data["state"] = ActivationState.FACTORY_INSTALLED.value
            _write_state(state_data)
            return {"success": False, "state": ActivationState.FACTORY_INSTALLED.value, "error": "bootstrap_tailnet_failed"}

        tailnet_ready = await _wait_for_tailnet(timeout_sec=120)
        if not tailnet_ready:
            return {"success": False, "state": ActivationState.FACTORY_INSTALLED.value, "error": "tailnet_timeout"}

    if current_state == ActivationState.FORMAL_TAILNET_JOINED.value:
        if await _confirm_formal_tailnet(factory_fingerprint, factory_license_content, machine_id):
            state_data["state"] = ActivationState.FORMAL_LICENSE_ACTIVE.value
            state_data["message"] = "activation_complete"
            state_data["activated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            _write_state(state_data)
            _BOOTSTRAP_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
            _BOOTSTRAP_SENTINEL.touch()
            return {"success": True, "state": ActivationState.FORMAL_LICENSE_ACTIVE.value, "message": "activation_complete"}
        state_data["state"] = ActivationState.FORMAL_TAILNET_JOINED.value
        state_data["message"] = "formal_tailnet_confirm_failed"
        _write_state(state_data)
        return {"success": False, "state": ActivationState.FORMAL_TAILNET_JOINED.value, "error": "formal_tailnet_confirm_failed"}

    state_data["state"] = ActivationState.BOOTSTRAP_TAILNET_JOINED.value
    _write_state(state_data)

    # Get bootstrap tailnet info
    ts_status = _run_cmd(["tailscale", "status", "--json"], timeout=5)
    bootstrap_ts_node = ""
    bootstrap_ts_ip = ""
    if ts_status.returncode == 0:
        try:
            ts_data = json.loads(ts_status.stdout)
            bootstrap_ts_node = ts_data.get("Self", {}).get("HostName", "")
            ips = ts_data.get("Self", {}).get("TailscaleIPs", [])
            bootstrap_ts_ip = ips[0] if ips else ""
        except Exception:
            pass

    # Step 3: Call Cloud Ops activation
    if not settings.CLOUD_OPS_BASE_URL:
        logger.error("cloud_ops_base_url_not_configured")
        return {"success": False, "state": current_state, "error": "cloud_ops_base_url_not_configured"}

    try:
        activation_result = await _call_cloud_activation(
            factory_license=factory_license_content,
            factory_license_fingerprint=factory_fingerprint,
            machine_id=machine_id,
            bootstrap_ts_node=bootstrap_ts_node,
            bootstrap_ts_ip=bootstrap_ts_ip,
            requested_tenant_id=requested_tenant_id,
            requested_line_id=requested_line_id,
        )
    except httpx.HTTPStatusError as exc:
        logger.error("cloud_activation_http_error", status=exc.response.status_code, body=exc.response.text[:500])
        return {"success": False, "state": state_data["state"], "error": f"cloud_activation_http_{exc.response.status_code}"}
    except Exception as exc:
        logger.error("cloud_activation_failed", error=str(exc))
        return {"success": False, "state": state_data["state"], "error": f"cloud_activation_failed: {exc}"}

    if not activation_result.get("success", True):
        logger.error("cloud_activation_rejected", detail=activation_result)
        return {"success": False, "state": state_data["state"], "error": "cloud_activation_rejected", "detail": activation_result}

    state_data["state"] = ActivationState.CLOUD_ACTIVATED.value
    state_data["device_id"] = activation_result.get("device_id")
    state_data["tenant_id"] = activation_result.get("tenant_id")
    state_data["line_id"] = activation_result.get("line_id")
    state_data["node_name"] = activation_result.get("node_name")
    state_data["first_online_at"] = activation_result.get("first_online_at")
    _write_state(state_data)
    _save_runtime_credentials(activation_result)

    # Step 4: Install production license
    license_payload = activation_result.get("license_payload")
    license_pub = activation_result.get("license_public_key")
    if license_payload and license_pub:
        _install_license(license_payload, license_pub)
    elif activation_result.get("state") == ActivationState.PENDING_OPS_APPROVAL.value:
        state_data["state"] = ActivationState.PENDING_OPS_APPROVAL.value
        state_data["message"] = "waiting_for_ops_approval"
        _write_state(state_data)
        logger.info("factory_activation_pending_ops_approval")
        return {"success": True, "state": ActivationState.PENDING_OPS_APPROVAL.value, "message": "waiting_for_ops_approval"}
    else:
        logger.error("cloud_activation_missing_license")
        return {"success": False, "state": state_data["state"], "error": "cloud_activation_missing_license"}

    # Step 5: Save device identity
    _save_device_identity({
        "device_id": activation_result.get("device_id"),
        "tenant_id": activation_result.get("tenant_id"),
        "line_id": activation_result.get("line_id"),
        "node_name": activation_result.get("node_name"),
        "machine_id": machine_id,
        "factory_license_fingerprint": factory_fingerprint,
        "activated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })

    # Step 6: Formal Tailscale auth
    formal_ts_authkey = activation_result.get("formal_ts_authkey")
    formal_ts_hostname = activation_result.get("formal_ts_hostname")
    formal_join_ok = False
    if formal_ts_authkey and formal_ts_hostname:
        formal_join_ok = _run_formal_tailscale_auth(formal_ts_authkey, formal_ts_hostname)
        if formal_join_ok:
            state_data["state"] = ActivationState.FORMAL_TAILNET_JOINED.value
            _write_state(state_data)
            formal_join_ok = await _confirm_formal_tailnet(factory_fingerprint, factory_license_content, machine_id)
        else:
            # Key may have been consumed by tailscale up even though join failed.
            # Request a fresh key from Cloud Ops and retry once.
            logger.warning("formal_join_failed_requesting_new_key")
            reissue = await _reissue_ts_key(factory_fingerprint, factory_license_content, machine_id)
            if reissue and reissue.get("formal_ts_authkey"):
                formal_join_ok = _run_formal_tailscale_auth(
                    reissue["formal_ts_authkey"],
                    reissue.get("formal_ts_hostname") or formal_ts_hostname,
                )
                if formal_join_ok:
                    state_data["state"] = ActivationState.FORMAL_TAILNET_JOINED.value
                    _write_state(state_data)
                    formal_join_ok = await _confirm_formal_tailnet(factory_fingerprint, factory_license_content, machine_id)
                else:
                    logger.error("formal_join_failed_again_after_reissue")
            else:
                logger.warning("reissue_ts_key_unavailable")
    else:
        logger.info("no_formal_ts_authkey_provided")

    # Step 7: Cleanup auth keys
    _cleanup_auth_keys()

    # Step 8: Mark as fully activated only if formal join succeeded
    if formal_join_ok:
        state_data["state"] = ActivationState.FORMAL_LICENSE_ACTIVE.value
    # else: stay at CLOUD_ACTIVATED / BOOTSTRAP_TAILNET_JOINED, retry formal join later
    state_data["activated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _write_state(state_data)

    if state_data["state"] != ActivationState.FORMAL_LICENSE_ACTIVE.value:
        return {
            "success": False,
            "state": state_data["state"],
            "device_id": activation_result.get("device_id"),
            "tenant_id": activation_result.get("tenant_id"),
            "error": "formal_tailnet_confirm_failed" if state_data["state"] == ActivationState.FORMAL_TAILNET_JOINED.value else "formal_tailnet_pending",
        }

    # Create bootstrap sentinel
    _BOOTSTRAP_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    _BOOTSTRAP_SENTINEL.touch()

    logger.info(
        "activation_complete",
        device_id=activation_result.get("device_id"),
        node_name=activation_result.get("node_name"),
        tenant_id=activation_result.get("tenant_id"),
    )

    return {
        "success": True,
        "state": state_data["state"],
        "device_id": activation_result.get("device_id"),
        "tenant_id": activation_result.get("tenant_id"),
        "line_id": activation_result.get("line_id"),
        "node_name": activation_result.get("node_name"),
        "message": "activation_complete",
    }
