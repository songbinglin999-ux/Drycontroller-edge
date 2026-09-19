import asyncio
import struct
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daq.modbus_proxy import (
    ModbusProxy, WriteJournal, frame, read_frame,
    validate_proxy_runtime,
)


def config(tmp_path, **overrides):
    return {
        "listen_host": "127.0.0.1", "listen_port": 0, "allowed_client_ips": ["127.0.0.1"],
        "response_timeout_sec": 0.15, "write_enabled": True, "source_network_verified": True,
        "write_journal": str(tmp_path / "write.json"),
        "write_rules": [
            {"area": area, "address": address, "min": 0, "max": limit,
             "repeat_safe": True, "evidence": "isolated fake PLC test only"}
            for area, address, limit in [("coil", 30144, 1), ("coil", 30145, 1),
                                         ("holding", 30, 500), ("holding", 31, 500)]
        ], **overrides,
    }


class FakePLC:
    def __init__(self):
        self.requests = []
        self.writers = set()
        self.connections = 0
        self.hold = asyncio.Event()
        self.behavior = "normal"
        self.value = 123

    async def handle(self, reader, writer):
        self.writers.add(writer)
        self.connections += 1
        try:
            while True:
                tid, unit, pdu = await read_frame(reader, 2)
                self.requests.append((tid, unit, pdu))
                if self.behavior == "timeout":
                    await self.hold.wait()
                    return
                if self.behavior == "exception":
                    response = bytes([pdu[0] | 0x80, 2])
                elif self.behavior == "unknown_exception":
                    response = bytes([pdu[0] | 0x80, 4])
                elif pdu[0] in {1, 2}:
                    count = (int.from_bytes(pdu[3:5], "big") + 7) // 8
                    response = bytes([pdu[0], count]) + b'\x01' * count
                elif pdu[0] in {3, 4}:
                    count = int.from_bytes(pdu[3:5], "big")
                    response = bytes([pdu[0], count * 2]) + struct.pack('>H', self.value) * count
                else:
                    response = pdu[:5]
                if self.behavior == "bad_echo":
                    response = response[:-1] + bytes([response[-1] ^ 1])
                writer.write(frame(tid + int(self.behavior == "bad_tid"), unit, response))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)


@asynccontextmanager
async def running(tmp_path, *, takeover=None, **overrides):
    plc = FakePLC()
    server = await asyncio.start_server(plc.handle, "127.0.0.1", 0)
    calls = []
    async def default_takeover(request):
        calls.append(request)
        return {"success": True, "mode": "MANUAL", "generation": len(calls)}
    proxy = ModbusProxy(config(tmp_path, **overrides), plc_host="127.0.0.1",
                        plc_port=server.sockets[0].getsockname()[1], unit=1,
                        takeover=takeover or default_takeover)
    await proxy.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    try:
        yield plc, proxy, reader, writer, calls
    finally:
        writer.close()
        await writer.wait_closed()
        await proxy.stop()
        plc.hold.set()
        for connection in list(plc.writers):
            connection.close()
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0)


async def exchange(reader, writer, pdu, tid=17, unit=1):
    writer.write(frame(tid, unit, pdu))
    await writer.drain()
    response_tid, response_unit, response = await read_frame(reader, 2)
    assert (response_tid, response_unit) == (tid, unit)
    return response


@pytest.mark.parametrize("fc", [1, 2, 3, 4])
async def test_reads_preserve_addresses_and_real_responses(tmp_path, fc):
    async with running(tmp_path) as (plc, proxy, r, w, calls):
        pdu = bytes([fc]) + struct.pack('>HH', 30061, 1)
        expected = bytes([fc, 1, 1]) if fc < 3 else bytes([fc, 2, 0, 123])
        assert await exchange(r, w, pdu) == expected
        plc.value = 456
        await exchange(r, w, pdu, tid=18)
        assert plc.requests == [(17, 1, pdu), (18, 1, pdu)]
        assert plc.connections == 1 and calls == []


@pytest.mark.parametrize("pdu", [
    b'\x05' + struct.pack('>HH', 30144, 0xFF00),
    b'\x05' + struct.pack('>HH', 30144, 0),
    b'\x06' + struct.pack('>HH', 30, 400),
    b'\x0f' + struct.pack('>HHB', 30144, 2, 1) + b'\x01',
    b'\x10' + struct.pack('>HHBHH', 30, 2, 4, 400, 450),
])
async def test_writes_take_manual_then_forward_unmodified(tmp_path, pdu):
    async with running(tmp_path) as (plc, proxy, r, w, calls):
        assert await exchange(r, w, pdu) == pdu[:5]
        assert plc.requests == [(17, 1, pdu)]
        assert len(calls) == 1 and calls[0]['address'] == int.from_bytes(pdu[1:3], 'big')
        assert not proxy.journal.locked and not proxy.journal.path.exists()


