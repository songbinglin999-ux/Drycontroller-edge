import io
import subprocess
import sys
import tarfile
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


def test_plc_deployment_defaults_match_rtu_over_tcp_contract():
    edge_daq_env = _env_map(_compose_services()["edge-daq"])
    example = _env_example()

    assert edge_daq_env["PLC_PORT"] == "${PLC_PORT:-2022}"
    assert edge_daq_env["MODBUS_FRAMER"] == "${MODBUS_FRAMER:-rtu}"
    assert example["PLC_PORT"] == "2022"
    assert example["PLC_SLAVE_ID"] == "1"
    assert example["MODBUS_FRAMER"] == "rtu"


def test_license_install_contract_keeps_public_key_anchor_read_only():
    services = _compose_services()
    edge_api = services["edge-api"]
    license_service = services["license-service"]

    assert "/etc/drycontroller/license:/etc/drycontroller/license" in edge_api["volumes"]
    assert "/etc/drycontroller/keys:/etc/drycontroller/keys:ro" in edge_api["volumes"]
    assert "/etc/drycontroller/license:/etc/drycontroller/license:ro" in license_service["volumes"]
    assert "/etc/drycontroller/keys:/etc/drycontroller/keys:ro" in license_service["volumes"]

    edge_api_env = _env_map(edge_api)
    assert edge_api_env["LICENSE_FILE_PATH"] == "${LICENSE_FILE_PATH:-/etc/drycontroller/license/license.json}"
    assert edge_api_env["LICENSE_PUBLIC_KEY_PATH"] == "${LICENSE_PUBLIC_KEY_PATH:-/etc/drycontroller/keys/license.pub}"


def test_edge_monitor_ops_auth_contract_supports_edge_api_admin_key_proxy():
    edge_monitor_env = _env_map(_compose_services()["edge-monitor"])

    assert edge_monitor_env["MONITOR_API_KEY_HEADER"] == "${API_KEY_HEADER:-X-API-Key}"
    assert edge_monitor_env["MONITOR_OPS_API_KEYS"] == "${MONITOR_OPS_API_KEYS}"
    assert edge_monitor_env["ADMIN_API_KEYS"] == "${ADMIN_API_KEYS}"


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


def test_root_ota_helper_uses_fixed_program_and_systemd_boundary():
    helper = (ROOT / "infra/ota-helper/drycontroller_ota_helper.py").read_text(encoding="utf-8")
    apply = (ROOT / "infra/ota-helper/ota-apply").read_text(encoding="utf-8")
    release_engine = (ROOT / "infra/ota-helper/release_engine.py").read_text(encoding="utf-8")
    unit = (ROOT / "infra/ota-helper/drycontroller-ota-helper.service").read_text(encoding="utf-8")

    assert 'APPLY_PROGRAM = Path("/usr/libexec/drycontroller/ota-apply")' in helper
    assert 'PROJECT_DIR = Path("/opt/Drycontroller-edge")' in helper
    assert 'OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")' in helper
    assert "sys.argv" not in helper
    assert "os.getenv" not in helper
    assert "if len(sys.argv) != 1:" in apply
    assert "ReleaseEngine().apply()" in apply
    assert "pre-check" not in apply
    assert "post-check" not in apply
    assert 'RELEASE_JOURNAL_SCHEMA = "drycontroller.release-journal.v1"' in release_engine
    assert 'SOFTWARE_RELEASE_SCHEMA = "drycontroller.software-release.v1"' in release_engine
    assert '"--pull", "never"' in release_engine
    assert '"verify-blob"' in release_engine
    assert '"verify"' in release_engine
    assert "shell=True" not in release_engine
    assert "os.getenv" not in release_engine
    assert "User=root" in unit
    assert "Group=root" in unit
    assert "ExecStart=/usr/bin/python3 /usr/libexec/drycontroller/drycontroller_ota_helper.py" in unit
    assert "ReadWritePaths=/run/drycontroller-ota /var/lib/drycontroller-ota /var/log/drycontroller-ota" in unit


def test_source_bundle_excludes_and_rejects_macos_metadata():
    builder = (ROOT / "tools" / "build-bundle-images.sh").read_text(encoding="utf-8")

    assert "COPYFILE_DISABLE=1 tar -czf" in builder
    assert "--exclude='._*'" in builder
    assert "--exclude='__MACOSX'" in builder
    assert 'python3 scripts/validate-source-bundle.py "$BUNDLE"' in builder
    assert "source bundle contains forbidden macOS metadata" in builder


def _write_source_bundle(path: Path, member_name: str) -> None:
    payload = b"bundle-test"
    member = tarfile.TarInfo(member_name)
    member.size = len(payload)
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(payload))


def test_source_bundle_validator_accepts_clean_archive(tmp_path):
    bundle = tmp_path / "clean.tar.gz"
    _write_source_bundle(bundle, "./infra/prometheus/rules/ai-softsensor.yml")

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate-source-bundle.py"), str(bundle)],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert bundle.is_file()


def test_source_bundle_validator_removes_forbidden_or_corrupt_archive(tmp_path):
    for index, member_name in enumerate(
        (
            "./infra/prometheus/rules/._ai-softsensor.yml",
            "./infra/prometheus/rules/.DS_Store",
            "./__MACOSX/infra/prometheus/rules/ai-softsensor.yml",
        )
    ):
        bundle = tmp_path / f"forbidden-{index}.tar.gz"
        _write_source_bundle(bundle, member_name)
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "validate-source-bundle.py"), str(bundle)],
            text=True,
            capture_output=True,
        )
        assert result.returncode == 1
        assert not bundle.exists()

    corrupt = tmp_path / "corrupt.tar.gz"
    _write_source_bundle(corrupt, "./large-member.bin")
    corrupt.write_bytes(corrupt.read_bytes()[:-8])
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "validate-source-bundle.py"), str(corrupt)],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert not corrupt.exists()
