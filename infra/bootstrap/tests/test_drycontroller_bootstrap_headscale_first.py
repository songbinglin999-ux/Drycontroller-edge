import subprocess
from pathlib import Path


TAILNET_SCRIPT = Path(__file__).resolve().parents[1] / "drycontroller-host-tailnet.sh"
ACTIVATION_SCRIPT = Path(__file__).resolve().parents[1] / "drycontroller-host-activation.sh"
INSTALL_SCRIPT = Path(__file__).resolve().parents[1] / "install-edge-host.sh"
AUTOINSTALL_DIR = Path(__file__).resolve().parents[2] / "autoinstall"
USB_MAKER = Path(__file__).resolve().parents[3] / "tools" / "os-installer-usb-maker" / "os_installer_usb_maker.py"
PREFLIGHT_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "preflight.sh"
RELEASE_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "build-os-installer-release.sh"
USB_WRITE_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "write-os-installer-usb.sh"
LEGACY_TAILNET_DIR = Path(__file__).resolve().parents[2] / "tailnet"
EDGE_API_ACTIVATION_API = (
    Path(__file__).resolve().parents[3] / "services" / "edge-api" / "app" / "api" / "v1" / "activation.py"
)
LEGACY_EDGE_API_ACTIVATION_CLI = (
    Path(__file__).resolve().parents[3] / "services" / "edge-api" / "app" / "services" / "activation_cli.py"
)


def test_host_tailnet_allows_existing_formal_state_without_bootstrap_authkey():
    text = TAILNET_SCRIPT.read_text(encoding="utf-8")

    assert 'BUNDLE_FILE="${DRYCONTROLLER_ACTIVATION_BUNDLE:-/etc/drycontroller/activation-bundle.json}"' in text
    assert "materialize_activation_bundle" in text
    assert '[ -f "$ENV_FILE" ] && return 0' not in text
    assert 'if [ ! -f "$BUNDLE_FILE" ]; then' in text
    assert "no activation bundle; skipping tailnet join" in text
    assert 'existing_ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"' in text
    assert '[ -n "${TS_AUTHKEY:-}" ] || die "TS_AUTHKEY is required when no existing tailnet IPv4 is available"' in text
    assert text.index('existing_ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"') < text.index(
        '[ -n "${TS_AUTHKEY:-}" ] || die "TS_AUTHKEY is required when no existing tailnet IPv4 is available"'
    )


def test_host_activation_agent_binds_current_tailnet_node_without_formal_reauth():
    text = ACTIVATION_SCRIPT.read_text(encoding="utf-8")

    assert 'BUNDLE_FILE="${DRYCONTROLLER_ACTIVATION_BUNDLE:-/etc/drycontroller/activation-bundle.json}"' in text
    assert "drycontroller.activation-bundle.v1" in text
    assert "if not env_path.exists():" in text
    assert 'if [ ! -f "$BUNDLE_FILE" ]; then' in text
    assert "no activation bundle; skipping activation" in text
    assert "activation bundle must materialize it" in text
    assert "/api/v1/factory-activation" in text
    assert '"factory_license": json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))' in text
    assert "factory_license_fingerprint" in text
    assert "formal_ts_authkey" not in text
    assert '"pending_ops_approval"' in text
    assert "factory activation is pending ops approval; bootstrap tailnet preserved" in text
    assert 'exit 75' in text
    assert "tailscale up --reset --authkey" not in text
    assert "bind_current_tailnet_node" in text
    assert 'tailscale set --hostname="${hostname}"' in text
    assert "sync_tailnet_env_after_activation" in text
    assert "sync_edge_env_after_activation" in text
    assert '"EDGE_API_BIND_ADDR"] = ip' in text
    assert 'for key in ("TS_LOGIN_SERVER", "CLOUD_OPS_BASE_URL", "CLOUD_OPS_ENROLL_TOKEN", "CLOUD_OPS_ENROLL_TOKEN_HEADER")' in text
    assert "updates[key] = value" in text
    assert 'EDGE_ENV_FILE="${EDGE_ENV_FILE:-${EDGE_ROOT}/.env}"' in text
    assert "reapply_edge_compose_binding" in text
    assert "confirm_current_tailnet_binding" in text
    assert "/bind-tailnet-node" in text
    assert 'BOOTSTRAP_SENTINEL="${DRYCONTROLLER_BOOTSTRAP_SENTINEL:-/var/lib/drycontroller/.bootstrapped}"' in text
    assert "mark_bootstrap_complete" in text
    assert "start_host_enroll_service_if_configured" in text
    assert 'grep -q \'^CLOUD_OPS_ENROLL_TOKEN=\' "$ENV_FILE"' in text
    assert "systemctl enable drycontroller-enroll.service" in text
    assert "systemctl restart drycontroller-enroll.service" in text
    assert "save_runtime_credentials" in text
    assert "save_edge_ops_credentials" in text
    assert 'resp.get("node_enroll_token") or resp.get("enroll_token")' in text
    assert 'resp.get("edge_admin_api_key")' in text
    assert 'resp.get("edge_control_api_key")' in text
    assert "import shlex" in text
    assert 'updates["CLOUD_OPS_ENROLL_TOKEN"] = node_token' in text
    assert 'updates["ADMIN_API_KEYS"] = json.dumps([admin_key]' in text
    assert 'updates["CONTROL_API_KEYS"] = json.dumps([control_key]' in text
    assert 'updates["TENANT_ID"] = resp["tenant_id"].strip()' in text
    assert 'updates["LINE_ID"] = resp["line_id"].strip()' in text
    assert 'write_state "cloud_activated" "tailnet_node_bind_failed"' in text
    assert 'log "production license installed; current tailnet node bind failed, activation will retry"' in text


