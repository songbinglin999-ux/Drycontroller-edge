from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "drycontroller-bootstrap.sh"


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
    assert 'NODE_ENROLL_TOKEN="${NODE_ENROLL_TOKEN:-$ENROLL_TOKEN}"' in text
    assert 'set_kv "CLOUD_OPS_ENROLL_TOKEN" "${NODE_ENROLL_TOKEN}"' in text
    assert 'printf \'CLOUD_OPS_ENROLL_TOKEN=%q\\n\' "${NODE_ENROLL_TOKEN:-${ENROLL_TOKEN:-}}"' in text
    assert '-H "X-Enroll-Token: ${ENROLL_TOKEN}"' in text


def test_allocation_response_is_cached_for_bootstrap_retry():
    text = _script_text()

    assert 'ALLOCATION_CACHE_FILE="${STATE_DIR}/allocation-response.json"' in text
    assert 'using cached cloud allocation response' in text
    assert 'install -m 0600 "$ALLOCATION_RESP_FILE" "$ALLOCATION_CACHE_FILE"' in text
