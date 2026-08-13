import datetime as dt
import hashlib
import json

import pytest


def _identity(credential="per-device-secret", *, expires_at=None):
    return {
        "schema": "drycontroller.device-identity.v1",
        "device_id": "EDGE-ABCDEF123456",
        "tenant_id": "tenant-a",
        "line_id": "LINE-A",
        "node_name": "edge-abcdef123456",
        "identity_fingerprint": "11" * 32,
        "runtime_credential": credential,
        "runtime_credential_hash": hashlib.sha256(credential.encode()).hexdigest(),
        "credential_expires_at": expires_at
        or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(),
    }


def test_runtime_identity_requires_0600_and_matching_unexpired_credential(tmp_path):
    from app.core.device_identity import DeviceIdentityUnavailable, load_runtime_identity

    path = tmp_path / "device.json"
    path.write_text(json.dumps(_identity()), encoding="utf-8")
    path.chmod(0o600)
    loaded = load_runtime_identity(path, require_root_owner=False)
    assert loaded["device_id"] == "EDGE-ABCDEF123456"

    path.chmod(0o644)
    with pytest.raises(DeviceIdentityUnavailable, match="0600"):
        load_runtime_identity(path, require_root_owner=False)

    path.write_text(json.dumps(_identity(expires_at="2020-01-01T00:00:00Z")), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(DeviceIdentityUnavailable, match="expired"):
        load_runtime_identity(path, require_root_owner=False)

    invalid = _identity()
    invalid["runtime_credential_hash"] = "00" * 32
    path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(DeviceIdentityUnavailable, match="invalid"):
        load_runtime_identity(path, require_root_owner=False)
