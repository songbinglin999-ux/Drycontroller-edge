import pytest

from daq.modbus_client import ModbusClient


@pytest.mark.asyncio
async def test_modbus_circuit_breaker(monkeypatch):
    mc = ModbusClient(host="localhost", port=502)
    mc.cb_fail_threshold = 2
    mc.cb_cooldown = 0.5

    # 触发熔断
    mc._update_failure("fail1")
    mc._update_failure("fail2")
    assert mc.cb_open_until > 0

    called = False

    async def fake_connect():
        nonlocal called
        called = True
        mc.connected = True

    monkeypatch.setattr(mc, "connect", fake_connect)
    ok = await mc.ensure_connected()
    assert ok is False
    assert called is False

    # 冷却结束后可重连
    mc.cb_open_until = 0
    ok = await mc.ensure_connected()
    assert ok is True
    assert called is True

