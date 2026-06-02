from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "drycontroller-bootstrap.sh"
TAILNET_SCRIPT = Path(__file__).resolve().parents[1] / "drycontroller-host-tailnet.sh"
ACTIVATION_SCRIPT = Path(__file__).resolve().parents[1] / "drycontroller-host-activation.sh"
INSTALL_SCRIPT = Path(__file__).resolve().parents[1] / "install-edge-host.sh"
AUTOINSTALL_DIR = Path(__file__).resolve().parents[2] / "autoinstall"
USB_MAKER = Path(__file__).resolve().parents[3] / "tools" / "os-installer-usb-maker" / "os_installer_usb_maker.py"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_bootstrap_uses_tailnet_runtime_cloud_url_for_post_join_calls():
    text = _script_text()

    assert 'CLOUD_OPS_TAILNET_BASE_URL="$(pyget cloud_ops_tailnet_base_url || true)"' in text
    assert 'CLOUD_OPS_RUNTIME_BASE_URL="${CLOUD_OPS_TAILNET_BASE_URL:-$CLOUD_OPS_BASE_URL}"' in text
    assert 'set_kv "CLOUD_OPS_BASE_URL" "${CLOUD_OPS_RUNTIME_BASE_URL}"' in text
    assert 'curl -fsS "${CLOUD_OPS_RUNTIME_BASE_URL%/}/api/v1/provisioning/license/bind"' in text


def test_bootstrap_starts_tailnet_before_enroll_license_and_core_stack():
    text = _script_text()

    tailnet_start = text.index('log "starting host tailnet before cloud allocation..."')
    allocation = text.index('log "allocating device identity from cloud-ops batch ${BATCH_ID}..."')
    registry_degrade = text.index("registry login to ${HARBOR_HOST} failed; continuing with offline-loaded images")
    host_tailnet = text.index('log "starting host tailnet..."')
    core_start = text.index('log "starting edge core stack..."')
    enroll_start = text.index('log "starting host enroll service..."')
    license_bind = text.index('log "binding license on cloud-ops..."')

    # tailnet URL 模式必须先让宿主机入网，再通过 tailnet allocate。
    # Registry login 在 core stack 前尝试，但离线镜像已加载时不能挡死首次激活。
    assert tailnet_start < allocation < registry_degrade < host_tailnet < core_start < enroll_start < license_bind


def test_bootstrap_uses_host_tailnet_instead_of_compose_tailscale():
    text = _script_text()

    assert "start_host_tailnet" in text
    assert "start_host_enroll" in text
    assert "docker exec edge-tailscale" not in text
    assert "compose --profile remote-debug" not in text


def test_registry_login_failure_degrades_for_offline_activation():
    text = _script_text()

    assert "registry_login || exit 3" not in text
    assert "registry credentials not provided; continuing with offline-loaded images" in text
    assert "registry login to ${HARBOR_HOST} failed; continuing with offline-loaded images" in text


def test_tailnet_mode_rebinds_license_instead_of_using_public_allocation_license():
    text = _script_text()

    assert 'if [ -n "${ALLOCATION_RESP_FILE:-}" ] && [ -z "${CLOUD_OPS_TAILNET_BASE_URL:-}" ]; then' in text
    assert 'log "using signed license from cloud allocation response"' in text
    assert 'log_err "host tailnet did not become ready; bootstrap will not fall back to public cloud url"' in text


def test_tailnet_readiness_probes_cloud_ops_health_not_just_local_ip():
    # tailscale ip -4 拿到 IP 不代表 cloud-ops 经 tailnet 可达（路由收敛 / ECS 端口放行）。
    # 必须在宿主机对 ${CLOUD_OPS_RUNTIME_BASE_URL}/health 做真可达探测。
    text = _script_text()

    assert 'TAILNET_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"' in text
    assert 'curl -fsS --max-time 5 "${health_url%/}/health"' in text


def test_tailnet_mode_binds_edge_api_to_tailnet_ip_not_all_interfaces():
    text = _script_text()

    assert 'set_kv "EDGE_API_BIND_ADDR" "127.0.0.1"' in text
    assert 'set_kv "EDGE_API_BIND_ADDR" "0.0.0.0"' not in text
    assert 'TAILNET_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"' in text
    assert 'set_kv "EDGE_API_BIND_ADDR" "$TAILNET_IP"' in text


