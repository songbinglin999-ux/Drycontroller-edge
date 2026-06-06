import hashlib
import json
from pathlib import Path
import sys

PATH = Path(__file__).resolve()
for candidate in (PATH.parents[1], PATH.parents[1] / "app"):
    entry = str(candidate)
    if entry not in sys.path:
        sys.path.append(entry)

from app.services import activation


def test_activation_state_reports_host_owned_activation_files(tmp_path: Path, monkeypatch):
    state_path = tmp_path / "activation-state.json"
    license_archive = tmp_path / "factory-license.json"
    license_path = tmp_path / "license.json"
    public_key_path = tmp_path / "license.pub"
    device_json_path = tmp_path / "device.json"
    tailscale_dir = tmp_path / "tailscale"

    state_path.write_text(
        json.dumps({"state": "formal_license_active", "message": "activation_complete"}),
        encoding="utf-8",
    )
    factory_license = {
        "schema": "drycontroller.factory-license.v1",
        "expires_at": "2026-07-01T00:00:00+00:00",
        "signature": "sig",
    }
    license_archive.write_text(json.dumps(factory_license), encoding="utf-8")
    public_key_path.write_text("pub\n", encoding="utf-8")
    device_json_path.write_text("{}", encoding="utf-8")
    tailscale_dir.mkdir()
    (tailscale_dir / "tailscaled.state").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(activation, "_STATE_FILE", state_path)
    monkeypatch.setattr(activation, "_FACTORY_LICENSE_ARCHIVE_PATH", license_archive)
    monkeypatch.setattr(activation, "_FACTORY_LICENSE_PATH", license_path)
    monkeypatch.setattr(activation, "_FACTORY_LICENSE_PUB_PATH", public_key_path)
    monkeypatch.setattr(activation, "_DEVICE_JSON_PATH", device_json_path)
    monkeypatch.setattr(activation, "_TAILSCALE_CONFIG_DIR", tailscale_dir)
    monkeypatch.setattr(activation, "_machine_id", lambda: "machine-1")

    raw = json.dumps(factory_license, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    h = hashlib.sha256()
    h.update(raw)
    h.update(b"\0")
    h.update(b"machine-1")

    result = activation.get_activation_state()

    assert result["state"] == "formal_license_active"
    assert result["message"] == "activation_complete"
    assert result["factory_license_exists"] is True
    assert result["factory_pub_exists"] is True
    assert result["device_json_exists"] is True
    assert result["tailscale_state_exists"] is True
    assert result["machine_id"] == "machine-1"
    assert result["factory_license_fingerprint"] == h.hexdigest()


def test_edge_api_activation_service_is_status_only():
    assert not hasattr(activation, "_TAILNET_ENV_PATH")
    assert not hasattr(activation, "_save_runtime_credentials")