async def test_write_range_allows_full_d_area_using_plc_offset(tmp_path):
    async with running(
        tmp_path,
        write_rules=[],
        write_ranges=[{
            "area": "holding", "start_address": 40000, "end_address": 48191,
            "min": 0, "max": 65535, "repeat_safe": True,
            "evidence": "human approved full dedicated HMI D write window 2026-09-19",
        }],
    ) as (plc, proxy, r, w, calls):
        assert await exchange(r, w, b'\x06' + struct.pack('>HH', 40000, 1)) == b'\x06' + struct.pack('>HH', 40000, 1)
        assert await exchange(r, w, b'\x06' + struct.pack('>HH', 41000, 1), tid=18) == b'\x06' + struct.pack('>HH', 41000, 1)
        assert await exchange(r, w, b'\x06' + struct.pack('>HH', 48048, 1), tid=19) == b'\x06' + struct.pack('>HH', 48048, 1)
        assert await exchange(r, w, b'\x06' + struct.pack('>HH', 1000, 1), tid=20) == b'\x86\x02'
        assert await exchange(r, w, b'\x06' + struct.pack('>HH', 39999, 1), tid=21) == b'\x86\x02'
        assert await exchange(r, w, b'\x06' + struct.pack('>HH', 48192, 1), tid=22) == b'\x86\x02'
        assert await exchange(r, w, b'\x10' + struct.pack('>HHBHH', 48190, 2, 4, 1, 1),
                              tid=23) == b'\x10' + struct.pack('>HH', 48190, 2)
        assert await exchange(r, w, b'\x10' + struct.pack('>HHBHH', 48191, 2, 4, 1, 1),
                              tid=24) == b'\x90\x02'
        assert len(plc.requests) == 4
        assert [call["address"] for call in calls] == [40000, 41000, 48048, 48190]


@pytest.mark.parametrize('pdu,code', [
    (b'\x06' + struct.pack('>HH', 32, 2), 2),
    (b'\x06' + struct.pack('>HH', 30, 501), 3),
    (b'\x05' + struct.pack('>HH', 30144, 1), 3),
    (b'\x10' + struct.pack('>HHBHH', 31, 2, 4, 100, 100), 2),
    (b'\x0f' + struct.pack('>HHB', 65535, 2, 1) + b'\x00', 2),
    (b'\x10\x00\x1e\x00\x02\x02\x00\x01', 3),
    (b'\x03\x00\x00\x00\x00', 3),
    (b'\x03\x00\x00\x00\x7e', 3),
    (b'\x17\x00\x00\x00\x01', 1),
])
async def test_invalid_requests_do_not_switch_mode_or_touch_plc(tmp_path, pdu, code):
    async with running(tmp_path) as (plc, proxy, r, w, calls):
        assert await exchange(r, w, pdu) == bytes([pdu[0] | 0x80, code])
        assert not plc.requests and not calls


async def test_readonly_config_and_wrong_unit(tmp_path):
    async with running(tmp_path, write_enabled=False) as (plc, proxy, r, w, calls):
        assert await exchange(r, w, b'\x06\x00\x1e\x00\x01') == b'\x86\x06'
        assert await exchange(r, w, b'\x03\x00\x1e\x00\x01', unit=0) == b'\x83\x0b'
        assert not plc.requests and not calls


async def test_takeover_denial_does_not_forward(tmp_path):
    async def denied(request):
        return {"success": False, "mode": "MANUAL"}
    async with running(tmp_path, takeover=denied) as (plc, proxy, r, w, calls):
        assert await exchange(r, w, b'\x06\x00\x1e\x00\x01') == b'\x86\x06'
        assert not plc.requests and not proxy.journal.locked


@pytest.mark.parametrize('behavior', ['timeout', 'bad_tid', 'bad_echo', 'unknown_exception'])
async def test_unknown_write_is_never_retried_and_survives_restart(tmp_path, behavior):
    async with running(tmp_path) as (plc, proxy, r, w, calls):
        plc.behavior = behavior
        pdu = b'\x06\x00\x1e\x00\x01'
        expected = b'\x86\x04' if behavior == 'unknown_exception' else b'\x86\x0b'
        assert await exchange(r, w, pdu) == expected
        assert proxy.journal.locked and WriteJournal(proxy.journal.path).locked
        assert await exchange(r, w, pdu, tid=18) == b'\x86\x06'
        assert len(plc.requests) == 1
        plc.behavior = 'normal'
        assert await exchange(r, w, b'\x03\x00\x1e\x00\x01', tid=19) == b'\x03\x02\x00\x7b'


async def test_explicit_illegal_address_does_not_lock_writes(tmp_path):
    async with running(tmp_path) as (plc, proxy, r, w, calls):
        plc.behavior = 'exception'
        assert await exchange(r, w, b'\x06\x00\x1e\x00\x01') == b'\x86\x02'
        assert not proxy.journal.locked