def test_background_enroll_uses_node_token_not_license_bind_token():
    text = _script_text()

    assert '"NODE_ENROLL_TOKEN": resp.get("node_enroll_token")' in text
    assert 'NODE_ENROLL_TOKEN="${NODE_ENROLL_TOKEN:-${ENROLL_TOKEN:-}}"' in text
    assert 'set_kv "CLOUD_OPS_ENROLL_TOKEN" "${NODE_ENROLL_TOKEN}"' in text
    assert 'printf \'CLOUD_OPS_ENROLL_TOKEN=%q\\n\' "${NODE_ENROLL_TOKEN:-${ENROLL_TOKEN:-}}"' in text
    assert '-H "X-Enroll-Token: ${ENROLL_TOKEN}"' in text
    assert 'if [ -n "${NODE_ENROLL_TOKEN:-}" ]; then\n  log "starting host enroll service..."' in text


def test_allocation_response_is_cached_for_bootstrap_retry():
    text = _script_text()

    assert 'ALLOCATION_CACHE_FILE="${STATE_DIR}/allocation-response.json"' in text
    assert 'using cached cloud allocation response' in text
    assert 'install -m 0600 "$ALLOCATION_RESP_FILE" "$ALLOCATION_CACHE_FILE"' in text


def test_formal_tailnet_reauth_failure_preserves_bootstrap_tailnet():
    text = _script_text()

    assert "attempt_formal_tailnet_reauth \"$LICENSE_RESP_FILE\"" in text
    assert "formal tailnet re-auth failed; preserving bootstrap tailnet for retry" in text
    assert "systemctl restart drycontroller-tailnet.service || true" in text
    assert "exit 5" not in text[text.index("attempt_formal_tailnet_reauth()"):text.index("REGISTRY_LOGIN_DONE=0")]


def test_host_tailnet_allows_existing_formal_state_without_bootstrap_authkey():
    text = TAILNET_SCRIPT.read_text(encoding="utf-8")

    assert 'existing_ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"' in text
    assert '[ -n "${TS_AUTHKEY:-}" ] || die "TS_AUTHKEY is required when no existing tailnet IPv4 is available"' in text
    assert text.index('existing_ip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"') < text.index(
        '[ -n "${TS_AUTHKEY:-}" ] || die "TS_AUTHKEY is required when no existing tailnet IPv4 is available"'
    )


def test_host_activation_agent_calls_factory_activation_and_never_breaks_tailnet_on_formal_reauth_failure():
    text = ACTIVATION_SCRIPT.read_text(encoding="utf-8")

    assert "/api/v1/factory-activation" in text
    assert '"factory_license": json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))' in text
    assert "factory_license_fingerprint" in text
    assert "formal_ts_authkey" in text
    assert '"pending_ops_approval"' in text
    assert "factory activation is pending ops approval; bootstrap tailnet preserved" in text
    assert 'exit 75' in text
    assert 'tailscale up --reset --authkey="${authkey}"' in text
    assert "formal tailnet re-auth failed; preserving bootstrap tailnet for retry" in text
    assert "systemctl restart drycontroller-tailnet.service || true" in text
    assert "sync_tailnet_env_after_formal_reauth" in text
    assert "sync_edge_env_after_formal_reauth" in text
    assert '"EDGE_API_BIND_ADDR"] = ip' in text
    assert 'EDGE_ENV_FILE="${EDGE_ENV_FILE:-${EDGE_ROOT}/.env}"' in text
    assert "reapply_edge_compose_binding" in text
    assert "confirm_formal_tailnet_joined" in text
    assert "/confirm-formal-tailnet" in text
    assert "start_host_enroll_service_if_configured" in text
    assert 'grep -q \'^CLOUD_OPS_ENROLL_TOKEN=\' "$ENV_FILE"' in text
    assert "systemctl enable drycontroller-enroll.service" in text
    assert "systemctl restart drycontroller-enroll.service" in text
    assert "save_runtime_credentials" in text
    assert 'resp.get("node_enroll_token") or resp.get("enroll_token")' in text
    assert "import shlex" in text
    assert 'updates["CLOUD_OPS_ENROLL_TOKEN"] = node_token' in text
    assert 'updates["TENANT_ID"] = resp["tenant_id"].strip()' in text
    assert 'updates["LINE_ID"] = resp["line_id"].strip()' in text
    assert 'write_state "cloud_activated" "formal_tailnet_reauth_failed_bootstrap_tailnet_preserved"' in text
    assert 'log "production license installed; formal tailnet re-auth failed, bootstrap tailnet preserved"' in text


