import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.api.v1 import control
from app.core.config import settings
from app.services.control_state import get_control_state_service


@pytest.mark.parametrize('key,licensed,enabled,expected', [
    (None, True, True, 401), ('wrong', True, True, 403),
    ('test-control', False, True, 403), ('test-control', True, False, 409),
    ('test-control', True, True, 200),
])
async def test_takeover_uses_existing_auth_and_license(monkeypatch, key, licensed, enabled, expected):
    monkeypatch.setenv('HMI_PROXY_ENABLED', str(enabled).lower())
    monkeypatch.setattr(settings, 'CONTROL_API_KEYS', ['test-control'])
    monkeypatch.setattr(settings, 'ADMIN_API_KEYS', [])
    def license_gate(capability):
        assert capability == 'production-control'
        if not licensed:
            raise HTTPException(status_code=403, detail='license denied')
    monkeypatch.setattr(control, 'require_license_capability', license_gate)
    state = AsyncMock()
    state.takeover_for_hmi.return_value = {'success': True, 'mode': 'MANUAL', 'generation': 1}
    app = FastAPI()
    app.include_router(control.router, prefix='/control')
    app.dependency_overrides[get_control_state_service] = lambda: state
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://edge') as client:
        response = await client.post('/control/vendor-hmi/takeover',
            headers={'X-API-Key': key} if key else {},
            json={'peer': '192.0.2.100', 'function_code': 6, 'address': 30, 'values': [200]})
    assert response.status_code == expected
    assert state.takeover_for_hmi.await_count == int(expected == 200)


async def test_tcp_proxy_to_authenticated_http_takeover_and_real_plc_reply(tmp_path, monkeypatch):
    import asyncio
    import socket
    import uvicorn
    from unittest.mock import Mock
    from app.services.control_state import ControlStateService
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'edge-daq'))
    from daq.modbus_proxy import ModbusProxy, ManualTakeover, frame, read_frame

    monkeypatch.setenv('HMI_PROXY_ENABLED', 'true')
    monkeypatch.setattr(settings, 'CONTROL_API_KEYS', ['loopback-control'])
    monkeypatch.setattr(settings, 'ADMIN_API_KEYS', [])
    monkeypatch.setattr(control, 'require_license_capability', lambda capability: None)
    state = ControlStateService(realtime_service=Mock(), command_service=Mock())
    state._command.transport_status.return_value = {'ready': True}
    state._audit = AsyncMock()
    state._mode = 'AUTO'
    app = FastAPI()
    app.include_router(control.router, prefix='/api/v1/control')
    app.dependency_overrides[get_control_state_service] = lambda: state
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    api_task = asyncio.create_task(server.serve(sockets=[sock]))
    requests, plc_writers = [], set()
    async def fake_plc(reader, writer):
        plc_writers.add(writer)
        try:
            while True:
                tid, unit, pdu = await read_frame(reader, 2)
                requests.append(pdu)
                assert state._mode == 'MANUAL' or pdu[0] == 3
                response = b'\x03\x02\x00\x7b' if pdu[0] == 3 else pdu
                writer.write(frame(tid, unit, response))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            plc_writers.discard(writer)
    plc = await asyncio.start_server(fake_plc, '127.0.0.1', 0)
    key_file = tmp_path / 'key'
    key_file.write_text('loopback-control')
    proxy = ModbusProxy({
        'listen_host': '127.0.0.1', 'listen_port': 0, 'allowed_client_ips': ['127.0.0.1'],
        'write_enabled': True, 'source_network_verified': True,
        'write_journal': str(tmp_path / 'journal.json'),
        'write_rules': [{'area': 'holding', 'address': 30, 'min': 0, 'max': 500,
                         'repeat_safe': True, 'evidence': 'loopback only'}],
    }, plc_host='127.0.0.1', plc_port=plc.sockets[0].getsockname()[1], unit=1,
        takeover=ManualTakeover(f'http://127.0.0.1:{port}/api/v1/control/vendor-hmi/takeover',
                                str(key_file), 'L1', 2))
    try:
        async def api_ready():
            while not server.started:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(api_ready(), 2)
        await proxy.start()
        reader, writer = await asyncio.open_connection('127.0.0.1', proxy.port)
        try:
            writer.write(frame(1, 1, b'\x03\x00\x1e\x00\x01'))
            await writer.drain()
            assert await read_frame(reader, 2) == (1, 1, b'\x03\x02\x00\x7b')
            assert state._mode == 'AUTO'
            writer.write(frame(2, 1, b'\x06\x00\x1e\x00\x64'))
            await writer.drain()
            assert await read_frame(reader, 2) == (2, 1, b'\x06\x00\x1e\x00\x64')
            assert state._mode == 'MANUAL' and state._requires_reauthorization
            assert len(requests) == 2 and not proxy.journal.locked
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await proxy.stop()
        for writer in list(plc_writers):
            writer.close()
        plc.close()
        await plc.wait_closed()
        server.should_exit = True
        await asyncio.wait_for(api_task, 3)
