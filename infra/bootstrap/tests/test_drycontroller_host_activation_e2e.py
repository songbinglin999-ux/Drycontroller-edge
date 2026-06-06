import json
import os
import stat
import subprocess
from pathlib import Path


BOOTSTRAP_DIR = Path(__file__).resolve().parents[1]
ACTIVATION_SCRIPT = BOOTSTRAP_DIR / "drycontroller-host-activation.sh"
TAILNET_SCRIPT = BOOTSTRAP_DIR / "drycontroller-host-tailnet.sh"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_path(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    _write_executable(
        bin_dir / "id",
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-u" ]; then echo 0; exit 0; fi\n'
        "/usr/bin/id \"$@\"\n",
    )
    _write_executable(
        bin_dir / "cat",
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "/etc/machine-id" ]; then echo e2e-machine-id; exit 0; fi\n'
        "/bin/cat \"$@\"\n",
    )
    _write_executable(
        bin_dir / "tailscale",
        "#!/usr/bin/env bash\n"
        "case \"${1:-}\" in\n"
        "  ip) echo 100.100.0.44 ;;\n"
        "  status) printf '{\"Self\":{\"HostName\":\"bootstrap-edge\"}}\\n' ;;\n"
        "  set) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    _write_executable(
        bin_dir / "systemctl",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        bin_dir / "curl",
        "#!/usr/bin/env bash\n"
        "out=''\n"
        "data=''\n"
        "url=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -o) out=\"$2\"; shift 2 ;;\n"
        "    -d) data=\"$2\"; shift 2 ;;\n"
        "    -*) shift ;;\n"
        "    *) url=\"$1\"; shift ;;\n"
        "  esac\n"
        "done\n"
        "mkdir -p \"$DRYCONTROLLER_E2E_CAPTURE_DIR\"\n"
        "if [[ \"$url\" == */bind-tailnet-node ]]; then\n"
        "  printf '%s' \"$data\" > \"$DRYCONTROLLER_E2E_CAPTURE_DIR/bind-payload.json\"\n"
        "  [ -n \"$out\" ] && : > \"$out\"\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s' \"$data\" > \"$DRYCONTROLLER_E2E_CAPTURE_DIR/activation-payload.json\"\n"
        "cat > \"$out\" <<'JSON'\n"
        "{\"success\":true,\"state\":\"cloud_activated\",\"device_id\":\"EDGE-E2E0001\",\"tenant_id\":\"tenant-e2e\",\"line_id\":\"line-a\",\"node_name\":\"edge-e2e0001\",\"license_payload\":{\"schema\":\"drycontroller.license.v1\",\"signature\":\"signed-production\",\"expires_at\":\"2027-01-01T00:00:00+00:00\"},\"license_public_key\":\"public-key-e2e\",\"license_key_id\":\"test-key\",\"node_enroll_token\":\"node-token-e2e\",\"enroll_token\":\"node-token-e2e\",\"edge_admin_api_key\":\"edge-admin-e2e\",\"edge_control_api_key\":\"edge-control-e2e\",\"message\":\"activation_auto_approved\"}\n"
        "JSON\n",
    )
    return bin_dir


def _activation_bundle() -> dict:
    return {
        "schema": "drycontroller.activation-bundle.v1",
        "tailnet": {
            "ts_login_server": "https://headscale.example.test",
            "ts_hostname": "bootstrap-edge",
            "ts_authkey": "tskey-bootstrap",
            "cloud_ops_base_url": "https://ops.example.test",
            "cloud_ops_enroll_token_header": "X-Enroll-Token",
        },
        "factory_license": {
            "schema": "drycontroller.factory-license.v1",
            "license_id": "factory-e2e",
            "expires_at": "2027-01-01T00:00:00+00:00",
            "signature": "signed-factory",
        },
    }


