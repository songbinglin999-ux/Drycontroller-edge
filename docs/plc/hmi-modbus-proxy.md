# HMI Modbus TCP proxy / 专用屏 Modbus TCP 代理

An optional, address-preserving Modbus TCP proxy that lets an existing vendor
HMI keep its original PLC project while all traffic flows through the Edge.
**Disabled by default; writes are disabled by default.**

可选的“同地址”Modbus TCP 代理：原有厂商 HMI 工程不改地址，只把目标 IP 改为 Edge。
默认关闭，默认禁写。

```text
Vendor HMI 192.0.2.100
  └─ Modbus TCP client → Edge field IP:2000 / unit 1
                           └─ Modbus TCP → PLC 192.0.2.200:2000 / unit 1
```
(All addresses are documentation-range examples.)

## Behaviour

- Runs inside `edge-daq` (`HMI_PROXY_ENABLED=true` on **both** `edge-api` and
  `edge-daq`). FC01/02/03/04 reads are forwarded on demand; FC05/06/15/16
  writes are only forwarded when they match an explicit rule.
- Standard MBAP on both sides; transaction id, unit, function code, length and
  write echo are validated. PLC timeouts return gateway exception `0x0B`; no
  cached or fake responses.
- At most `max_clients` HMI connections and one upstream PLC request in flight.
- A permitted write first switches the line to MANUAL via
  `POST /api/v1/control/vendor-hmi/takeover` (control API key from
  `HMI_PROXY_CONTROL_API_KEY_FILE` + `production-control` license gate).
- Before a write, a journal entry is fsync'ed. Timeout / bad echo / disconnect
  keeps the **unknown-result lock**: later writes are refused until an operator
  verifies PLC state and clears the journal manually. There is no auto-unlock.
- The proxy never retries writes. MBAP transaction ids are not idempotency keys.

## Configuration

Template: [`configs/modbus-proxy.yaml`](../../configs/modbus-proxy.yaml)
(`write_enabled: false`, one narrow example range). To enable writes you must:

1. set `write_enabled: true` **and** `source_network_verified: true`;
2. list each address/range with `min`, `max`, `repeat_safe: true` and an
   `evidence` reference to your engineering sign-off;
3. bind the listener to one field interface (`HMI_PROXY_BIND_IP`) and firewall
   it so only the HMI can reach it.

An allowed source IP is **not** an identity — Modbus TCP is unauthenticated and
IPs can be spoofed on the same LAN. Network isolation is mandatory; see
[SECURITY.md](../../SECURITY.md).

## Acceptance

Local tests cover real loopback TCP forwarding, fragmentation, all supported
function codes, range/value rejection, source/unit rejection, timeouts,
the persistent lock and API-key/license rejection
(`services/edge-daq/tests/`, `services/edge-api/tests/test_vendor_hmi_takeover.py`).
Field acceptance (driver compatibility, alarm display, power-isolated write
tests, PLC protections) is the integrator's responsibility.
