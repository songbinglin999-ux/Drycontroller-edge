import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)


def test_production_rejects_default_secrets():
    from app.core.config import Settings

    settings = Settings(
        ENV="production",
        POSTGRES_PASSWORD="edge_password",
        MQTT_PASSWORD="mqtt_password",
    )

    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD, MQTT_PASSWORD"):
        settings.validate_startup_security()


def test_secret_defaults_are_empty(monkeypatch):
    from app.core.config import Settings

    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("MQTT_PASSWORD", raising=False)

    settings = Settings(_env_file=None)

    assert settings.POSTGRES_PASSWORD == ""
    assert settings.MQTT_PASSWORD == ""


def test_production_rejects_placeholder_secrets():
    from app.core.config import Settings

    settings = Settings(
        ENV="production",
        POSTGRES_PASSWORD="change_me_postgres_password",
        MQTT_PASSWORD="change_me_mqtt_edge_api",
    )

    with pytest.raises(RuntimeError, match="POSTGRES_PASSWORD, MQTT_PASSWORD"):
        settings.validate_startup_security()


def test_development_allows_default_secrets():
    from app.core.config import Settings

    settings = Settings(
        ENV="development",
        POSTGRES_PASSWORD="edge_password",
        MQTT_PASSWORD="mqtt_password",
    )

    settings.validate_startup_security()


@pytest.mark.asyncio
async def test_lifespan_calls_startup_security_validation(monkeypatch):
    from app import main

    called = False

    def _fail_validation():
        nonlocal called
        called = True
        raise RuntimeError("startup security failed")

    monkeypatch.setattr(main.settings, "validate_startup_security", _fail_validation)

    with pytest.raises(RuntimeError, match="startup security failed"):
        async with main.lifespan(main.app):
            pass

    assert called
