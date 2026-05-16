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
    tailnet_start = text.index("compose --profile remote-debug up -d tailscale")
    core_start = text.index('log "starting edge core stack..."')
    enroll_start = text.index("compose --profile remote-debug up -d edge-enroll")
    license_bind = text.index('log "binding license on cloud-ops..."')

    # Harbor 走公网，docker login 不依赖 tailnet，前置以早 fail；
    # 但 enroll / core / license bind 必须在 tailnet 就绪之后。
    assert docker_login < tailnet_start < core_start < enroll_start < license_bind


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
