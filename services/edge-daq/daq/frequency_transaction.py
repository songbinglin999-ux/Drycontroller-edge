"""D5100 single-frequency transaction writer.

The writer talks to the final PLC request block directly:
payload registers first, D5100 sequence last, then D5205/D5206/D5207 and
D5210-D5215 decide whether the write is accepted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

D_MODBUS_BASE = 40000

EDGE_REQ_SEQ = 5100
EDGE_REQ_TYPE = 5101
EDGE_REQ_MODE = 5102
EDGE_REQ_TIMEOUT_S = 5103
EDGE_FREQ_CHANNEL = 5104
EDGE_FREQ_VALUE = 5110
EDGE_REMOTE_RUN_START = 5130
EDGE_REQ_CHECK = 5190

PLC_LAST_SEEN_SEQ = 5204
PLC_LAST_ACK_SEQ = 5205
PLC_LAST_NAK_SEQ = 5206
PLC_NAK_CODE = 5207
PLC_ACTIVE_FREQ_TARGET_START = 5210
EDGE_HOST_ALIVE = 5291

REQUEST_TYPE_SINGLE_FREQUENCY = 1
REQUEST_MODE_MANUAL = 1
REQUEST_MODE_AUTO = 2

MAX_SEQUENCE = 32760


@dataclass(frozen=True)
class FrequencyTransactionResult:
    status: str
    seq: int
    channel: int = 0
    value: int = 0
    ack_seq: int = 0
    nak_seq: int = 0
    nak_code: int = 0
    active_targets: list[int] = field(default_factory=list)
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success"


class SingleFrequencyTransactionWriter:
    """Write one frequency target through the final D5100 PLC transaction."""

    def __init__(
        self,
        modbus_client,
        *,
        d_modbus_base: int = D_MODBUS_BASE,
        poll_interval_sec: float = 0.1,
    ):
        self.modbus_client = modbus_client
        self.d_modbus_base = int(d_modbus_base)
        self.poll_interval_sec = float(poll_interval_sec)
        self._last_seq = 0
        self._edge_alive_value = 0

    def _addr(self, d_number: int) -> int:
        return self.d_modbus_base + d_number

    async def refresh_edge_host_alive(self, value: int | None = None) -> bool:
        """Increment/write D5291 for standalone L2 tests or explicit refresh."""
        if value is None:
            self._edge_alive_value = 1 if self._edge_alive_value >= 65535 else self._edge_alive_value + 1
            value = self._edge_alive_value
        else:
            value = self._u16(value)
            self._edge_alive_value = value
        return await self.modbus_client.write_register(self._addr(EDGE_HOST_ALIVE), value)

    async def write_one(
        self,
        channel: Any,
        value: Any,
        *,
        mode: int = REQUEST_MODE_AUTO,
        seq: int | None = None,
        timeout_sec: float = 3.0,
        request_timeout_s: int = 10,
        flags: int = 0,
        checksum: bool = True,
        refresh_heartbeat: bool = False,
    ) -> FrequencyTransactionResult:
        try:
            request_channel = self._coerce_channel(channel)
            request_value = self._coerce_value(value)
            request_mode = self._coerce_mode(mode)
            request_timeout = self._u16(request_timeout_s)
        except ValueError as exc:
            return FrequencyTransactionResult(
                status="failed",
                seq=0,
                reason="invalid_request",
                detail={"error": str(exc)},
            )

        if refresh_heartbeat and not await self.refresh_edge_host_alive():
            return FrequencyTransactionResult(status="failed", seq=0, channel=request_channel, value=request_value, reason="heartbeat_write_failed")

        try:
            chosen_seq = self._coerce_sequence(seq) if seq is not None else await self._next_sequence()
        except ValueError as exc:
            return FrequencyTransactionResult(
                status="failed",
                seq=0,
                channel=request_channel,
                value=request_value,
                reason="invalid_sequence",
                detail={"error": str(exc)},
            )
        if chosen_seq == 0:
            return FrequencyTransactionResult(status="failed", seq=0, channel=request_channel, value=request_value, reason="invalid_sequence")

        status_before = await self._read_ack_status()
        if status_before and chosen_seq in (status_before.get("ack_seq"), status_before.get("nak_seq")):
            return FrequencyTransactionResult(
                status="failed",
                seq=chosen_seq,
                channel=request_channel,
                value=request_value,
                ack_seq=int(status_before.get("ack_seq") or 0),
                nak_seq=int(status_before.get("nak_seq") or 0),
                nak_code=int(status_before.get("nak_code") or 0),
                reason="duplicate_sequence",
            )

        payload_head = [
            REQUEST_TYPE_SINGLE_FREQUENCY,
            request_mode,
            request_timeout,
            request_channel,
        ]
        remote_run = [0, 0]
        check_value = 0
        if checksum:
            check_value = sum([*payload_head, request_value, *remote_run]) & 0xFFFF

        for writer, address, payload in (
            (self.modbus_client.write_registers, self._addr(EDGE_REQ_TYPE), payload_head),
            (self.modbus_client.write_register, self._addr(EDGE_FREQ_VALUE), request_value),
            (self.modbus_client.write_registers, self._addr(EDGE_REMOTE_RUN_START), remote_run),
            (self.modbus_client.write_register, self._addr(EDGE_REQ_CHECK), check_value),
            (self.modbus_client.write_register, self._addr(EDGE_REQ_SEQ), chosen_seq),
        ):
            if not await writer(address, payload):
                return FrequencyTransactionResult(
                    status="failed",
                    seq=chosen_seq,
                    channel=request_channel,
                    value=request_value,
                    reason="plc_write_failed",
                )

        self._last_seq = chosen_seq
        return await self._wait_for_result(chosen_seq, request_channel, request_value, timeout_sec)

    async def read_active_targets(self) -> list[int] | None:
        registers = await self.modbus_client.read_registers(
            self._addr(PLC_ACTIVE_FREQ_TARGET_START),
            6,
            function_code=3,
        )
        if registers is None or len(registers) != 6:
            return None
        return [self._u16(value) for value in registers]

    async def _wait_for_result(
        self,
        seq: int,
        channel: int,
        value: int,
        timeout_sec: float,
    ) -> FrequencyTransactionResult:
        deadline = asyncio.get_running_loop().time() + float(timeout_sec)
        last_status: dict[str, int] | None = None

        while asyncio.get_running_loop().time() <= deadline:
            status = await self._read_ack_status()
            if status:
                last_status = status
                ack_seq = status["ack_seq"]
                nak_seq = status["nak_seq"]
                nak_code = status["nak_code"]
                if ack_seq == seq:
                    active_targets = await self.read_active_targets()
                    if active_targets is None:
                        return FrequencyTransactionResult(
                            status="failed",
                            seq=seq,
                            channel=channel,
                            value=value,
                            ack_seq=ack_seq,
                            nak_seq=nak_seq,
                            nak_code=nak_code,
                            reason="readback_failed",
                        )
                    if active_targets[channel - 1] != value:
                        return FrequencyTransactionResult(
                            status="failed",
                            seq=seq,
                            channel=channel,
                            value=value,
                            ack_seq=ack_seq,
                            nak_seq=nak_seq,
                            nak_code=nak_code,
                            active_targets=active_targets,
                            reason="readback_mismatch",
                            detail={"requested_channel": channel, "requested_value": value},
                        )
                    return FrequencyTransactionResult(
                        status="success",
                        seq=seq,
                        channel=channel,
                        value=value,
                        ack_seq=ack_seq,
                        nak_seq=nak_seq,
                        nak_code=nak_code,
                        active_targets=active_targets,
                    )
                if nak_seq == seq:
                    active_targets = await self.read_active_targets()
                    return FrequencyTransactionResult(
                        status="nak",
                        seq=seq,
                        channel=channel,
                        value=value,
                        ack_seq=ack_seq,
                        nak_seq=nak_seq,
                        nak_code=nak_code,
                        active_targets=active_targets or [],
                        reason=f"plc_nak_{nak_code}",
                    )

            await asyncio.sleep(self.poll_interval_sec)

        detail = last_status or {}
        logger.warning("single_frequency_transaction_timeout", seq=seq, channel=channel, value=value, detail=detail)
        return FrequencyTransactionResult(
            status="timeout",
            seq=seq,
            channel=channel,
            value=value,
            reason="ack_timeout",
            detail=detail,
        )

    async def _read_ack_status(self) -> dict[str, int] | None:
        registers = await self.modbus_client.read_registers(
            self._addr(PLC_LAST_ACK_SEQ),
            3,
            function_code=3,
        )
        if registers is None or len(registers) != 3:
            return None
        return {
            "ack_seq": self._u16(registers[0]),
            "nak_seq": self._u16(registers[1]),
            "nak_code": self._u16(registers[2]),
        }

    async def _next_sequence(self) -> int:
        registers = await self.modbus_client.read_registers(
            self._addr(PLC_LAST_SEEN_SEQ),
            4,
            function_code=3,
        )
        if registers and len(registers) == 4:
            high_water = max(self._u16(value) for value in registers[:3])
            self._last_seq = max(self._last_seq, high_water)
        self._last_seq = 1 if self._last_seq >= MAX_SEQUENCE else self._last_seq + 1
        return self._last_seq

    @staticmethod
    def _coerce_mode(mode: Any) -> int:
        value = _coerce_int_field(mode, "mode")
        if value not in (REQUEST_MODE_MANUAL, REQUEST_MODE_AUTO):
            raise ValueError("mode must be 1 manual or 2 auto")
        return value

    @staticmethod
    def _coerce_channel(channel: Any) -> int:
        value = _coerce_int_field(channel, "channel")
        if value < 1 or value > 6:
            raise ValueError("channel must be 1..6")
        return value

    @classmethod
    def _coerce_value(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("frequency target must be a finite number")
        if float(value) < 0 or float(value) > 65535:
            raise ValueError("frequency target must fit an unsigned 16-bit register")
        if not float(value).is_integer():
            raise ValueError("frequency target must be an integer Hz value")
        return cls._u16(value)

    @staticmethod
    def _coerce_sequence(seq: Any) -> int:
        value = _coerce_int_field(seq, "sequence")
        if value < 1 or value > MAX_SEQUENCE:
            raise ValueError("sequence must be 1..32760")
        return value

    @staticmethod
    def _u16(value: Any) -> int:
        return int(value) & 0xFFFF


def _coerce_int_field(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized or not normalized.isdecimal():
            raise ValueError(f"{field_name} must be an integer")
        return int(normalized)
    raise ValueError(f"{field_name} must be an integer")
