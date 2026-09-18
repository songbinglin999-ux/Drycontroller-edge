"""Address-preserving Modbus TCP gateway. Disabled until explicitly configured.

No speculative cache, address translation, write retry or implicit write grants.
An acknowledged PLC write is not proof of physical equipment operation.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import struct
import time
from pathlib import Path

import aiohttp
import structlog
import yaml

logger = structlog.get_logger(__name__)
READ_FUNCTIONS = {1, 2, 3, 4}
WRITE_FUNCTIONS = {5, 6, 15, 16}


class ProtocolError(ValueError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"Modbus exception {code}")


def parse_request(pdu: bytes) -> tuple[int, int, list[int]]:
    """Validate the whole PDU before either authorization or network side effects."""
    fc = pdu[0]
    if fc not in READ_FUNCTIONS | WRITE_FUNCTIONS:
        raise ProtocolError(1)
    if len(pdu) < 5:
        raise ProtocolError(3)
    address, count = struct.unpack(">HH", pdu[1:5])
    if fc in READ_FUNCTIONS:
        if len(pdu) != 5 or not 1 <= count <= (2000 if fc in {1, 2} else 125):
            raise ProtocolError(3)
        values = []
    elif fc in {5, 6}:
        if len(pdu) != 5 or (fc == 5 and count not in {0, 0xFF00}):
            raise ProtocolError(3)
        values = [int(count == 0xFF00)] if fc == 5 else [count]
        count = 1
    else:
        limit = 1968 if fc == 15 else 123
        expected = (count + 7) // 8 if fc == 15 else count * 2
        if len(pdu) < 6 or not 1 <= count <= limit or pdu[5] != expected or len(pdu) != 6 + expected:
            raise ProtocolError(3)
        values = ([int(bool(pdu[6 + i // 8] & (1 << (i % 8)))) for i in range(count)]
                  if fc == 15 else list(struct.unpack(f">{count}H", pdu[6:])))
    if address + count > 65536:
        raise ProtocolError(2)
    return address, count, values


async def read_frame(reader: asyncio.StreamReader, timeout: float) -> tuple[int, int, bytes]:
    async def read():
        tid, protocol, length, unit = struct.unpack(">HHHB", await reader.readexactly(7))
        if protocol != 0 or not 2 <= length <= 254:
            raise ValueError("invalid MBAP header")
        return tid, unit, await reader.readexactly(length - 1)
    return await asyncio.wait_for(read(), timeout)


def frame(tid: int, unit: int, pdu: bytes) -> bytes:
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


def validate_response(request: bytes, response: bytes) -> None:
    fc = request[0]
    if len(response) == 2 and response[0] == fc | 0x80 and 1 <= response[1] <= 11:
        return
    if not response or response[0] != fc:
        raise ValueError("PLC function mismatch")
    if fc in WRITE_FUNCTIONS:
        if response != request[:5]:
            raise ValueError("PLC write echo mismatch")
    else:
        _, count, _ = parse_request(request)
        expected = (count + 7) // 8 if fc in {1, 2} else count * 2
        if len(response) != expected + 2 or response[1] != expected:
            raise ValueError("PLC read length mismatch")


class WriteJournal:
    """A crash/unknown-result interlock, not an end-to-end idempotency key."""
    def __init__(self, path: Path):
        self.path = path
        self.locked = path.exists()

    def _sync_parent(self):
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def begin(self, record: dict):
        if self.locked or self.path.exists():
            self.locked = True
            raise OSError("previous write outcome unresolved")
        self.locked = True
        # O_EXCL also stops accidental multiple workers sharing this journal.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        self._sync_parent()

    def finish(self):
        self.path.unlink()
        self._sync_parent()
        self.locked = False


class ManualTakeover:
    """DAQ authenticates to the existing Edge control/License boundary."""
    def __init__(self, url: str, key_file: str, line: str, timeout: float):
        self.url, self.key_file, self.line, self.timeout = url, key_file, line, timeout

    async def __call__(self, request: dict) -> dict:
        key = Path(self.key_file).read_text().strip()
        if not key:
            raise ValueError("missing proxy control credential")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
            async with session.post(
                self.url, json={"line": self.line, **request},
                headers={os.getenv("API_KEY_HEADER", "X-API-Key"): key}, allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ValueError("manual takeover denied")
                result = await response.json()
        if result.get("success") is not True or result.get("mode") != "MANUAL" or not isinstance(result.get("generation"), int):
            raise ValueError("manual takeover not confirmed")
        return result


async def close_stream(writer, timeout: float):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout)
    except (OSError, asyncio.TimeoutError):
        writer.transport.abort()
    except asyncio.CancelledError:
        writer.transport.abort()
        raise


class ModbusProxy:
    def __init__(self, config: dict, *, plc_host: str, plc_port: int, unit: int,
                 takeover, write_permitted=lambda: True):
        self.host = str(config.get("listen_host", "127.0.0.1"))
        self.port = int(config.get("listen_port", 2000))
        self.allowed_peers = {str(ipaddress.ip_address(ip)) for ip in config.get("allowed_client_ips", [])}
        if not self.allowed_peers or not 1 <= unit <= 247 or not 0 <= self.port <= 65535:
            raise ValueError("proxy requires explicit clients, port and non-broadcast unit")
        self.plc_host, self.plc_port, self.unit = plc_host, plc_port, unit
        self.timeout = float(config.get("response_timeout_sec", 2.0))
        self.idle_timeout = float(config.get("client_idle_timeout_sec", 30.0))
        self.max_clients = int(config.get("max_clients", 2))
        if not 0.1 <= self.timeout <= 10 or not 1 <= self.idle_timeout <= 120 or not 1 <= self.max_clients <= 8:
            raise ValueError("invalid proxy resource limits")
        self.write_enabled = config.get("write_enabled") is True
        self.rules = {}
        for rule in config.get("write_rules", []):
            area, address = rule["area"], rule["address"]
            lo, hi = rule["min"], rule["max"]
            if (area not in {"coil", "holding"} or type(address) is not int or not 0 <= address <= 65535
                    or type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi <= (1 if area == "coil" else 65535)
                    or rule.get("repeat_safe") is not True or not rule.get("evidence")
                    or (area, address) in self.rules):
                raise ValueError("write rule requires unique raw address, range and repeat-safe evidence")
            self.rules[area, address] = (lo, hi)
        if self.write_enabled and (config.get("source_network_verified") is not True or not self.rules):
            raise ValueError("writes require commissioned rules and verified source network")
        self.journal = WriteJournal(Path(config.get("write_journal", "/data/edge-daq/hmi-proxy-write.json")))
        self.takeover, self.write_permitted = takeover, write_permitted
        self.server = None
        self._stopping = False
        self._reader = self._writer = None
        self._lock = asyncio.Lock()
        self._clients = set()
        self._tasks = set()
        self.last_error = None

    async def start(self):
        self._stopping = False
        if self.write_enabled:
            # Require an existing durable directory; do not silently use an ephemeral fallback.
            if not self.journal.path.parent.is_dir():
                raise ValueError("proxy write journal directory missing")
        self.server = await asyncio.start_server(self._handle_client, self.host, self.port, limit=1024)
        self.port = self.server.sockets[0].getsockname()[1]
        logger.info("modbus_proxy_started", host=self.host, port=self.port, writes=self.write_enabled)

    async def stop(self):
        self._stopping = True
        server, self.server = self.server, None
        if server:
            server.close()
        # Python 3.12+ Server.wait_closed waits for active clients as well.
        # Close/cancel those before waiting for the listener to finish.
        for client in list(self._clients):
            client.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        await self._disconnect()
        if server:
            await asyncio.wait_for(server.wait_closed(), self.timeout)

    def get_status(self):
        return {"ready": self.server is not None, "mode": "modbus_tcp_proxy",
                "legacy_control_enabled": False, "auto_available": False,
                "write_enabled": self.write_enabled, "write_outcome_unknown": self.journal.locked,
                "clients": len(self._clients), "last_error": self.last_error}

    async def _disconnect(self):
        writer, self._writer = self._writer, None
        self._reader = None
        if writer:
            await close_stream(writer, self.timeout)

    def _authorize_write(self, fc, address, values):
        if not self.write_enabled or not self.write_permitted() or self.journal.locked:
            raise ProtocolError(6)
        area = "coil" if fc in {5, 15} else "holding"
        for offset, value in enumerate(values):
            bounds = self.rules.get((area, address + offset))
            if bounds is None:
                raise ProtocolError(2)
            if not bounds[0] <= value <= bounds[1]:
                raise ProtocolError(3)

    async def _exchange(self, tid: int, pdu: bytes, peer: str) -> bytes:
        address, count, values = parse_request(pdu)
        writing = pdu[0] in WRITE_FUNCTIONS
        if writing:
            self._authorize_write(pdu[0], address, values)
        try:
            await asyncio.wait_for(self._lock.acquire(), self.timeout)
        except asyncio.TimeoutError:
            raise ProtocolError(6)
        try:
            if writing:
                self._authorize_write(pdu[0], address, values)
                try:
                    takeover = await asyncio.wait_for(self.takeover({
                        "peer": peer, "function_code": pdu[0], "address": address,
                        "values": values,
                    }), self.timeout)
                except (OSError, ValueError, aiohttp.ClientError, asyncio.TimeoutError):
                    raise ProtocolError(6)
                if takeover.get("success") is not True or takeover.get("mode") != "MANUAL":
                    raise ProtocolError(6)
            if self._writer is None:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.plc_host, self.plc_port), self.timeout)
            if writing:
                self._authorize_write(pdu[0], address, values)
                self.journal.begin({"ts": time.time(), "peer": peer, "tid": tid,
                                    "unit": self.unit, "pdu": pdu.hex(), "takeover": takeover})
            self._writer.write(frame(tid, self.unit, pdu))
            await asyncio.wait_for(self._writer.drain(), self.timeout)
            response_tid, response_unit, response = await read_frame(self._reader, self.timeout)
            if response_tid != tid or response_unit != self.unit:
                raise ValueError("PLC transaction/unit mismatch")
            validate_response(pdu, response)
            if writing:
                # Only normal acknowledgement or explicit pre-execution rejection resolves the write.
                if response[0] == pdu[0] or response[1] in {1, 2, 3}:
                    self.journal.finish()
                logger.info("modbus_proxy_write_result", peer=peer, address=address, count=count,
                            function=pdu[0], values=values, response=response.hex(),
                            generation=takeover.get("generation"), outcome_unknown=self.journal.locked)
            self.last_error = None
            return response
        except ProtocolError:
            raise
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
            self.last_error = type(exc).__name__
            logger.warning("modbus_proxy_exchange_failed", error=self.last_error,
                           writing=writing, outcome_unknown=self.journal.locked)
            await self._disconnect()
            raise ProtocolError(11)
        except asyncio.CancelledError:
            await self._disconnect()
            raise
        finally:
            self._lock.release()

    async def _handle_client(self, reader, writer):
        peer = writer.get_extra_info("peername")[0]
        task = asyncio.current_task()
        if self._stopping or peer not in self.allowed_peers or len(self._clients) >= self.max_clients:
            await close_stream(writer, self.timeout)
            return
        self._clients.add(writer)
        self._tasks.add(task)
        try:
            while True:
                tid, unit, pdu = await read_frame(reader, self.idle_timeout)
                try:
                    if unit != self.unit:
                        raise ProtocolError(11)
                    response = await self._exchange(tid, pdu, peer)
                except ProtocolError as exc:
                    response = bytes([pdu[0] | 0x80, exc.code])
                writer.write(frame(tid, unit, response))
                await asyncio.wait_for(writer.drain(), self.timeout)
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            try:
                await close_stream(writer, self.timeout)
            finally:
                self._clients.discard(writer)
                self._tasks.discard(task)


def load_proxy_config() -> dict | None:
    if os.getenv("HMI_PROXY_ENABLED", "false").lower() != "true":
        return None
    path = Path(os.getenv("HMI_PROXY_CONFIG", "/app/configs/modbus-proxy.yaml"))
    config = yaml.safe_load(path.read_text())
    if not isinstance(config, dict):
        raise ValueError("invalid Modbus proxy configuration")
    return config


def validate_proxy_runtime(config: dict, *, simulator: bool, framer: str):
    if simulator or framer.lower() not in {"socket", "tcp"}:
        raise ValueError("HMI proxy requires a real TCP target (loopback fake PLC is allowed)")
    if os.getenv("CONTROL_ENABLE_D5100_WRITER", "false").lower() == "true":
        raise ValueError("HMI proxy cannot coexist with D5100 writer")
    if config.get("heartbeat", {}).get("write_address") is not None:
        raise ValueError("HMI proxy cannot coexist with periodic PLC writes")
    if any(item.get("writable") for item in config.get("modbus_address_map", {}).values() if isinstance(item, dict)):
        raise ValueError("HMI proxy cannot coexist with legacy writable address bindings")
