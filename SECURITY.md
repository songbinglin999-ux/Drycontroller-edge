# Security Policy

DryController Edge talks to physical equipment. Misconfiguration can move
motors, change set-points or hide alarms. Treat every deployment as
safety-relevant.

## Threat model and required deployment controls

1. **Modbus TCP is unauthenticated.** Anyone who can reach the PLC or the
   Edge HMI proxy port on the field LAN can read and (if enabled) write
   registers. IP allowlists (`allowed_client_ips`) can be spoofed on the same
   L2 segment and are **not** an identity.
   - Put the PLC, the vendor HMI and the Edge field NIC on a dedicated,
     physically or VLAN-isolated network with no route to office/Internet.
   - Firewall the Edge field interface so only the HMI can reach the proxy
     port, and only the Edge can reach the PLC.
2. **HMI proxy writes are disabled by default** (`configs/modbus-proxy.yaml`:
   `write_enabled: false`). Only enable writes for addresses whose range,
   repeat-safety and PLC-side protection are signed off, and keep ranges as
   narrow as possible. Never open a whole register area.
3. **The PLC remains the safety authority.** Emergency stop, interlocks and
   equipment protection must be implemented in the PLC / hard-wired and must
   not depend on the Edge. This software is not SIL/PL certified.
4. **Secrets.** Replace every `change_me_*` value in `env.example` /
   `.env.example`. `edge-api` refuses to start in production with default
   Postgres/MQTT passwords. Never commit `.env`, license private keys, cosign
   private keys, registry credentials or device state.
5. **Network exposure.** Databases, MQTT, Prometheus and `edge-api` bind to
   `127.0.0.1` by default. The read-only data plane
   (`READONLY_WS_ALLOWLIST`) defaults to loopback only; widen it explicitly.
   Remote admin access is meant to go through a tailnet (Tailscale/Headscale)
   and the single-request ingress proxy, never a public port.
6. **OTA and licensing trust roots.** Replace `configs/keys/cosign.pub` and the
   license trust policy with your own keys before building releases.

## Supported versions

Only the latest `main` branch receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
private vulnerability reporting ("Security" tab → "Report a vulnerability") on
this repository. Include affected component, version/commit, reproduction
steps and impact. You can expect an acknowledgement within 7 days.
