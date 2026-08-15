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

    assert 'APPLY_PROGRAM = Path("/opt/drycontroller-host/current/infra/ota-helper/ota-apply")' in helper
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
    assert "ExecStart=/usr/bin/python3 /opt/drycontroller-host/current/infra/ota-helper/drycontroller_ota_helper.py" in unit
    assert "ReadWritePaths=/run/drycontroller-ota /var/lib/drycontroller-ota /var/log/drycontroller-ota" in unit


def test_host_release_runner_is_outside_its_signed_update_surface():
    unit = (ROOT / "infra/host-release-runner/drycontroller-host-release-runner.service").read_text(encoding="utf-8")
    engine = (ROOT / "infra/host-release-runner/host_release_engine.py").read_text(encoding="utf-8")
    builder = (ROOT / "scripts/prepare-host-release.py").read_text(encoding="utf-8")

    assert "ExecStart=/usr/libexec/drycontroller/host-release-runner" in unit
    assert "ReadWritePaths=" in unit
    writable = next(line for line in unit.splitlines() if line.startswith("ReadWritePaths="))
    assert "/usr/libexec" not in writable
    assert "/opt/drycontroller-host" in writable
    assert "/opt/Drycontroller-edge" not in writable
    assert "compatibility_target != self.paths.current_link" in engine
    assert '"infra/host-release-runner/' not in builder
    assert '"configs/keys/"' in engine
    assert '"--lock-fd"' in engine
    assert "pass_fds=inherited_fds" in engine


def test_rb6_installer_seeds_only_the_signed_public_h1_t1_s1_path():
    installer = (ROOT / "infra/bootstrap/install-edge-host.sh").read_text(encoding="utf-8")
    post_install = (ROOT / "infra/autoinstall/post-install.sh").read_text(encoding="utf-8")
    user_data = (ROOT / "infra/autoinstall/user-data").read_text(encoding="utf-8")
    release_builder = (ROOT / "scripts/build-os-installer-release.sh").read_text(encoding="utf-8")
    usb_writer = (ROOT / "scripts/write-os-installer-usb.sh").read_text(encoding="utf-8")

    for required in (
        "--host-release",
        "--host-image-bundle",
        "--software-release",
        "--software-image-bundle",
        "--host-cosign-public-key",
        "--software-cosign-public-key",
        "--device-identity-cloud-key",
        "--identity-wheelhouse",
        "--public-config",
        "/usr/libexec/drycontroller/initialize-initial-release",
        "/usr/libexec/drycontroller/host-release-runner",
    ):
        assert required in installer
    for retired in ("--initial-release", "--host-active", "--cosign-public-key"):
        assert retired not in installer
    assert "[ -c /dev/tpmrm0 ]" in installer
    assert 'password: "!"' in user_data
    assert "allow-pw: false" in user_data
    assert "      all-en:" in user_data
    assert "      all-eth:" in user_data
    assert "wifi.env" not in user_data
    assert not (ROOT / "infra/autoinstall/password.env.example").exists()
    assert "infra/autoinstall/password.env" not in (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "activation-bundle.json" not in user_data
    assert "host-release-seed.json" in post_install
    assert "software-release-seed.json" in post_install
    assert "host-active.json" not in post_install
    assert 'VERSION_ID:-}" = "24.04"' in release_builder
    assert "release worktree must be clean" in release_builder
    assert "formal builder cosign checksum is not approved" in release_builder
    assert 'export PATH="$PINNED_TOOL_DIR:$PATH"' in release_builder
    assert "scripts/build-device-identity-wheelhouse.sh" in release_builder
    assert "scripts/build-autoinstall-offline-apt-repo.sh" in release_builder
    assert "scripts/create-os-installer-manifest.py" in release_builder
    assert release_builder.count("scripts/public-material-scan.py") == 2
    assert "--surfaces source host software" in release_builder
    assert "--surfaces iso filesystem" in release_builder
    assert "--public-scan-report" in release_builder
    assert "unsquashfs" in release_builder
    assert "zstd" in release_builder
    assert "maker.build_iso" in release_builder
    assert "cosign sign-blob" in release_builder
    assert "cosign verify-blob" in release_builder
    assert "--installer-public-key" in usb_writer
    assert "scripts/public-material-scan.py" in usb_writer
    assert usb_writer.count("scripts/stable-release-artifact.py snapshot") == 4
    assert usb_writer.count("--maximum-size") == 4
    assert usb_writer.count("--timeout-seconds") == 4
    assert "--maximum-size 16777216" in usb_writer
    assert "--maximum-size 1048576" in usb_writer
    assert "--maximum-size 65536" in usb_writer
    assert "--maximum-size 17179869184" in usb_writer
    assert "scripts/stable-release-artifact.py verify-fd" in usb_writer
    assert "if=/dev/fd/9" in usb_writer
    assert '"if=$ISO"' not in usb_writer
    assert '"$EXPECTED_ISO_SIZE" "$EXPECTED_ISO_SHA256"' in usb_writer
    assert "--surfaces iso filesystem" in usb_writer
    assert "drycontroller.os-installer-release.v3" in usb_writer
    assert "--base-iso-sha256" in release_builder
    assert "--approved-base-iso-sha256" in release_builder
    assert "e907d92eeec9df64163a7e454cbc8d7755e8ddc7ed42f99dbc80c40f1a138433" in release_builder
    assert "base ISO checksum is not on the reviewed allowlist" in release_builder
    assert "require_cmd unsquashfs" in usb_writer
    assert "require_cmd zstd" in usb_writer
    assert "signature_verifier=Path(host_cosign)" in usb_writer
    assert "USB raw compare passed" in usb_writer


def test_source_bundle_uses_only_tracked_commit_files_and_rejects_macos_metadata():
    builder = (ROOT / "tools" / "build-bundle-images.sh").read_text(encoding="utf-8")

    assert 'git archive --format=tar.gz --output="$BUNDLE" HEAD -- "${SOURCE_BUNDLE_PATHS[@]}"' in builder
    for required in (
        "env.example",
        "infra/bootstrap/install-edge-host.sh",
        "infra/bootstrap/installer_public_config.py",
        "infra/host-release-runner/host_release_engine.py",
        "infra/ota-helper/drycontroller-ota-helper.service",
        "services/device-identity/identity_service",
    ):
        assert required in builder
    assert "tar -czf" not in builder
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