async def test_fragmented_and_coalesced_frames(tmp_path):
    async with running(tmp_path) as (plc, proxy, r, w, calls):
        pdu = b'\x03\x00\x1e\x00\x01'
        data = frame(1, 1, pdu) + frame(2, 1, pdu)
        w.write(data[:3])
        await w.drain()
        w.write(data[3:])
        await w.drain()
        assert (await read_frame(r, 2))[0] == 1
        assert (await read_frame(r, 2))[0] == 2
        assert len(plc.requests) == 2


async def test_untrusted_source_is_disconnected(tmp_path):
    async with running(tmp_path, allowed_client_ips=['192.0.2.100']) as (plc, proxy, r, w, calls):
        assert await asyncio.wait_for(r.read(1), 1) == b''
        assert not plc.requests


async def test_invalid_mbap_disconnects_without_upstream(tmp_path):
    async with running(tmp_path) as (plc, proxy, r, w, calls):
        w.write(struct.pack('>HHHB', 1, 1, 6, 1) + b'\x03\x00\x1e\x00\x01')
        await w.drain()
        assert await asyncio.wait_for(r.read(1), 1) == b''
        assert not plc.requests


async def test_disk_failure_prevents_wire_write(tmp_path, monkeypatch):
    async with running(tmp_path) as (plc, proxy, r, w, calls):
        def fail(record):
            raise OSError('disk full')
        monkeypatch.setattr(proxy.journal, 'begin', fail)
        assert await exchange(r, w, b'\x06\x00\x1e\x00\x01') == b'\x86\x0b'
        assert not plc.requests


@pytest.mark.parametrize('extra', [
    {'source_network_verified': False}, {'write_rules': []}, {'allowed_client_ips': []},
    {'write_rules': [], 'write_ranges': [{'area': 'holding', 'start_address': 2, 'end_address': 1,
                                          'min': 0, 'max': 1, 'repeat_safe': True, 'evidence': 'bad'}]},
    {'write_ranges': [{'area': 'holding', 'start_address': 30, 'end_address': 30,
                       'min': 0, 'max': 1, 'repeat_safe': True, 'evidence': 'overlap'}]},
    {'write_rules': [{"area": "holding", "address": 30, "min": 0, "max": 1,
                      "repeat_safe": True, "evidence": "overlap"},
                     {"area": "holding", "address": 30, "min": 0, "max": 1,
                      "repeat_safe": True, "evidence": "overlap"}]},
])
def test_uncommissioned_config_rejected(tmp_path, extra):
    with pytest.raises(ValueError):
        ModbusProxy(config(tmp_path, **extra), plc_host='127.0.0.1', plc_port=1, unit=1, takeover=None)


@pytest.mark.parametrize('config_', [
    {'heartbeat': {'write_address': 5291}},
    {'modbus_address_map': {'SP': {'writable': True}}},
])
def test_proxy_refuses_parallel_physical_writers(config_):
    with pytest.raises(ValueError):
        validate_proxy_runtime(config_, simulator=False, framer='socket')


def test_proxy_refuses_legacy_writer_and_rtu(monkeypatch):
    with pytest.raises(ValueError):
        validate_proxy_runtime({}, simulator=False, framer='rtu')
    monkeypatch.setenv('CONTROL_ENABLE_D5100_WRITER', 'true')
    with pytest.raises(ValueError):
        validate_proxy_runtime({}, simulator=False, framer='socket')


async def test_close_timeout_aborts_transport():
    from daq.modbus_proxy import close_stream
    from unittest.mock import Mock
    writer = Mock()
    async def stuck():
        await asyncio.Event().wait()
    writer.wait_closed = stuck
    await close_stream(writer, 0.01)
    writer.transport.abort.assert_called_once()


async def test_max_clients_is_enforced(tmp_path):
    async with running(tmp_path, max_clients=1) as (plc, proxy, r, w, calls):
        # Force the first connection to be fully admitted.
        await exchange(r, w, b'\x03\x00\x1e\x00\x01')
        extra_r, extra_w = await asyncio.open_connection('127.0.0.1', proxy.port)
        assert await asyncio.wait_for(extra_r.read(1), 1) == b''
        extra_w.close()
        await extra_w.wait_closed()
        assert len(proxy._clients) == 1


async def test_stop_during_inflight_write_keeps_persistent_lock(tmp_path):
    async with running(tmp_path) as (plc, proxy, r, w, calls):
        plc.behavior = 'timeout'
        w.write(frame(1, 1, b'\x06\x00\x1e\x00\x01'))
        await w.drain()
        async def sent():
            while not plc.requests:
                await asyncio.sleep(0.001)
        await asyncio.wait_for(sent(), 1)
        await asyncio.wait_for(proxy.stop(), 1)
        assert not proxy._tasks and not proxy._clients
        assert WriteJournal(proxy.journal.path).locked