def test_host_activation_syncs_cloud_runtime_env_into_edge_env(tmp_path):
    text = ACTIVATION_SCRIPT.read_text(encoding="utf-8")
    start = text.index("sync_edge_env_after_activation() {")
    end = text.index("\nreapply_edge_compose_binding() {", start)
    function_text = text[start:end]

    tailnet_env = tmp_path / "tailnet.env"
    tailnet_env.write_text(
        "\n".join(
            [
                "TS_LOGIN_SERVER=https://headscale.example:8443",
                "TS_HOSTNAME=edge-bootstrap",
                "CLOUD_OPS_BASE_URL=http://100.100.0.1:8088",
                "CLOUD_OPS_ENROLL_TOKEN=node-token-1",
                "CLOUD_OPS_ENROLL_TOKEN_HEADER=X-Enroll-Token",
                "",
            ]
        ),
        encoding="utf-8",
    )
    edge_env = tmp_path / ".env"
    edge_env.write_text(
        "\n".join(
            [
                "UNCHANGED=1",
                "CLOUD_OPS_BASE_URL=http://old-cloud",
                "CLOUD_OPS_ENROLL_TOKEN=old-token",
                "",
            ]
        ),
        encoding="utf-8",
    )
    runner = tmp_path / "run-sync.sh"
    runner.write_text(
        "\n".join(
            [
                "set -euo pipefail",
                "log() { :; }",
                function_text,
                f"ENV_FILE={tailnet_env}",
                f"EDGE_ENV_FILE={edge_env}",
                'sync_edge_env_after_activation "100.100.0.4" "edge-formal"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    subprocess.run(["bash", str(runner)], check=True)

    lines = edge_env.read_text(encoding="utf-8").splitlines()
    assert "UNCHANGED=1" in lines
    assert "EDGE_API_BIND_ADDR=100.100.0.4" in lines
    assert "TS_HOSTNAME=edge-formal" in lines
    assert "TS_LOGIN_SERVER=https://headscale.example:8443" in lines
    assert "CLOUD_OPS_BASE_URL=http://100.100.0.1:8088" in lines
    assert "CLOUD_OPS_ENROLL_TOKEN=node-token-1" in lines
    assert "CLOUD_OPS_ENROLL_TOKEN_HEADER=X-Enroll-Token" in lines
    assert sum(line.startswith("CLOUD_OPS_BASE_URL=") for line in lines) == 1
    assert sum(line.startswith("CLOUD_OPS_ENROLL_TOKEN=") for line in lines) == 1


def test_autoinstall_media_carries_and_loads_first_boot_image_bundle():
    user_data = (AUTOINSTALL_DIR / "user-data").read_text(encoding="utf-8")
    post_install = (AUTOINSTALL_DIR / "post-install.sh").read_text(encoding="utf-8")
    installer = INSTALL_SCRIPT.read_text(encoding="utf-8")
    maker = USB_MAKER.read_text(encoding="utf-8")

    assert "/cdrom/bundle-images.tar" in user_data
    assert "/target/opt/drycontroller-images/bundle-images.tar" in user_data
    assert 'IMAGE_BUNDLE="/opt/drycontroller-images/bundle-images.tar"' in post_install
    assert 'INITIAL_RELEASE="/opt/drycontroller-release/software-release-seed.json"' in post_install
    assert "--edge-root" not in post_install
    assert '--source "$SOURCE_ROOT"' in post_install
    assert '--initial-release "$INITIAL_RELEASE"' in post_install
    assert '--image-bundle "$IMAGE_BUNDLE"' in post_install
    assert 'docker image load -i "$INITIAL_IMAGE_BUNDLE"' in installer
    assert 'initialize-active-release "$INITIAL_RELEASE"' in installer
    assert 'rm -f "$IMAGE_BUNDLE" "$INITIAL_RELEASE" "$COSIGN_PUBLIC_KEY" "$COSIGN_BIN"' in post_install
    assert "/cdrom/software-release-seed.json" in user_data
    assert "/cdrom/cosign.pub" in user_data
    assert "/cdrom/cosign-linux-amd64" in user_data
    assert "self.include_images_var = tk.BooleanVar(value=True)" in maker
    assert 'maps.append(("dist/bundle-images.tar", "/bundle-images.tar"))' in maker
    assert '("dist/autoinstall/software-release-seed.json", "/software-release-seed.json")' in maker
    assert '("dist/autoinstall/cosign.pub", "/cosign.pub")' in maker
    assert '("dist/autoinstall/cosign-linux-amd64", "/cosign-linux-amd64")' in maker


def test_autoinstall_media_accepts_single_activation_bundle():
    user_data = (AUTOINSTALL_DIR / "user-data").read_text(encoding="utf-8")
    maker = USB_MAKER.read_text(encoding="utf-8")
    tailnet_service = (Path(__file__).resolve().parents[1] / "drycontroller-tailnet.service").read_text(encoding="utf-8")
    activation_service = (Path(__file__).resolve().parents[1] / "drycontroller-activation.service").read_text(encoding="utf-8")

    assert "/cdrom/activation-bundle.json" in user_data
    assert "/target/etc/drycontroller/activation-bundle.json" in user_data
    assert "/cdrom/tailnet.env" not in user_data
    assert "/cdrom/factory-license.json" not in user_data
    assert "DEFAULT_ACTIVATION_BUNDLE" in maker
    assert "validate_activation_bundle" in maker
    assert "Factory activation materials are not included" not in maker
    assert 'warnings.append("未包含 Factory 材料")' not in maker
    assert 'maps.append(("dist/autoinstall/activation-bundle.json", "/activation-bundle.json"))' in maker
    assert 'maps.append(("dist/autoinstall/tailnet.env", "/tailnet.env"))' not in maker
    assert 'maps.append(("dist/autoinstall/factory-license.json", "/factory-license.json"))' not in maker
    assert "Removed stale split factory material" in maker
    assert "allow_legacy_split_materials" not in maker
    assert "ConditionPathExists=/etc/drycontroller/tailnet.env" not in tailnet_service
    assert "ConditionPathExists=/etc/drycontroller/tailnet.env" not in activation_service


def test_os_installer_usb_maker_verifies_final_iso_release_checklist():
    user_data = (AUTOINSTALL_DIR / "user-data").read_text(encoding="utf-8")
    maker = USB_MAKER.read_text(encoding="utf-8")

    assert "http://127.0.0.1:18080" in user_data
    assert "python3 -m http.server 18080" in user_data
    assert "sources_list:" in user_data
    assert "disable_suites:" in user_data
    assert "mirrors.aliyun.com" not in user_data
    assert "security.ubuntu.com" not in user_data
    assert "verify_final_iso(app, output_iso" in maker
    assert "def verify_final_iso" in maker
    assert "ISO-local HTTP repo" in maker
    assert "external_mirrors" in maker
    assert '"activation-bundle.json"' in maker
    assert '"bundle-images.tar"' in maker
    assert '"tailnet.env", "factory-license.json"' in maker
    assert "validate_activation_bundle_payload(bundle)" in maker
    assert "DryController Edge Autoinstall" in maker
    assert "autoinstall ds=nocloud" in maker
    assert '"linux-generic", "linux-image-generic", "grub-pc", "grub-efi-amd64"' in maker
    assert "Final ISO release checklist passed." in maker


def test_os_installer_release_and_write_wrappers_are_fail_closed():
    release = RELEASE_SCRIPT.read_text(encoding="utf-8")
    writer = USB_WRITE_SCRIPT.read_text(encoding="utf-8")

    assert "git status --porcelain=v1" in release
    assert "git worktree must be clean before building a release ISO" in release
    assert "tools/build-bundle-images.sh" in release
    assert "scripts/prepare-initial-release.py" in release
    assert '--software-release "$SOFTWARE_RELEASE"' in release
    assert '--cosign-public-key "$COSIGN_PUBLIC_KEY"' in release
    assert '--cosign-linux-amd64 "$COSIGN_LINUX_AMD64"' in release
    assert '--source-commit "$(git rev-parse HEAD)"' in release
    assert "build-autoinstall-offline-apt-repo.sh" in release
    assert "build-autoinstall-bundle.sh" in release
    assert '"include_images": True' in release
    assert "INCLUDE_WIFI=false" in release
    assert "--include-wifi" in release
    assert "dist/autoinstall/wifi.env exists; pass --include-wifi" in release
    assert "output ISO must not be the base ISO" in release
    assert "drycontroller.os-installer-release.v1" in release
    assert "final_iso_release_checklist" in release
    assert "bundle-images.tar" in release
    assert "activation-bundle.json" in release
    assert "software-release-seed.json" in release
    assert '"initial_active_release"' in release
    assert '"source_commit": initial_manifest["source_commit"]' in release
    assert '"include_wifi": include_wifi' in release
    assert '"wifi_env"' in release

    assert "manifest schema mismatch" in writer
    assert "ISO sha256 mismatch" in writer
    assert "maker.verify_final_iso" in writer
    assert "diskutil list external physical" in writer
    assert '"diskutil", "list", "-plist", "external", "physical"' in writer
    assert "refusing to write non-external-physical disk" in writer
    assert 'payload.get("VirtualOrPhysical") != "Physical"' in writer
    assert "refusing to write internal disk" in writer
    assert "/dev/r${DISK_ID}" in writer
    assert "ERASE ${DISK_ID}" in writer


def test_host_activation_requires_local_license_when_cloud_omits_payload():
    text = ACTIVATION_SCRIPT.read_text(encoding="utf-8")

    assert "activation response omitted license and no local license is installed" in text
    assert "Path(sys.argv[2]).is_file() and Path(sys.argv[3]).is_file()" in text


def test_host_enroll_exports_payload_environment():
    text = (Path(__file__).resolve().parents[1] / "drycontroller-host-enroll.sh").read_text(encoding="utf-8")

    assert "export ENROLL_NAME ENROLL_EDGE_API_PORT LINE_ID" in text
    assert 'export TAILNET_IP ENROLL_EDGE_API_PORT LINE_ID' in text
    assert '"line_id": os.environ.get("LINE_ID") or None' in text


def test_host_activation_service_retries_pending_ops_approval():
    text = (Path(__file__).resolve().parents[1] / "drycontroller-activation.service").read_text(encoding="utf-8")

    assert "StartLimitIntervalSec=0" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=60" in text


def test_host_activation_persists_server_compatible_factory_fingerprint_before_production_license_install():
    text = ACTIVATION_SCRIPT.read_text(encoding="utf-8")

    assert "FACTORY_LICENSE_ARCHIVE_PATH" in text
    assert "ensure_factory_license_archive" in text
    assert 'json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))' in text
    assert 'h.update(b"\\0")' in text
    assert 'FACTORY_LICENSE_FINGERPRINT="$(license_fingerprint)"' in text
    assert 'state["factory_license_fingerprint"] = fingerprint' in text
    assert text.index("ensure_factory_license_archive") < text.index('install_license_from_response "$response_file"')
    assert text.index('state["factory_license_fingerprint"] = fingerprint') < text.index(
        'install_license_from_response "$response_file"'
    )


def test_host_activation_only_logs_complete_after_tailnet_node_bind():
    text = ACTIVATION_SCRIPT.read_text(encoding="utf-8")

    bind = text.index('bind_current_tailnet_node "$node_hostname"')
    success = text.index('write_state "formal_license_active" "activation_complete"', bind)
    complete = text.index('log "activation complete"', success)
    failed = text.index('write_state "cloud_activated" "tailnet_node_bind_failed"', complete)

    assert bind < success < complete < failed


def test_host_activation_removes_bootstrap_key_only_after_tailnet_bind_confirmed():
    text = ACTIVATION_SCRIPT.read_text(encoding="utf-8")

    sync_start = text.index("sync_tailnet_env_after_activation() {")
    sync_end = text.index("\nremove_bootstrap_tailnet_authkey() {", sync_start)
    assert 'line.startswith("TS_AUTHKEY=")' not in text[sync_start:sync_end]

    bind_start = text.index("bind_current_tailnet_node() {")
    bind_end = text.index('\nresponse_file="$(mktemp)"', bind_start)
    bind_text = text[bind_start:bind_end]
    confirm = bind_text.index('if ! confirm_current_tailnet_binding "$current_ip" "$hostname"; then')
    remove = bind_text.index("remove_bootstrap_tailnet_authkey", confirm)
    done_log = bind_text.index("current tailnet node bound as", remove)

    assert confirm < remove < done_log


def test_host_activation_is_idempotent_after_formal_license_active():
    text = ACTIVATION_SCRIPT.read_text(encoding="utf-8")

    active = text.index('if [ "$current_state" = "formal_license_active" ]; then')
    sync = text.index('sync_edge_env_after_activation "$(tailnet_ip)" "${TS_HOSTNAME:-}"', active)
    mark_complete = text.index("mark_bootstrap_complete", sync)
    start_enroll = text.index("start_host_enroll_service_if_configured", mark_complete)
    exit_ok = text.index("exit 0", start_enroll)
    call_activation = text.index('write_state "bootstrap_tailnet_joined" "calling_cloud_activation"')

    assert active < sync < mark_complete < start_enroll < exit_ok < call_activation


def test_shell_preflight_uses_activated_edge_api_bind_address():
    text = PREFLIGHT_SCRIPT.read_text(encoding="utf-8")

    assert 'EDGE_API_READY_HOST="${PREFLIGHT_EDGE_API_HOST:-${EDGE_API_BIND_ADDR:-127.0.0.1}}"' in text
    assert 'http://${EDGE_API_READY_HOST}:${EDGE_API_PORT}/ready' in text
    assert 'http://127.0.0.1:${EDGE_API_PORT}/ready' not in text
    assert 'allowed_health={"unhealthy","starting"}' in text


def test_edge_api_no_longer_exposes_factory_activation_write_path():
    api_text = EDGE_API_ACTIVATION_API.read_text(encoding="utf-8")

    assert "run_activation" not in api_text
    assert "@router.post" not in api_text
    assert "ActivationTriggerRequest" not in api_text
    assert not LEGACY_EDGE_API_ACTIVATION_CLI.exists()


def test_legacy_tailnet_installer_tree_removed():
    assert not LEGACY_TAILNET_DIR.exists()


def test_install_edge_host_installs_activation_agent_service():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "drycontroller-host-activation.sh" in text
    assert "drycontroller-activation.service" in text
    assert '"activation_service": "drycontroller-activation.service"' in text
    assert "systemctl enable drycontroller-tailnet.service drycontroller-activation.service" in text
    assert "retire_legacy_bootstrap_service" in text
    assert "systemctl disable --now drycontroller-bootstrap.service" in text
    assert "rm -f /etc/systemd/system/drycontroller-bootstrap.service" in text
    assert "rm -f /usr/local/sbin/drycontroller-bootstrap.sh" in text
    assert '"bootstrap_service"' not in text


def test_install_edge_host_installs_root_owned_ota_helper():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert 'EDGE_ROOT="/opt/Drycontroller-edge"' in text
    assert "--edge-root" not in text
    assert "infra/ota-helper/drycontroller_ota_helper.py" in text
    assert "infra/ota-helper/release_engine.py" in text
    assert "infra/ota-helper/ota-apply" in text
    assert "infra/ota-helper/initialize-active-release" in text
    assert "infra/ota-helper/start-active-release" in text
    assert "signed initial SoftwareRelease seed is required" in text
    assert "complete initial release image bundle is required" in text
    assert 'install -o root -g root -m 0644 "$COSIGN_PUBLIC_KEY" /etc/drycontroller/keys/cosign.pub' in text
    assert 'install -o root -g root -m 0755 "$COSIGN_BIN" /usr/local/bin/cosign' in text
    assert "install -o root -g root -m 0755" in text
    assert "systemctl enable --now drycontroller-ota-helper.service" in text
    assert text.index('initialize-active-release "$INITIAL_RELEASE"') < text.index(
        "systemctl enable --now drycontroller-ota-helper.service"
    )
    assert "factory installer refuses an existing release journal" in text
    assert text.index("factory installer refuses an existing release journal") < text.index(
        'log "preparing Drycontroller edge host"'
    )
    assert '"ota_helper_service": "drycontroller-ota-helper.service"' in text


def test_activation_starts_only_the_signed_active_release_without_registry_pull():
    text = ACTIVATION_SCRIPT.read_text(encoding="utf-8")

    assert 'START_ACTIVE_PROGRAM="${DRYCONTROLLER_START_ACTIVE_PROGRAM:-/usr/libexec/drycontroller/start-active-release}"' in text
    assert '"$START_ACTIVE_PROGRAM"' in text
    assert "docker compose up -d" not in text
    assert "reapply_edge_compose_binding || return 1" in text
