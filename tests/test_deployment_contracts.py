import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _compose_services() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


def _env_map(service: dict) -> dict[str, str]:
    env = service.get("environment", {})
    if isinstance(env, dict):
        return env
    result: dict[str, str] = {}
    for item in env:
        key, _, value = str(item).partition("=")
        result[key] = value
    return result


def _env_example() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (ROOT / "env.example").read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def _active_runtime_files() -> dict[str, tuple[str, int]]:
    path = ROOT / "infra/bootstrap/install-edge-app-runtime.py"
    spec = importlib.util.spec_from_file_location("deployment_edge_app_installer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RUNTIME_FILES


def test_plc_deployment_defaults_match_rtu_over_tcp_contract():
    edge_daq_env = _env_map(_compose_services()["edge-daq"])
    example = _env_example()

    assert edge_daq_env["PLC_PORT"] == "${PLC_PORT:-2022}"
    assert edge_daq_env["MODBUS_FRAMER"] == "${MODBUS_FRAMER:-rtu}"
    assert example["PLC_PORT"] == "2022"
    assert example["PLC_SLAVE_ID"] == "1"
    assert example["MODBUS_FRAMER"] == "rtu"


def test_edge_daq_binds_line_identity_and_persists_publish_buffer():
    edge_daq = _compose_services()["edge-daq"]
    environment = _env_map(edge_daq)
    example = _env_example()

    assert environment["LINE_ID"] == "${LINE_ID:?missing LINE_ID}"
    assert environment["BUFFER_DIR"] == "/data/edge-daq"
    assert example["LINE_ID"] == "L1"
    assert "/var/lib/drycontroller/edge-daq/data:/data/edge-daq" in edge_daq["volumes"]


def test_runtime_excludes_retired_services_and_product_surfaces():
    retired_services = {"license-service", "ai-softsensor", "ai-ctrl-optimizer"}
    retired_paths = {
        ".gitea/workflows/edge-gate-c-build.yml",
        ".gitea/workflows/edge-host-release.yml",
        ".gitea/workflows/edge-release-handoff-recovery.yml",
        ".gitea/workflows/edge-release-public-keys.yml",
        ".gitea/workflows/edge-software-release.yml",
        "configs/ai-config.yaml",
        "infra/autoinstall/user-data",
        "infra/autoinstall/post-install.sh",
        "infra/bootstrap/install-edge-host.sh",
        "infra/host-release-runner/host_release_runner.py",
        "infra/host-release-runner/host_release_engine.py",
        "infra/ota-helper/release_engine.py",
        "infra/prometheus/rules/ai-softsensor.yml",
        "scripts/build-os-installer-release.sh",
        "scripts/write-os-installer-usb.sh",
        "scripts/ci/build-host-release.sh",
        "scripts/ci/build-software-release.sh",
        "scripts/ci/software_release_builder.py",
        "scripts/export_training_dataset.py",
        "scripts/import_lab_results.py",
        "services/edge-api/app/api/v1/ai.py",
        "services/edge-api/app/services/ai_softsensor.py",
    }

    assert all(not (ROOT / "services" / name).exists() for name in retired_services)
    assert all(not (ROOT / path).exists() for path in retired_paths)
    assert set(_compose_services()) == {
        "alertmanager",
        "edge-api",
        "edge-daq",
        "edge-monitor",
        "influxdb",
        "mosquitto",
        "mosquitto-init",
        "postgres",
        "prometheus",
    }


def test_license_verification_runs_in_edge_api_without_resident_service():
    services = _compose_services()
    edge_api = services["edge-api"]

    assert "license-service" not in services
    assert "/etc/drycontroller/license:/etc/drycontroller/license:ro" in edge_api["volumes"]
    assert "/etc/drycontroller/keys:/etc/drycontroller/keys:ro" in edge_api["volumes"]

    edge_api_env = _env_map(edge_api)
    assert edge_api_env["LICENSE_FILE_PATH"] == "${LICENSE_FILE_PATH:-/etc/drycontroller/license/license.json}"
    assert edge_api_env["LICENSE_TRUST_POLICY_PATH"] == (
        "${LICENSE_TRUST_POLICY_PATH:-/etc/drycontroller/keys/license-trust/policy.json}"
    )


def test_edge_monitor_ops_auth_contract_supports_edge_api_admin_key_proxy():
    edge_monitor_env = _env_map(_compose_services()["edge-monitor"])

    assert edge_monitor_env["MONITOR_API_KEY_HEADER"] == "${API_KEY_HEADER:-X-API-Key}"
    assert edge_monitor_env["MONITOR_OPS_API_KEYS"] == "${MONITOR_OPS_API_KEYS}"
    assert edge_monitor_env["ADMIN_API_KEYS"] == "${ADMIN_API_KEYS}"


def test_tailnet_cidr_is_transport_not_readonly_authorization():
    config = (ROOT / "services/edge-api/app/core/config.py").read_text(encoding="utf-8")

    assert '"100.64.0.0/10"' not in config


def test_edge_api_entrypoint_uses_runtime_postgres_settings_for_migration_backup():
    entrypoint = (ROOT / "services/edge-api/entrypoint.sh").read_text(encoding="utf-8")

    assert "from app.core.config import settings" in entrypoint
    assert "db_url = settings.database_url" in entrypoint
    assert "using POSTGRES_* settings for pre-migration backup" in entrypoint


def test_business_containers_have_no_docker_mutation_surface():
    services = _compose_services()

    assert "docker-socket-proxy" not in services
    assert "docker-socket-proxy-ota" not in services
    assert "edge-ota-agent" not in services
    for service in services.values():
        assert all("docker.sock" not in str(volume) for volume in service.get("volumes", []))
        assert "DOCKER_HOST" not in _env_map(service)

    assert "/run/drycontroller-ota:/run/drycontroller-ota:ro" in services["edge-api"]["volumes"]


def test_root_ota_helper_uses_only_edge_app_v1_runtime():
    helper = (ROOT / "infra/ota-helper/edge_app_ota_helper.py").read_text(encoding="utf-8")
    apply = (ROOT / "infra/ota-helper/edge-app-ota-apply").read_text(encoding="utf-8")
    release_engine = (ROOT / "infra/ota-helper/edge_app_release.py").read_text(encoding="utf-8")
    unit = (ROOT / "infra/ota-helper/drycontroller-edge-app-ota-helper.service").read_text(
        encoding="utf-8"
    )

    assert 'APPLY_PROGRAM = Path("/usr/libexec/drycontroller/ota-apply")' in helper
    assert 'PROJECT_DIR = Path("/opt/drycontroller-host/current")' in helper
    assert 'OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")' in helper
    assert "sys.argv" not in helper
    assert "os.getenv" not in helper
    assert "if len(sys.argv) != 1:" in apply
    assert "ReleaseEngine().apply()" in apply
    assert "from edge_app_release import ReleaseEngine" in apply
    assert 'RELEASE_JOURNAL_SCHEMA = "drycontroller.edge-app-release-journal.v2"' in release_engine
    assert 'SOFTWARE_RELEASE_SCHEMA = "drycontroller.app-release.v2"' in release_engine
    assert 'EDGE_SERVICES = ("edge-api", "edge-daq")' in release_engine
    assert "verify_manifest_signature(manifest" in release_engine
    assert "shell=True" not in release_engine
    assert "os.getenv" not in release_engine
    assert "release_engine" not in helper
    assert "User=root" in unit
    assert "Group=root" in unit
    assert "ExecStart=/usr/bin/python3 /usr/libexec/drycontroller/drycontroller_ota_helper.py" in unit


def test_active_runtime_installer_contains_no_host_release_source():
    runtime_files = _active_runtime_files()

    assert not any("host-release" in source or "host_release" in source for source in runtime_files)
    assert runtime_files["scripts/manage-license-transaction.py"] == (
        "/usr/libexec/drycontroller/manage-license-transaction.py",
        0o755,
    )


def test_edge_api_tailnet_preflight_uses_standalone_observation():
    services = _compose_services()
    edge_api = services["edge-api"]
    edge_api_env = _env_map(edge_api)
    observer = (ROOT / "services/edge-api/app/services/tailnet_observation.py").read_text(
        encoding="utf-8"
    )
    ops = (ROOT / "services/edge-api/app/api/v1/ops.py").read_text(encoding="utf-8")

    assert edge_api_env["TAILSCALE_EXPECTED_VERSION"] == "${TAILSCALE_EXPECTED_VERSION:-}"
    assert "TS_HOSTNAME" not in edge_api_env
    assert all("tailscaled.sock" not in str(volume) for volume in edge_api["volumes"])
    assert (
        "/run/drycontroller-tailnet-observation:/run/drycontroller-tailnet-observation:ro"
        in edge_api["volumes"]
    )
    assert 'Path("/run/drycontroller-tailnet-observation/status.json")' in observer
    assert "from app.services.tailnet_observation import load_tailnet_observation" in ops
    assert not (ROOT / "services/edge-api/app/services/host_release_ops.py").exists()
    assert "/host-release/" not in ops


def test_factory_device_prep_keeps_lane_contract_and_secret_free_surface():
    prep = (ROOT / "infra/bootstrap/prep-factory-device.sh").read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts/prep-device-from-mac.sh").read_text(encoding="utf-8")
    example = (ROOT / "infra/bootstrap/factory-device-public-config.example.json").read_text(
        encoding="utf-8"
    )

    assert '"169.254.203.2/30"' in example
    assert "169.254.203.2/30" in prep and "169.254.203.2:8765" in prep
    assert "factory-closed.json" in prep and "/var/lib/drycontroller/device.json" in prep
    assert "PasswordAuthentication no" in prep
    assert "install-device-identity-runtime.py" in prep
    assert "initialize-runtime-config.py" in prep
    assert "drycontroller-factory-identity.service" in prep
    assert "xorriso" not in prep and "dd " not in prep and "diskutil" not in prep

    assert "DEVICE_IDENTITY_SIGNING_PRIVATE_KEY" in wrapper
    assert "print(base64.b64encode(public).decode())" in wrapper
    assert "approved-wheelhouse-manifest-sha256" in prep
    assert "status --porcelain" in wrapper
    install_lines = [line for line in prep.splitlines() if "apt-get install" in line]
    assert install_lines and all("python3.12-venv" in line for line in install_lines)
    assert "docker-ce" not in prep and "docker.io" not in prep and "containerd" not in prep