def test_autoinstall_media_carries_and_loads_first_boot_image_bundle():
    user_data = (AUTOINSTALL_DIR / "user-data").read_text(encoding="utf-8")
    post_install = (AUTOINSTALL_DIR / "post-install.sh").read_text(encoding="utf-8")
    maker = USB_MAKER.read_text(encoding="utf-8")

    assert "/cdrom/bundle-images.tar" in user_data
    assert "/target/opt/drycontroller-images/bundle-images.tar" in user_data
    assert 'IMAGE_BUNDLE="/opt/drycontroller-images/bundle-images.tar"' in post_install
    assert 'docker load -i "$IMAGE_BUNDLE"' in post_install
    assert 'rm -f "$IMAGE_BUNDLE"' in post_install
    assert "self.include_images_var = tk.BooleanVar(value=True)" in maker
    assert 'maps.append(("dist/bundle-images.tar", "/bundle-images.tar"))' in maker


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


def test_host_activation_only_logs_complete_after_formal_tailnet_success():
    text = ACTIVATION_SCRIPT.read_text(encoding="utf-8")

    success = text.index('write_state "formal_license_active" "activation_complete"')
    complete = text.index('log "activation complete"')
    failed = text.index('write_state "cloud_activated" "formal_tailnet_reauth_failed_bootstrap_tailnet_preserved"')
    pending = text.index('write_state "cloud_activated" "production_license_installed_formal_tailnet_pending"')

    assert success < complete < failed < pending


def test_bootstrap_invokes_host_activation_before_legacy_license_bind_when_enabled():
    text = _script_text()

    assert 'FACTORY_ACTIVATION_ENABLED="$(pyget factory_activation_enabled || true)"' in text
    assert "should_run_host_activation()" in text
    assert "start_host_activation()" in text
    assert text.index('log "starting host activation service..."') < text.index('log "binding license on cloud-ops..."')
    assert 'if [ "$HOST_ACTIVATION_DONE" != "1" ]; then' in text


def test_bootstrap_reapplies_edge_binding_after_factory_activation():
    text = _script_text()

    assert "sync_formal_tailnet_binding()" in text
    assert "reload_tailnet_env()" in text
    assert "unset TS_AUTHKEY" in text
    assert 'if [ -n "${TS_AUTHKEY:-}" ]; then' in text
    assert 'printf \'EDGE_ENV_FILE=%q\\n\' "${ENV_FILE:-}"' in text
    assert 'printf \'EDGE_ROOT=%q\\n\' "${EDGE_ROOT:-}"' in text
    assert 'set_kv "EDGE_API_BIND_ADDR" "$current_ip"' in text
    assert 'tailscale up --reset --authkey="${formal_authkey}"' in text
    assert 'log "re-applying edge core stack after activation tailnet sync..."' in text
    assert text.index("reload_tailnet_env") < text.index("sync_formal_tailnet_binding")
    assert text.index("sync_formal_tailnet_binding") < text.index('log "re-applying edge core stack after activation tailnet sync..."')


def test_factory_activation_mode_does_not_require_legacy_identity_or_enroll_token():
    text = _script_text()

    assert 'elif [ -z "${ENROLL_TOKEN:-}" ]; then' in text
    assert 'if ! should_run_host_activation && { [ -z "${DEVICE_ID:-}" ] || [ -z "${NODE_NAME:-}" ]; }; then' in text
    assert text.index('log "factory activation mode enabled; device identity and production license will come from cloud activation"') < text.index(
        'log "missing enroll_token in provisioning.json or cloud allocation response"'
    )


def test_factory_activation_preserves_device_json_written_by_activation_agent():
    text = _script_text()

    assert 'if [ -f "${STATE_DIR}/device.json" ]; then' in text
    assert 'python3 - "${STATE_DIR}/device.json"' in text
    assert 'if [ "$HOST_ACTIVATION_DONE" != "1" ]; then\n  DEVICE_JSON="${STATE_DIR}/device.json"' in text


def test_factory_activation_starts_host_enroll_if_activation_installs_node_token():
    text = _script_text()

    assert '. "$TAILNET_ENV_FILE"' in text
    assert 'NODE_ENROLL_TOKEN="${CLOUD_OPS_ENROLL_TOKEN:-${NODE_ENROLL_TOKEN:-}}"' in text
    assert 'log "starting host enroll service after activation..."' in text
    assert 'log_warn "node enroll token still missing after activation; host enroll service remains stopped"' in text


def test_install_edge_host_installs_activation_agent_service():
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert "drycontroller-host-activation.sh" in text
    assert "drycontroller-activation.service" in text
    assert '"activation_service": "drycontroller-activation.service"' in text
    assert "systemctl enable drycontroller-tailnet.service drycontroller-activation.service" in text
    assert "ENABLE_LEGACY_BOOTSTRAP=false" in text
    assert "legacy drycontroller-bootstrap.service" in text
