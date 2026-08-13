import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


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
        bin_dir / "docker",
        "#!/usr/bin/env bash\n"
        "if [ \"${1:-}\" = volume ] && [ \"${2:-}\" = ls ]; then\n"
        "  case \"${DRYCONTROLLER_E2E_VOLUME_STATE:-fresh}\" in\n"
        "    fresh) exit 0 ;;\n"
        "    partial) echo drycontroller-edge_postgres_data; exit 0 ;;\n"
        "    existing)\n"
        "      for name in postgres_data influxdb_data influxdb_config mosquitto_data mosquitto_logs mosquitto_secrets prometheus_data alertmanager_data; do\n"
        "        echo drycontroller-edge_${name}\n"
        "      done\n"
        "      exit 0\n"
        "      ;;\n"
        "    error) exit 1 ;;\n"
        "  esac\n"
        "fi\n"
        "exit 1\n",
    )
    _write_executable(
        bin_dir / "mv",
        "#!/usr/bin/env bash\n"
        "previous=''\n"
        "current=''\n"
        "for arg in \"$@\"; do previous=\"$current\"; current=\"$arg\"; done\n"
        "if [ -n \"${DRYCONTROLLER_E2E_FAIL_STATE:-}\" ] "
        "&& [ \"$current\" = \"${DRYCONTROLLER_ACTIVATION_STATE:-}\" ] "
        "&& /usr/bin/grep -q \"\\\"state\\\": \\\"${DRYCONTROLLER_E2E_FAIL_STATE}\\\"\" \"$previous\"; then\n"
        "  exit 1\n"
        "fi\n"
        "exec /bin/mv \"$@\"\n",
    )
    _write_executable(
        bin_dir / "python3",
        "#!/usr/bin/env bash\n"
        "if [ -n \"${DRYCONTROLLER_E2E_FAIL_AUTHKEY_FSYNC:-}\" ] "
        "&& [ \"${1:-}\" = - ] "
        "&& [[ \"${2:-}\" == \"$(dirname \"$DRYCONTROLLER_TAILNET_ENV\")/."
        "$(basename \"$DRYCONTROLLER_TAILNET_ENV\").\"*.tmp ]] "
        "&& ! /usr/bin/grep -q '^TS_AUTHKEY=' \"${2:-}\"; then\n"
        "  exit 1\n"
        "fi\n"
        "if [ -n \"${DRYCONTROLLER_E2E_FAIL_BUNDLE_PARENT_FSYNC:-}\" ] "
        "&& [ \"${1:-}\" = - ] "
        "&& [ \"${2:-}\" = \"$(dirname \"$DRYCONTROLLER_ACTIVATION_BUNDLE\")\" ] "
        "&& [ ! -e \"$DRYCONTROLLER_ACTIVATION_BUNDLE\" ]; then\n"
        "  exit 1\n"
        "fi\n"
        f"exec {sys.executable} \"$@\"\n",
    )
    _write_executable(
        bin_dir / "rm",
        "#!/usr/bin/env bash\n"
        "last=''\n"
        "for arg in \"$@\"; do last=\"$arg\"; done\n"
        "if [ -n \"${DRYCONTROLLER_E2E_FAIL_BUNDLE_RM:-}\" ] "
        "&& [ \"$last\" = \"${DRYCONTROLLER_ACTIVATION_BUNDLE:-}\" ]; then\n"
        "  exit 1\n"
        "fi\n"
        "exec /bin/rm \"$@\"\n",
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
        "  printf 'confirm\\n' >> \"$DRYCONTROLLER_E2E_CAPTURE_DIR/events.log\"\n"
        "  [ -n \"$out\" ] && : > \"$out\"\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s' \"$data\" > \"$DRYCONTROLLER_E2E_CAPTURE_DIR/activation-payload.json\"\n"
        "case \"${DRYCONTROLLER_E2E_OPS_KEYS:-strong}\" in\n"
        "  missing) ops_keys='\"edge_admin_api_key\":null,\"edge_control_api_key\":null' ;;\n"
        "  placeholder) ops_keys='\"edge_admin_api_key\":\"change_me_admin\",\"edge_control_api_key\":\"change_me_control\"' ;;\n"
        "  *) ops_keys='\"edge_admin_api_key\":\"edge-admin-e2e\",\"edge_control_api_key\":\"edge-control-e2e\"' ;;\n"
        "esac\n"
        "cat > \"$out\" <<JSON\n"
        "{\"success\":true,\"state\":\"cloud_activated\",\"device_id\":\"EDGE-E2E0001\",\"tenant_id\":\"tenant-e2e\",\"line_id\":\"line-a\",\"node_name\":\"edge-e2e0001\",\"license_payload\":{\"schema\":\"drycontroller.license.v1\",\"signature\":\"signed-production\",\"expires_at\":\"2027-01-01T00:00:00+00:00\"},\"license_public_key\":\"public-key-e2e\",\"license_key_id\":\"test-key\",\"node_enroll_token\":\"node-token-e2e\",\"enroll_token\":\"node-token-e2e\",${ops_keys},\"message\":\"activation_auto_approved\"}\n"
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
    edge_env.write_text(
        "\n".join(
            [
                "UNCHANGED=1",
                "CLOUD_OPS_BASE_URL=http://old-cloud",
                "POSTGRES_PASSWORD=change_me_postgres_password",
                "INFLUXDB_PASSWORD=change_me_influx_password",
                "INFLUXDB_TOKEN=change_me_influx_token",
                "MQTT_PASSWORD_EDGE_API=change_me_mqtt_edge_api",
                "MQTT_PASSWORD_EDGE_DAQ=change_me_mqtt_edge_daq",
                "MQTT_PASSWORD_AI_SOFTSENSOR=change_me_mqtt_ai_softsensor",
                "MQTT_PASSWORD_EDGE_MONITOR=change_me_mqtt_edge_monitor",
                "MQTT_PASSWORD_AI_CTRL_OPTIMIZER=change_me_mqtt_ai_ctrl_optimizer",
                "CONTROL_SIGNATURE_SECRET=change_me_control_signature_secret",
                "MONITOR_OPS_API_KEYS=change_me_admin_api_key",
                "",
            ]
        ),
        encoding="utf-8",
    )

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
    assert not bundle_path.exists()

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
    assert stat.S_IMODE((etc_dir / "tailnet.env").stat().st_mode) == 0o600
    assert stat.S_IMODE((var_dir / "factory-license.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((var_dir / "activation-state.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((var_dir / "device.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((var_dir / ".bootstrapped").stat().st_mode) == 0o600
    assert stat.S_IMODE((etc_dir / "license" / "license.json").stat().st_mode) == 0o640
    assert stat.S_IMODE((etc_dir / "keys" / "license.pub").stat().st_mode) == 0o640
    assert not list(etc_dir.glob(".tailnet.env.*.tmp"))
    assert not list(etc_dir.rglob(".*.tmp"))
    assert not list(var_dir.rglob(".*.tmp"))

    edge_env_lines = edge_env.read_text(encoding="utf-8").splitlines()
    assert "UNCHANGED=1" in edge_env_lines
    assert "EDGE_API_BIND_ADDR=100.100.0.44" in edge_env_lines
    assert "TS_HOSTNAME=edge-e2e0001" in edge_env_lines
    assert 'ADMIN_API_KEYS=["edge-admin-e2e"]' in edge_env_lines
    assert 'CONTROL_API_KEYS=["edge-control-e2e"]' in edge_env_lines
    assert "CLOUD_OPS_BASE_URL=https://ops.example.test" in edge_env_lines
    assert "CLOUD_OPS_ENROLL_TOKEN=node-token-e2e" in edge_env_lines
    assert "CLOUD_OPS_ENROLL_TOKEN_HEADER=X-Enroll-Token" in edge_env_lines
    edge_env_values = dict(line.split("=", 1) for line in edge_env_lines if "=" in line)
    generated_secret_names = (
        "POSTGRES_PASSWORD",
        "INFLUXDB_PASSWORD",
        "INFLUXDB_TOKEN",
        "MQTT_PASSWORD_EDGE_API",
        "MQTT_PASSWORD_EDGE_DAQ",
        "MQTT_PASSWORD_AI_SOFTSENSOR",
        "MQTT_PASSWORD_EDGE_MONITOR",
        "MQTT_PASSWORD_AI_CTRL_OPTIMIZER",
        "CONTROL_SIGNATURE_SECRET",
    )
    generated_secrets = [edge_env_values[name] for name in generated_secret_names]
    assert all(len(value) >= 32 and not value.startswith("change_me") for value in generated_secrets)
    assert len(set(generated_secrets)) == len(generated_secrets)
    assert edge_env_values["MONITOR_OPS_API_KEYS"] == edge_env_values["ADMIN_API_KEYS"]
    assert stat.S_IMODE(edge_env.stat().st_mode) == 0o600
    assert not list(edge_env.parent.glob("..env.*.tmp"))

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
    assert "no activation bundle; skipping tailnet join" in tailnet_rerun.stdout
    assert "TS_AUTHKEY=" not in (etc_dir / "tailnet.env").read_text(encoding="utf-8")

    bundle_path.write_text(json.dumps(_activation_bundle()), encoding="utf-8")
    with (etc_dir / "tailnet.env").open("a", encoding="utf-8") as handle:
        handle.write("TS_AUTHKEY=stale-reusable-key\n")
    activation_rerun = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "activation already complete" in activation_rerun.stdout
    assert (var_dir / ".bootstrapped").is_file()
    assert not bundle_path.exists()
    tailnet_env_after_rerun = (etc_dir / "tailnet.env").read_text(encoding="utf-8")
    assert "TS_AUTHKEY=" not in tailnet_env_after_rerun
    assert "CLOUD_OPS_ENROLL_TOKEN=node-token-e2e" in tailnet_env_after_rerun
    assert "TS_HOSTNAME=edge-e2e0001" in tailnet_env_after_rerun
    edge_env_after_rerun = dict(
        line.split("=", 1)
        for line in edge_env.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert [edge_env_after_rerun[name] for name in generated_secret_names] == generated_secrets

    existing_env = {**env, "DRYCONTROLLER_E2E_VOLUME_STATE": "existing"}
    existing_rerun = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env=existing_env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "activation already complete" in existing_rerun.stdout
    assert edge_env.read_text(encoding="utf-8") == "\n".join(edge_env_lines) + "\n"

    original = edge_env.read_text(encoding="utf-8")
    broken = original.replace(
        f"POSTGRES_PASSWORD={edge_env_values['POSTGRES_PASSWORD']}",
        "POSTGRES_PASSWORD=change_me_postgres_password",
    )
    edge_env.write_text(broken, encoding="utf-8")
    edge_env.chmod(0o600)
    failed_existing = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env=existing_env,
        text=True,
        capture_output=True,
    )
    assert failed_existing.returncode != 0
    assert "credential migration is required" in failed_existing.stderr
    assert edge_env.read_text(encoding="utf-8") == broken
    assert not list(edge_env.parent.glob("..env.*.tmp"))

    partial_env = {**env, "DRYCONTROLLER_E2E_VOLUME_STATE": "partial"}
    failed_partial = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env=partial_env,
        text=True,
        capture_output=True,
    )
    assert failed_partial.returncode != 0
    assert "credential migration is required" in failed_partial.stderr
    assert edge_env.read_text(encoding="utf-8") == broken

    volume_error_env = {**env, "DRYCONTROLLER_E2E_VOLUME_STATE": "error"}
    failed_volume_query = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env=volume_error_env,
        text=True,
        capture_output=True,
    )
    assert failed_volume_query.returncode != 0
    assert "cannot inspect edge runtime storage" in failed_volume_query.stderr
    assert edge_env.read_text(encoding="utf-8") == broken

    edge_env.parent.chmod(0o500)
    try:
        failed_write = subprocess.run(
            ["bash", str(ACTIVATION_SCRIPT)],
            env=env,
            text=True,
            capture_output=True,
        )
    finally:
        edge_env.parent.chmod(0o700)
    assert failed_write.returncode != 0
    assert edge_env.read_text(encoding="utf-8") == broken
    assert not list(edge_env.parent.glob("..env.*.tmp"))


def test_host_activation_retires_bootstrap_material_before_signed_start_and_recovers(tmp_path: Path):
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
    edge_env.write_text(
        'ADMIN_API_KEYS=["change_me_admin_api_key"]\n'
        'CONTROL_API_KEYS=["change_me_control_api_key"]\n'
        "MONITOR_OPS_API_KEYS=change_me_admin_api_key\n",
        encoding="utf-8",
    )
    edge_env.chmod(0o600)
    (edge_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    start_program = tmp_path / "start-active"
    _write_executable(
        start_program,
        "#!/usr/bin/env bash\n"
        "printf 'start\\n' >> \"$DRYCONTROLLER_E2E_CAPTURE_DIR/events.log\"\n"
        "exit \"${DRYCONTROLLER_E2E_START_RC:-0}\"\n",
    )
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
        "DRYCONTROLLER_START_ACTIVE_PROGRAM": str(start_program),
        "DRYCONTROLLER_E2E_START_RC": "1",
    }

    first = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
    )

    assert first.returncode == 75
    assert "bootstrap material retired; signed runtime start will retry" in first.stdout
    assert json.loads((var_dir / "activation-state.json").read_text(encoding="utf-8"))["state"] == (
        "formal_tailnet_joined"
    )
    assert not bundle_path.exists()
    assert "TS_AUTHKEY=" not in (etc_dir / "tailnet.env").read_text(encoding="utf-8")
    assert capture_dir.joinpath("events.log").read_text(encoding="utf-8").splitlines() == [
        "confirm",
        "start",
    ]
    assert not (var_dir / ".bootstrapped").exists()

    capture_dir.joinpath("activation-payload.json").unlink()
    capture_dir.joinpath("events.log").unlink()
    recovered = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env={**env, "DRYCONTROLLER_E2E_START_RC": "0"},
        text=True,
        capture_output=True,
        check=True,
    )

    assert "activation bundle consumed; resuming state=formal_tailnet_joined" in recovered.stdout
    assert "activation complete" in recovered.stdout
    assert json.loads((var_dir / "activation-state.json").read_text(encoding="utf-8"))["state"] == (
        "formal_license_active"
    )
    assert not capture_dir.joinpath("activation-payload.json").exists()
    assert capture_dir.joinpath("events.log").read_text(encoding="utf-8").splitlines() == [
        "confirm",
        "start",
    ]
    assert (var_dir / ".bootstrapped").is_file()
    assert not bundle_path.exists()
    assert not list(etc_dir.rglob(".*.tmp"))
    assert not list(var_dir.rglob(".*.tmp"))


def test_host_activation_keeps_bootstrap_material_when_recovery_state_write_fails(tmp_path: Path):
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
    edge_env.write_text(
        'ADMIN_API_KEYS=["change_me_admin_api_key"]\n'
        'CONTROL_API_KEYS=["change_me_control_api_key"]\n',
        encoding="utf-8",
    )
    edge_env.chmod(0o600)
    (edge_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    start_marker = tmp_path / "start-active-called"
    start_program = tmp_path / "start-active"
    _write_executable(start_program, f"#!/usr/bin/env bash\ntouch {start_marker}\n")

    result = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env={
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
            "DRYCONTROLLER_START_ACTIVE_PROGRAM": str(start_program),
            "DRYCONTROLLER_E2E_FAIL_STATE": "formal_tailnet_joined",
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode == 75
    assert "recovery state could not be persisted; keeping bootstrap material" in result.stdout
    assert bundle_path.exists()
    assert "TS_AUTHKEY=tskey-bootstrap" in (etc_dir / "tailnet.env").read_text(encoding="utf-8")
    assert not start_marker.exists()
    assert capture_dir.joinpath("events.log").read_text(encoding="utf-8").splitlines() == ["confirm"]
    assert json.loads((var_dir / "activation-state.json").read_text(encoding="utf-8"))["state"] == (
        "cloud_activated"
    )
    assert not list(etc_dir.rglob(".*.tmp"))
    assert not list(var_dir.rglob(".*.tmp"))


@pytest.mark.parametrize(
    ("failure_flag", "expected_message", "bundle_remains", "authkey_remains"),
    [
        (
            "DRYCONTROLLER_E2E_FAIL_AUTHKEY_FSYNC",
            "bootstrap authkey retirement failed; cleanup will retry",
            True,
            True,
        ),
        (
            "DRYCONTROLLER_E2E_FAIL_BUNDLE_RM",
            "activation bundle retirement was not durable; cleanup will retry",
            True,
            False,
        ),
        (
            "DRYCONTROLLER_E2E_FAIL_BUNDLE_PARENT_FSYNC",
            "activation bundle retirement was not durable; cleanup will retry",
            False,
            False,
        ),
    ],
)
def test_host_activation_preserves_recovery_state_on_post_confirm_cleanup_failure(
    tmp_path: Path,
    failure_flag: str,
    expected_message: str,
    bundle_remains: bool,
    authkey_remains: bool,
):
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
    edge_env.write_text(
        'ADMIN_API_KEYS=["change_me_admin_api_key"]\n'
        'CONTROL_API_KEYS=["change_me_control_api_key"]\n',
        encoding="utf-8",
    )
    edge_env.chmod(0o600)
    (edge_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    start_marker = tmp_path / "start-active-called"
    start_program = tmp_path / "start-active"
    _write_executable(start_program, f"#!/usr/bin/env bash\ntouch {start_marker}\n")
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
        "DRYCONTROLLER_START_ACTIVE_PROGRAM": str(start_program),
        failure_flag: "1",
    }

    failed_cleanup = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
    )

    assert failed_cleanup.returncode == 75
    assert expected_message in failed_cleanup.stdout
    assert json.loads((var_dir / "activation-state.json").read_text(encoding="utf-8"))["state"] == (
        "formal_tailnet_joined"
    )
    assert bundle_path.exists() is bundle_remains
    assert ("TS_AUTHKEY=tskey-bootstrap" in (etc_dir / "tailnet.env").read_text(encoding="utf-8")) is authkey_remains
    assert not start_marker.exists()
    assert capture_dir.joinpath("events.log").read_text(encoding="utf-8").splitlines() == ["confirm"]

    capture_dir.joinpath("events.log").unlink()
    recovered = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env={key: value for key, value in env.items() if key != failure_flag},
        text=True,
        capture_output=True,
        check=True,
    )

    assert "activation complete" in recovered.stdout
    assert json.loads((var_dir / "activation-state.json").read_text(encoding="utf-8"))["state"] == (
        "formal_license_active"
    )
    assert start_marker.exists()
    assert capture_dir.joinpath("events.log").read_text(encoding="utf-8").splitlines() == ["confirm"]


@pytest.mark.parametrize("ops_key_mode", ["missing", "placeholder"])
def test_host_activation_rejects_weak_cloud_ops_keys_before_signed_start(tmp_path: Path, ops_key_mode: str):
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
    original_env = (
        'ADMIN_API_KEYS=["change_me_admin_api_key"]\n'
        'CONTROL_API_KEYS=["change_me_control_api_key"]\n'
        "POSTGRES_PASSWORD=change_me_postgres_password\n"
    )
    edge_env.write_text(original_env, encoding="utf-8")
    edge_env.chmod(0o600)
    (edge_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    start_marker = tmp_path / "start-active-called"
    start_program = tmp_path / "start-active"
    _write_executable(start_program, f"#!/usr/bin/env bash\ntouch {start_marker}\n")

    result = subprocess.run(
        ["bash", str(ACTIVATION_SCRIPT)],
        env={
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
            "DRYCONTROLLER_E2E_OPS_KEYS": ops_key_mode,
            "DRYCONTROLLER_START_ACTIVE_PROGRAM": str(start_program),
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "activation response missing strong edge_admin_api_key" in result.stderr
    assert not start_marker.exists()
    assert edge_env.read_text(encoding="utf-8") == original_env
    assert stat.S_IMODE(edge_env.stat().st_mode) == 0o600
    assert not list(edge_env.parent.glob("..env.*.tmp"))
    assert bundle_path.exists()


def test_host_tailnet_materializes_bundle_env_durably(tmp_path: Path):
    bin_dir = _fake_path(tmp_path)
    bundle_path = tmp_path / "activation-bundle.json"
    bundle_path.write_text(json.dumps(_activation_bundle()), encoding="utf-8")
    env_file = tmp_path / "etc" / "tailnet.env"

    result = subprocess.run(
        ["bash", str(TAILNET_SCRIPT)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DRYCONTROLLER_TAILNET_ENV": str(env_file),
            "DRYCONTROLLER_ACTIVATION_BUNDLE": str(bundle_path),
            "DRYCONTROLLER_E2E_CAPTURE_DIR": str(tmp_path / "capture"),
        },
        text=True,
        capture_output=True,
        check=True,
    )

    assert "materialized tailnet env from activation bundle" in result.stdout
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "TS_AUTHKEY=tskey-bootstrap" in env_file.read_text(encoding="utf-8")
    assert not list(env_file.parent.glob(".tailnet.env.*.tmp"))


def test_host_tailnet_secures_existing_env_mode(tmp_path: Path):
    bin_dir = _fake_path(tmp_path)
    bundle_path = tmp_path / "activation-bundle.json"
    bundle_path.write_text(json.dumps(_activation_bundle()), encoding="utf-8")
    env_file = tmp_path / "tailnet.env"
    env_file.write_text(
        "TS_LOGIN_SERVER=https://headscale.example.test\n"
        "TS_HOSTNAME=existing-edge\n"
        "TS_AUTHKEY=existing-key\n"
        "CLOUD_OPS_BASE_URL=https://ops.example.test\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)

    subprocess.run(
        ["bash", str(TAILNET_SCRIPT)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "DRYCONTROLLER_TAILNET_ENV": str(env_file),
            "DRYCONTROLLER_ACTIVATION_BUNDLE": str(bundle_path),
            "DRYCONTROLLER_E2E_CAPTURE_DIR": str(tmp_path / "capture"),
        },
        text=True,
        capture_output=True,
        check=True,
    )

    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600

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
