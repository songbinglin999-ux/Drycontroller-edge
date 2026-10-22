# PLC communication parameters / PLC 通讯参数

This document describes the generic Modbus TCP link between `edge-daq` and a
PLC. All addresses below are **examples** (RFC 5737 documentation ranges);
use the values from your own commissioning record.

本文描述 `edge-daq` 与 PLC 之间的通用 Modbus TCP 链路，下列地址均为示例。

## Link / 链路

| Item | Example | Notes |
|---|---|---|
| Transport | Modbus TCP (MBAP, `MODBUS_FRAMER=socket`) | RTU-over-TCP (`rtu`) is supported for simulators/gateways |
| PLC address | `192.0.2.200` | `PLC_HOST` |
| PLC port | `2000` (or the standard `502`) | `PLC_PORT` |
| Unit / slave id | `1` | `PLC_SLAVE_ID` |
| Scan interval | `0.3` s | `PLC_SCAN_INTERVAL` |
| Edge field interface | `192.0.2.249/24` | dedicated NIC on the PLC LAN |

`edge-daq` is a Modbus **client**; the PLC is the server. Read points are
declared in `configs/tags.yaml` + `configs/modbus-address-map.yaml`
(`D0 -> 40000`, `X/M coils -> 0 / 30000` offsets are typical for many small
PLCs — verify against your PLC manual).

## Bring-up checklist / 联调步骤

1. Put the Edge field NIC on the PLC LAN with a static address; keep the
   management NIC separate.
2. Start with the read-only profile `docker-compose.field-comm.yml`
   (one heartbeat point, no writable addresses).
3. Verify 3+ consecutive reads (FC01/FC03) and the heartbeat toggle.
4. Only then extend the tag map. Writes stay disabled until each address
   has an engineering sign-off (see [hmi-modbus-proxy.md](hmi-modbus-proxy.md)
   and [SECURITY.md](../../SECURITY.md)).

Without hardware, use the simulator profile (`docker-compose.sim.yml`).

## Rollback / 回退

Remove the field-comm override and restart `edge-daq`; it falls back to the
built-in simulator (`USE_SIMULATOR=true`).
