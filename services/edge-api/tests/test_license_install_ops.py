"""
edge-api License 安装闭环测试。

目标：云端下发 License 后，边端可无人值守安装；新 License 校验失败时必须恢复旧文件。
"""

import sys
import base64
import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.api.v1 import ops as ops_module  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.services.registry_auth_writer import get_registry_auth_writer_client  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_settings_after_test():
    names = (
        "ADMIN_API_KEYS",
        "CONTROL_API_KEYS",
        "API_KEY_HEADER",
        "LICENSE_FILE_PATH",
        "LICENSE_TRUST_POLICY_PATH",
    )
    original = {name: getattr(settings, name) for name in names}
    yield
    for name, value in original.items():
        setattr(settings, name, value)


def _client(*, registry_writer=None) -> TestClient:
    app = FastAPI()
    app.include_router(ops_module.router, prefix="/api/v1/ops")
    if registry_writer is not None:
        app.dependency_overrides[get_registry_auth_writer_client] = lambda: registry_writer
    return TestClient(app)


class _MissingRegistryWriter:
    async def status(self):
        return {"status": "missing"}


def _write_device_identity(path: Path) -> None:
    credential = "test-device-credential"
    path.write_text(
        json.dumps(
            {
                "schema": "drycontroller.device-identity.v1",
                "device_id": "EDGE-ABCDEF123456",
                "tenant_id": "t1",
                "line_id": "LINE-A",
                "node_name": "edge-abcdef123456",
                "identity_fingerprint": "11" * 32,
                "runtime_credential": credential,
                "runtime_credential_hash": hashlib.sha256(credential.encode()).hexdigest(),
                "credential_expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_trust_policy(root: Path) -> Path:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    artifact = {
        "algorithm": "Ed25519",
        "key_id": "license-test",
        "public_key_sha256": "sha256:" + hashlib.sha256(der).hexdigest(),
        "public_key_spki_der_b64": base64.b64encode(der).decode("ascii"),
        "schema": "drycontroller.license-trust.v1",
    }
    artifact_bytes = json.dumps(
        artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    version = root / "versions" / "license-test"
    version.mkdir(parents=True)
    (version / "license-trust.json").write_bytes(artifact_bytes)
    policy = {
        "active": {
            "canonical_sha256": "sha256:" + hashlib.sha256(artifact_bytes).hexdigest(),
            "key_id": "license-test",
        },
        "previous": None,
        "schema": "drycontroller.license-trust-policy.v1",
    }
    policy_path = root / "policy.json"
    policy_path.write_text(
        json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return policy_path


def test_cloud_readiness_requires_edge_admin_key_not_tailnet_identity(monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.CONTROL_API_KEYS = ["control-1"]
    settings.API_KEY_HEADER = "X-API-Key"

    async def healthy():
        return {"postgres": {"status": "healthy"}, "mqtt": {"status": "healthy"}}

    monkeypatch.setattr(ops_module, "get_system_readiness", healthy)
    client = _client()

    missing = client.get("/api/v1/ops/ready")
    tailnet_only = client.get(
        "/api/v1/ops/ready",
        headers={"Tailscale-User-Login": "operator@example.test"},
    )
    authenticated = client.get(
        "/api/v1/ops/ready",
        headers={"X-API-Key": "admin-1"},
    )

    assert missing.status_code == 401
    assert tailnet_only.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json()["status"] == "ready"


def test_license_install_mutation_route_is_removed(tmp_path: Path):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    license_path = tmp_path / "license.json"
    license_path.write_text('{"old":true}\n', encoding="utf-8")
    settings.LICENSE_FILE_PATH = str(license_path)

    resp = _client().post(
        "/api/v1/ops/license/install",
        json={"license": {"key_id": "candidate"}},
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 404
    assert license_path.read_text(encoding="utf-8") == '{"old":true}\n'


def test_registry_credential_refresh_binds_root_status_to_precommitted_audit(monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"

    observed: dict[str, object] = {}
    audit: dict[str, object] = {}

    class Writer:
        status_payload = {"status": "missing"}

        async def install(self, payload):
            observed.update(payload)
            self.status_payload = {
                key: value
                for key, value in payload.items()
                if key != "secret"
            } | {
                "status": "active",
                "registry_host": "git.example.com",
                "updated_at": "2026-08-19T00:00:00+00:00",
            }
            return self.status_payload

        async def status(self):
            return self.status_payload

    async def write_audit(**kwargs):
        audit.update(kwargs)
        return 23

    monkeypatch.setattr(ops_module, "write_audit", write_audit)
    writer = Writer()

    resp = _client(registry_writer=writer).post(
        "/api/v1/ops/registry-credentials/refresh",
        json={
            "credential_id": "cred-1",
            "registry_host": "https://git.example.com/",
            "username": "robot$edge",
            "secret": "robot-secret",
            "secret_fingerprint": "fp-1",
            "version": 2,
        },
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["credential"]["registry_host"] == "git.example.com"
    assert "secret" not in body["credential"]
    assert "robot-secret" not in json.dumps(body)
    assert observed["secret"] == "robot-secret"
    assert observed["audit_id"] == 23
    assert observed["transaction_id"] == audit["correlation_id"]
    assert audit["result"] == "accepted"
    assert audit["detail"]["version"] == 2
    assert audit["detail"]["secret_fingerprint"] == "fp-1"

    resp = _client(registry_writer=writer).get(
        "/api/v1/ops/registry-credentials/status",
        headers={"X-API-Key": "admin-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "ok"
    assert resp.json()["credential"]["audit_id"] == 23


def test_registry_refresh_never_calls_root_writer_when_audit_commit_fails(monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    called = False

    class Writer:
        async def install(self, _payload):
            nonlocal called
            called = True

    async def failed_audit(**_kwargs):
        return None

    monkeypatch.setattr(ops_module, "write_audit", failed_audit)
    response = _client(registry_writer=Writer()).post(
        "/api/v1/ops/registry-credentials/refresh",
        json={
            "credential_id": "cred-1",
            "registry_host": "registry.example.test",
            "username": "edge-pull",
            "secret": "never-persisted",
            "secret_fingerprint": "fp-1",
            "version": 2,
        },
        headers={"X-API-Key": "admin-1"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "registry_credential_audit_unavailable"
    assert called is False
    assert "never-persisted" not in response.text


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("audit_id", -1, "registry_credential_audit_invalid"),
        ("transaction_id", "wrong", "registry_credential_transaction_invalid"),
        ("version", -1, "registry_credential_version_invalid"),
    ],
)
def test_registry_status_detects_invalid_atomic_protocol(field, value, reason):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    status = {
        "status": "active",
        "credential_id": "cred-1",
        "registry_host": "registry.example.test",
        "username": "edge-pull",
        "secret_fingerprint": "fp-1",
        "version": 2,
        "transaction_id": "a" * 32,
        "audit_id": 23,
        "updated_at": "2026-08-19T00:00:00+00:00",
    }
    status[field] = value

    class Writer:
        async def status(self):
            return status

    response = _client(registry_writer=Writer()).get(
        "/api/v1/ops/registry-credentials/status",
        headers={"X-API-Key": "admin-1"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["reason"] == reason


@pytest.mark.parametrize("failure", ["writer", "binding"])
def test_registry_refresh_reports_root_writer_failure_without_secret_disclosure(
    monkeypatch,
    failure: str,
):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"

    async def write_audit(**_kwargs):
        return 23

    monkeypatch.setattr(ops_module, "write_audit", write_audit)

    class Writer:
        async def install(self, payload):
            if failure == "writer":
                raise HTTPException(status_code=503, detail={"error": "writer_unavailable"})
            return {
                key: value
                for key, value in payload.items()
                if key != "secret"
            } | {
                "status": "active",
                "registry_host": "wrong.example.test",
                "updated_at": "2026-08-19T00:00:00+00:00",
            }

    response = _client(registry_writer=Writer()).post(
        "/api/v1/ops/registry-credentials/refresh",
        json={
            "credential_id": "cred-1",
            "registry_host": "registry.example.test",
            "username": "edge-pull",
            "secret": "never-persisted",
            "secret_fingerprint": "fp-1",
            "version": 2,
        },
        headers={"X-API-Key": "admin-1"},
    )

    assert response.status_code in {502, 503}
    assert "never-persisted" not in response.text


def test_ota_start_requires_registry_credential_status(monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    monkeypatch.setattr(
        ops_module,
        "require_license_capability",
        lambda capability: {"valid": True, "capabilities": [capability]},
    )
    resp = _client(registry_writer=_MissingRegistryWriter()).post(
        "/api/v1/ops/ota/start",
        json={"operation_id": "a" * 32},
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "registry_credential_missing"


def test_ota_start_rejects_missing_license_before_registry_or_helper(monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    class Writer:
        async def status(self):
            raise AssertionError("registry status must not be read before License admission")

    def reject(_capability):
        raise HTTPException(
            status_code=409,
            detail={"success": False, "error": "license_not_admitted"},
        )

    monkeypatch.setattr(ops_module, "require_license_capability", reject)
    response = _client(registry_writer=Writer()).post(
        "/api/v1/ops/ota/start",
        json={"operation_id": "a" * 32},
        headers={"X-API-Key": "admin-1"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "license_not_admitted"


def test_ota_start_rejects_every_execution_input_except_operation_id():
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"

    resp = _client().post(
        "/api/v1/ops/ota/start",
        json={
            "operation_id": "a" * 32,
            "compose_override_yaml": "services: {}",
        },
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 422


def test_preflight_returns_pass_with_degraded_allowed(tmp_path: Path, monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    settings.LICENSE_FILE_PATH = str(tmp_path / "license.json")
    settings.LICENSE_TRUST_POLICY_PATH = str(_write_trust_policy(tmp_path / "license-trust"))
    Path(settings.LICENSE_FILE_PATH).write_text("{}", encoding="utf-8")
    device_json = tmp_path / "device.json"
    _write_device_identity(device_json)
    monkeypatch.setenv("PREFLIGHT_DEVICE_JSON", str(device_json))
    monkeypatch.setattr(settings, "DEVICE_IDENTITY_PATH", str(device_json))
    monkeypatch.setattr(settings, "CLOUD_OPS_BASE_URL", "http://cloud")
    monkeypatch.setattr(settings, "TS_LOGIN_SERVER", "http://headscale")
    monkeypatch.setattr(settings, "TAILSCALE_EXPECTED_VERSION", "1.98.10")

    monkeypatch.setattr(
        ops_module,
        "load_tailnet_observation",
        lambda: {
            "ok": False,
            "reason": "tailscale_not_running",
            "detail": {},
        },
    )

    async def fake_verify():
        return {"url": "mock", "status_code": 200, "body": {"valid": True}}

    async def fake_http_check(checks, check_id, url, *, degraded_ok, allow_degraded, timeout=5.0):
        ops_module._preflight_check(checks, check_id, "pass", "ok", detail={"url": url})

    monkeypatch.setattr(ops_module, "_call_license_verify", fake_verify)
    monkeypatch.setattr(ops_module, "_preflight_http_check", fake_http_check)
    recorded = []

    async def fake_record_preflight_run(payload):
        recorded.append(payload)

    monkeypatch.setattr(ops_module, "record_preflight_run", fake_record_preflight_run)

    resp = _client().post(
        "/api/v1/ops/preflight",
        json={"strict": False, "check_ota": False},
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["status"] == "degraded"
    assert body["summary"]["fail"] == 0
    checks = {item["id"]: item for item in body["checks"]}
    assert checks["tailnet.identity"]["status"] == "degraded"
    assert checks["tailnet.identity"]["reason"] == "tailscale_not_running"
    assert checks["tailnet.application_e2e"]["status"] == "degraded"
    assert recorded[0]["status"] == "degraded"


@pytest.mark.asyncio
async def test_strict_preflight_application_probe_requires_both_authenticated_directions(monkeypatch):
    class FakeResponse:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {
                "success": True,
                "cloud_to_edge_ready": {
                    "status_code": 200,
                    "json": {"success": True},
                },
            }

    observed = {}

    class FakeHttpClient:
        def __init__(self, *, timeout):
            assert timeout == 10

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json, headers):
            observed.update({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    monkeypatch.setattr(settings, "CLOUD_OPS_BASE_URL", "https://ops.example.test")
    monkeypatch.setattr(
        ops_module,
        "load_runtime_identity",
        lambda **_kwargs: {
            "node_name": "edge-abcdef123456",
            "line_id": "LINE-A",
            "runtime_credential": "per-device-secret",
        },
    )
    monkeypatch.setattr(ops_module.httpx, "AsyncClient", FakeHttpClient)
    checks = []

    await ops_module._preflight_cloud_application_path(
        checks,
        tailnet={
            "ok": True,
            "reason": "ok",
            "detail": {"tailscale_ips": ["100.100.0.22", "fd7a:115c:a1e0::22"]},
        },
        allow_degraded=False,
    )

    assert checks == [
        {
            "id": "tailnet.application_e2e",
            "status": "pass",
            "reason": "ok",
            "hint": "",
            "detail": {"cloud_status_code": 200, "cloud_to_edge_status_code": 200},
        }
    ]
    assert observed["url"] == "https://ops.example.test/api/v1/edge-status"
    assert observed["json"]["address"] == "100.100.0.22"
    assert observed["json"]["verify_cloud_to_edge_ready"] is True
    assert observed["headers"] == {"X-Device-Credential": "per-device-secret"}


def test_preflight_check_ota_requires_registry_credential(tmp_path: Path, monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    settings.LICENSE_FILE_PATH = str(tmp_path / "license.json")
    settings.LICENSE_TRUST_POLICY_PATH = str(_write_trust_policy(tmp_path / "license-trust"))
    Path(settings.LICENSE_FILE_PATH).write_text("{}", encoding="utf-8")
    device_json = tmp_path / "device.json"
    _write_device_identity(device_json)
    monkeypatch.setenv("PREFLIGHT_DEVICE_JSON", str(device_json))
    monkeypatch.setattr(settings, "DEVICE_IDENTITY_PATH", str(device_json))
    monkeypatch.setattr(settings, "CLOUD_OPS_BASE_URL", "http://cloud")
    monkeypatch.setattr(settings, "TS_LOGIN_SERVER", "http://headscale")

    async def fake_verify():
        return {"url": "mock", "status_code": 200, "body": {"valid": True}}

    async def fake_http_check(checks, check_id, url, *, degraded_ok, allow_degraded, timeout=5.0):
        ops_module._preflight_check(checks, check_id, "pass", "ok", detail={"url": url})

    monkeypatch.setattr(ops_module, "_call_license_verify", fake_verify)
    monkeypatch.setattr(ops_module, "_preflight_http_check", fake_http_check)

    async def fake_record_preflight_run(_payload):
        return None

    monkeypatch.setattr(ops_module, "record_preflight_run", fake_record_preflight_run)

    resp = _client(registry_writer=_MissingRegistryWriter()).post(
        "/api/v1/ops/preflight",
        json={"strict": False, "check_ota": True},
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 409
    checks = {item["id"]: item for item in resp.json()["detail"]["checks"]}
    assert checks["registry.credential"]["reason"] == "registry_credential_missing"


def test_preflight_fails_when_required_files_missing(tmp_path: Path, monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    settings.LICENSE_FILE_PATH = str(tmp_path / "missing-license.json")
    settings.LICENSE_TRUST_POLICY_PATH = str(tmp_path / "missing-license-trust-policy.json")
    monkeypatch.setenv("PREFLIGHT_DEVICE_JSON", str(tmp_path / "missing-device.json"))
    monkeypatch.setattr(settings, "DEVICE_IDENTITY_PATH", str(tmp_path / "missing-device.json"))

    async def fake_verify():
        return {"url": "mock", "status_code": 200, "body": {"valid": False}}

    async def fake_http_check(checks, check_id, url, *, degraded_ok, allow_degraded, timeout=5.0):
        ops_module._preflight_check(checks, check_id, "pass", "ok", detail={"url": url})

    monkeypatch.setattr(ops_module, "_call_license_verify", fake_verify)
    monkeypatch.setattr(ops_module, "_preflight_http_check", fake_http_check)
    recorded = []

    async def fake_record_preflight_run(payload):
        recorded.append(payload)

    monkeypatch.setattr(ops_module, "record_preflight_run", fake_record_preflight_run)

    resp = _client().post(
        "/api/v1/ops/preflight",
        json={"strict": False, "check_ota": False},
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["success"] is False
    assert detail["status"] == "fail"
    assert detail["summary"]["fail"] >= 1
    assert recorded[0]["status"] == "fail"


def test_plc_config_apply_records_version(monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    written = []
    recorded = []

    def fake_atomic_write_yaml(path, payload):
        written.append((path, payload))

    async def fake_record_config_version(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(ops_module, "_atomic_write_yaml", fake_atomic_write_yaml)
    monkeypatch.setattr(ops_module, "record_config_version", fake_record_config_version)

    resp = _client().post(
        "/api/v1/ops/plc-config/apply",
        json={
            "plc_host": "192.168.1.20",
            "restart_edge_daq": False,
            "read_ready_after_restart": False,
        },
        headers={"X-API-Key": "admin-1"},
    )

    assert resp.status_code == 200
    assert written[0][1]["host"] == "192.168.1.20"
    assert recorded[0]["scope"] == "plc"
    assert recorded[0]["source"] == "cloud"
    assert recorded[0]["restart_required"] is True


def test_plc_config_apply_rolls_back_config_when_restart_fails(tmp_path: Path, monkeypatch):
    settings.ADMIN_API_KEYS = ["admin-1"]
    settings.API_KEY_HEADER = "X-API-Key"
    recorded = []
    cfg_path = tmp_path / "plc.yaml"
    old_config = "host: 192.168.1.10\nport: 502\n"
    cfg_path.write_text(old_config, encoding="utf-8")

    class _FailingMonitorClient:
        async def restart_service(self, *_args, **_kwargs):
            raise RuntimeError("restart failed")

    async def fake_record_config_version(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(ops_module, "PLC_CONFIG_RUNTIME_PATH", cfg_path)
    monkeypatch.setattr(ops_module, "record_config_version", fake_record_config_version)
    app = FastAPI()
    app.include_router(ops_module.router, prefix="/api/v1/ops")
    app.dependency_overrides[ops_module.get_monitor_ops_client] = lambda: _FailingMonitorClient()

    with pytest.raises(RuntimeError, match="restart failed"):
        TestClient(app).post(
            "/api/v1/ops/plc-config/apply",
            json={"plc_host": "192.168.1.20"},
            headers={"X-API-Key": "admin-1"},
        )

    assert recorded == []
    assert cfg_path.read_text(encoding="utf-8") == old_config
