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
AUTHORITY_OWNER = 5201

AUTH_REQ_SEQ = 5400
AUTH_REQ_SOURCE = 5401
AUTH_REQ_ACTION = 5402
AUTH_REQ_EPOCH = 5403
AUTH_REQ_TIMEOUT_S = 5404
AUTH_LAST_SEEN_SEQ = 5410
AUTH_GRANT_SEQ = 5411
AUTH_REJECT_SEQ = 5412
AUTH_REJECT_CODE = 5413
AUTHORITY_EPOCH = 5414
AUTH_TRANSITION_STATE = 5415

REQUEST_TYPE_SINGLE_FREQUENCY = 1
REQUEST_MODE_MANUAL = 1
REQUEST_MODE_AUTO = 2

AUTHORITY_OWNER_PLC_LOCAL = 0
AUTHORITY_OWNER_LOCAL_SCREEN = 1
AUTHORITY_OWNER_EDGE = 2
AUTHORITY_OWNER_HMI_DIRECT = 3
AUTHORITY_OWNER_MAINTENANCE = 4
AUTHORITY_OWNER_FAULT = 5

AUTH_SOURCE_EDGE = 1
AUTH_ACTION_REQUEST_OWNER = 1
AUTH_TRANSITION_STABLE = 0

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


@dataclass(frozen=True)
class AuthorityStatus:
    owner: int
    last_seen_seq: int
    grant_seq: int
    reject_seq: int
    reject_code: int
    epoch: int
    transition_state: int


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
        self._last_auth_seq = 0
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

        authority = await self.ensure_edge_authority(timeout_sec=timeout_sec)
        if not authority.ok:
            return FrequencyTransactionResult(
                status="failed",
                seq=chosen_seq,
                channel=request_channel,
                value=request_value,
                reason=authority.reason,
                detail=authority.detail,
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

    async def ensure_edge_authority(self, *, timeout_sec: float = 3.0, request_timeout_s: int = 10) -> FrequencyTransactionResult:
        """Ensure PLC owner/epoch grants the edge lane before a D5100 write."""
        status = await self.read_authority_status()
        if status is None:
            return FrequencyTransactionResult(status="failed", seq=0, reason="authority_read_failed")

        if status.owner in (AUTHORITY_OWNER_HMI_DIRECT, AUTHORITY_OWNER_MAINTENANCE, AUTHORITY_OWNER_FAULT):
            return FrequencyTransactionResult(
                status="failed",
                seq=0,
                reason="authority_owner_not_edge",
                detail={
                    "authority_owner": status.owner,
                    "authority_epoch": status.epoch,
                    "transition_state": status.transition_state,
                },
            )

        if status.epoch <= 0:
            return FrequencyTransactionResult(
                status="failed",
                seq=0,
                reason="authority_epoch_invalid",
                detail={
                    "authority_owner": status.owner,
                    "authority_epoch": status.epoch,
                    "transition_state": status.transition_state,
                },
            )

        if status.transition_state != AUTH_TRANSITION_STABLE:
            return FrequencyTransactionResult(
                status="failed",
                seq=0,
                reason="authority_transition_not_stable",
                detail={
                    "authority_owner": status.owner,
                    "authority_epoch": status.epoch,
                    "transition_state": status.transition_state,
                },
            )

        if status.owner == AUTHORITY_OWNER_EDGE:
            return FrequencyTransactionResult(
                status="success",
                seq=0,
                detail={"authority_owner": status.owner, "authority_epoch": status.epoch},
            )

        seq = self._next_auth_sequence(status)
        payload = [
            AUTH_SOURCE_EDGE,
            AUTH_ACTION_REQUEST_OWNER,
            self._u16(status.epoch),
            self._u16(request_timeout_s),
        ]
        if not await self.modbus_client.write_registers(self._addr(AUTH_REQ_SOURCE), payload):
            return FrequencyTransactionResult(status="failed", seq=seq, reason="authority_write_failed")
        if not await self.modbus_client.write_register(self._addr(AUTH_REQ_SEQ), seq):
            return FrequencyTransactionResult(status="failed", seq=seq, reason="authority_write_failed")

        return await self._wait_for_authority_grant(seq, timeout_sec)

    async def read_authority_status(self) -> AuthorityStatus | None:
        owner_register = await self.modbus_client.read_registers(
            self._addr(AUTHORITY_OWNER),
            1,
            function_code=3,
        )
        auth_registers = await self.modbus_client.read_registers(
            self._addr(AUTH_LAST_SEEN_SEQ),
            6,
            function_code=3,
        )
        if owner_register is None or len(owner_register) != 1:
            return None
        if auth_registers is None or len(auth_registers) != 6:
            return None
        return AuthorityStatus(
            owner=self._u16(owner_register[0]),
            last_seen_seq=self._u16(auth_registers[0]),
            grant_seq=self._u16(auth_registers[1]),
            reject_seq=self._u16(auth_registers[2]),
            reject_code=self._u16(auth_registers[3]),
            epoch=self._u16(auth_registers[4]),
            transition_state=self._u16(auth_registers[5]),
        )

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

    async def _wait_for_authority_grant(self, seq: int, timeout_sec: float) -> FrequencyTransactionResult:
        deadline = asyncio.get_running_loop().time() + float(timeout_sec)
        last_status: AuthorityStatus | None = None

        while asyncio.get_running_loop().time() <= deadline:
            status = await self.read_authority_status()
            if status is not None:
                last_status = status
                if status.grant_seq == seq:
                    if status.owner != AUTHORITY_OWNER_EDGE:
                        return FrequencyTransactionResult(
                            status="failed",
                            seq=seq,
                            reason="authority_grant_owner_mismatch",
                            detail={
                                "authority_owner": status.owner,
                                "authority_epoch": status.epoch,
                                "transition_state": status.transition_state,
                            },
                        )
                    if status.epoch <= 0:
                        return FrequencyTransactionResult(
                            status="failed",
                            seq=seq,
                            reason="authority_epoch_invalid",
                            detail={
                                "authority_owner": status.owner,
                                "authority_epoch": status.epoch,
                                "transition_state": status.transition_state,
                            },
                        )
                    if status.transition_state != AUTH_TRANSITION_STABLE:
                        return FrequencyTransactionResult(
                            status="failed",
                            seq=seq,
                            reason="authority_grant_not_stable",
                            detail={
                                "authority_owner": status.owner,
                                "authority_epoch": status.epoch,
                                "transition_state": status.transition_state,
                            },
                        )
                    self._last_auth_seq = max(self._last_auth_seq, seq)
                    return FrequencyTransactionResult(
                        status="success",
                        seq=seq,
                        detail={
                            "authority_owner": status.owner,
                            "authority_epoch": status.epoch,
                            "transition_state": status.transition_state,
                        },
                    )
                if status.reject_seq == seq:
                    self._last_auth_seq = max(self._last_auth_seq, seq)
                    return FrequencyTransactionResult(
                        status="failed",
                        seq=seq,
                        reason=f"authority_reject_{status.reject_code}",
                        detail={
                            "authority_owner": status.owner,
                            "authority_epoch": status.epoch,
                            "auth_reject_code": status.reject_code,
                            "transition_state": status.transition_state,
                        },
                    )

            await asyncio.sleep(self.poll_interval_sec)

        detail = {}
        if last_status is not None:
            detail = {
                "authority_owner": last_status.owner,
                "authority_epoch": last_status.epoch,
                "auth_grant_seq": last_status.grant_seq,
                "auth_reject_seq": last_status.reject_seq,
                "auth_reject_code": last_status.reject_code,
                "transition_state": last_status.transition_state,
            }
        logger.warning("authority_request_timeout", seq=seq, detail=detail)
        return FrequencyTransactionResult(status="timeout", seq=seq, reason="authority_timeout", detail=detail)

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

    def _next_auth_sequence(self, status: AuthorityStatus) -> int:
        high_water = max(status.last_seen_seq, status.grant_seq, status.reject_seq)
        self._last_auth_seq = max(self._last_auth_seq, high_water)
        self._last_auth_seq = 1 if self._last_auth_seq >= MAX_SEQUENCE else self._last_auth_seq + 1
        return self._last_auth_seq

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
