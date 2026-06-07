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
