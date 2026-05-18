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

    docker_login = text.index('log "docker login to ${HARBOR_HOST} ..."')
    tailnet_start = text.index('log "starting tailnet before cloud allocation..."')
    allocation = text.index('log "allocating device identity from cloud-ops batch ${BATCH_ID}..."')
    core_start = text.index('log "starting edge core stack..."')
    enroll_start = text.index("compose --profile remote-debug up -d edge-enroll")
    license_bind = text.index('log "binding license on cloud-ops..."')

    # tailnet URL 模式必须先入网，再通过 tailnet allocate；Harbor login 仍在 core stack 前 fail-fast。
    assert tailnet_start < allocation < docker_login < core_start < enroll_start < license_bind


def test_tailnet_mode_rebinds_license_instead_of_using_public_allocation_license():
    text = _script_text()

    assert 'if [ -n "${ALLOCATION_RESP_FILE:-}" ] && [ -z "${CLOUD_OPS_TAILNET_BASE_URL:-}" ]; then' in text
    assert 'log "using signed license from cloud allocation response"' in text
    assert 'log "tailnet did not become ready; bootstrap will not fall back to public cloud url"' in text


def test_tailnet_readiness_probes_cloud_ops_health_not_just_local_ip():
    # tailscale ip -4 拿到 IP 不代表 cloud-ops 经 tailnet 可达（路由收敛 / ECS 端口放行）。
    # 必须在容器内对 ${CLOUD_OPS_RUNTIME_BASE_URL}/health 做真可达探测。
    text = _script_text()

    assert 'docker exec edge-tailscale tailscale ip -4' in text
    assert 'docker exec edge-tailscale wget -q -T 5 -O /dev/null "${CLOUD_OPS_RUNTIME_BASE_URL%/}/health"' in text


def test_background_enroll_uses_node_token_not_license_bind_token():
    text = _script_text()

    assert '"NODE_ENROLL_TOKEN": resp.get("node_enroll_token")' in text
    assert 'NODE_ENROLL_TOKEN="${NODE_ENROLL_TOKEN:-$ENROLL_TOKEN}"' in text
    assert 'set_kv "CLOUD_OPS_ENROLL_TOKEN" "${NODE_ENROLL_TOKEN}"' in text
    assert '-H "X-Enroll-Token: ${ENROLL_TOKEN}"' in text


def test_allocation_response_is_cached_for_bootstrap_retry():
    text = _script_text()

    assert 'ALLOCATION_CACHE_FILE="${STATE_DIR}/allocation-response.json"' in text
    assert 'using cached cloud allocation response' in text
    assert 'install -m 0600 "$ALLOCATION_RESP_FILE" "$ALLOCATION_CACHE_FILE"' in text