def test_host_activation_bundle_to_formal_license_e2e(tmp_path: Path):
    bin_dir = _fake_path(tmp_path)
    etc_dir = tmp_path / "etc" / "drycontroller"
    var_dir = tmp_path / "var" / "lib" / "drycontroller"
    edge_root = tmp_path / "edge"
    capture_dir = tmp_path / "capture"
    etc_dir.mkdir(parents=True)
    var_dir.mkdir(parents=True)
    edge_root.mkdir()

    bundle_path = etc_dir / "activation-bundle.json"
    bundle_path.write_text(json.dumps(_activation_bundle()), encoding="utf-8")
    edge_env = edge_root / ".env"
    edge_env.write_text("UNCHANGED=1\nCLOUD_OPS_BASE_URL=http://old-cloud\n", encoding="utf-8")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DRYCONTROLLER_ACTIVATION_BUNDLE": str(bundle_path),
        "DRYCONTROLLER_TAILNET_ENV": str(etc_dir / "tailnet.env"),
        "DRYCONTROLLER_ACTIVATION_STATE": str(var_dir / "activation-state.json"),
        "DRYCONTROLLER_DEVICE_JSON": str(var_dir / "device.json"),
        "DRYCONTROLLER_BOOTSTRAP_SENTINEL": str(var_dir / ".bootstrapped"),
        "LICENSE_FILE_PATH": str(etc_dir / "license" / "license.json"),
        "FACTORY_LICENSE_ARCHIVE_PATH": str(var_dir / "factory-license.json"),
        "LICENSE_PUBLIC_KEY_PATH": str(etc_dir / "keys" / "license.pub"),
        "EDGE_ROOT": str(edge_root),
        "EDGE_ENV_FILE": str(edge_env),
        "DRYCONTROLLER_E2E_CAPTURE_DIR": str(capture_dir),
    }

    result = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "activation complete" in result.stdout
    state = json.loads((var_dir / "activation-state.json").read_text(encoding="utf-8"))
    assert state["state"] == "formal_license_active"
    assert state["message"] == "activation_complete"
    assert (var_dir / ".bootstrapped").is_file()

    factory_archive = json.loads((var_dir / "factory-license.json").read_text(encoding="utf-8"))
    assert factory_archive["schema"] == "drycontroller.factory-license.v1"
    production_license = json.loads((etc_dir / "license" / "license.json").read_text(encoding="utf-8"))
    assert production_license["schema"] == "drycontroller.license.v1"
    assert (etc_dir / "keys" / "license.pub").read_text(encoding="utf-8").strip() == "public-key-e2e"

    device = json.loads((var_dir / "device.json").read_text(encoding="utf-8"))
    assert device["device_id"] == "EDGE-E2E0001"
    assert device["tenant_id"] == "tenant-e2e"
    assert device["node_name"] == "edge-e2e0001"

    tailnet_env = (etc_dir / "tailnet.env").read_text(encoding="utf-8")
    assert "TS_AUTHKEY=" not in tailnet_env
    assert "CLOUD_OPS_ENROLL_TOKEN=node-token-e2e" in tailnet_env
    assert "TENANT_ID=tenant-e2e" in tailnet_env
    assert "LINE_ID=line-a" in tailnet_env
    assert "TS_HOSTNAME=edge-e2e0001" in tailnet_env

    edge_env_lines = edge_env.read_text(encoding="utf-8").splitlines()
    assert "UNCHANGED=1" in edge_env_lines
    assert "EDGE_API_BIND_ADDR=100.100.0.44" in edge_env_lines
    assert "TS_HOSTNAME=edge-e2e0001" in edge_env_lines
    assert 'ADMIN_API_KEYS=["edge-admin-e2e"]' in edge_env_lines
    assert 'CONTROL_API_KEYS=["edge-control-e2e"]' in edge_env_lines
    assert "CLOUD_OPS_BASE_URL=https://ops.example.test" in edge_env_lines
    assert "CLOUD_OPS_ENROLL_TOKEN=node-token-e2e" in edge_env_lines
    assert "CLOUD_OPS_ENROLL_TOKEN_HEADER=X-Enroll-Token" in edge_env_lines

    activation_payload = json.loads((capture_dir / "activation-payload.json").read_text(encoding="utf-8"))
    assert activation_payload["machine_id"] == "e2e-machine-id"
    assert activation_payload["bootstrap_ts_node"] == "bootstrap-edge"
    bind_payload = json.loads((capture_dir / "bind-payload.json").read_text(encoding="utf-8"))
    assert bind_payload["tailnet_ip"] == "100.100.0.44"
    assert bind_payload["tailnet_hostname"] == "edge-e2e0001"

    tailnet_rerun = subprocess.run(
        ["bash", str(TAILNET_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "tailnet already has IPv4" in tailnet_rerun.stdout
    assert "TS_AUTHKEY=" not in (etc_dir / "tailnet.env").read_text(encoding="utf-8")

    activation_rerun = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "activation already complete" in activation_rerun.stdout
    assert (var_dir / ".bootstrapped").is_file()
    tailnet_env_after_rerun = (etc_dir / "tailnet.env").read_text(encoding="utf-8")
    assert "TS_AUTHKEY=" not in tailnet_env_after_rerun
    assert "CLOUD_OPS_ENROLL_TOKEN=node-token-e2e" in tailnet_env_after_rerun
    assert "TS_HOSTNAME=edge-e2e0001" in tailnet_env_after_rerun


def test_host_tailnet_does_not_accept_split_material_without_bundle(tmp_path: Path):
    bin_dir = _fake_path(tmp_path)
    env_file = tmp_path / "tailnet.env"
    env_file.write_text(
        "TS_LOGIN_SERVER=https://headscale.example.test\n"
        "TS_HOSTNAME=legacy-edge\n"
        "TS_AUTHKEY=legacy-key\n"
        "CLOUD_OPS_BASE_URL=https://ops.example.test\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(TAILNET_SCRIPT)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DRYCONTROLLER_TAILNET_ENV": str(env_file),
            "DRYCONTROLLER_ACTIVATION_BUNDLE": str(tmp_path / "missing-activation-bundle.json"),
            "DRYCONTROLLER_E2E_CAPTURE_DIR": str(tmp_path / "capture"),
        },
        text=True,
        capture_output=True,
        check=True,
    )

    assert "no activation bundle; skipping tailnet join" in result.stdout
