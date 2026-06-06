# Drycontroller Edge

`Drycontroller-edge` is the edge IPC runtime for DryController. It runs the local
industrial stack close to the PLC: data acquisition, AI inference, control
optimization, local license verification, health monitoring, audit/outbox
syncing, and bootstrap/OTA execution.

This repository is one of three independent DryController repos:

| Repo | Role | Branch |
|---|---|---|
| `Drycontroller-edge` | Edge IPC runtime and OS Installer USB assets | `master` |
| `Drycontroller-cloudserver` | Cloud Ops, Cloud Console, Headscale, activation tooling | `main` |
| `Drycontroller-frontend` | Large-screen dashboard frontend | `main` |

The top-level workspace audit is the source of truth for cross-repo status:
`../architecture-audit-2026-05-06.md`.

## Current State

The edge stack is in staging-ready shape for software validation, with these
boundaries:

- Edge API, license-service, databases, Prometheus, and Alertmanager default to
  loopback/local exposure.
- Tailnet provisioning starts from loopback and only rebinds edge-api to the
  actual tailnet IPv4 after tailnet and cloud readiness checks pass.
- WebSocket realtime, AI, and system streams require the current mutual-auth
  handshake: client `hello` nonce, server HMAC proof, client `auth`, then
  `auth_ok`.
- Local audit outbox is active for audit and control-command domains. Other
  edge event classes remain backlog unless the top-level audit says otherwise.
- OS Installer USB assets are maintained here. Physical USB write, BIOS boot,
  and IPC installation still require real hardware acceptance.

## Runtime Components

| Component | Purpose |
|---|---|
| `edge-api` | FastAPI gateway, REST/WebSocket APIs, ops proxy, audit/outbox producer |
| `edge-daq` | PLC/Modbus data acquisition, MQTT publish, InfluxDB writes |
| `ai-softsensor` | ONNX-based moisture soft sensor |
| `ai-ctrl-optimizer` | Control optimization service |
| `license-service` | Local signed license verification |
| `edge-monitor` | Docker/system health, self-heal, metrics, ops log proxy |
| `edge-enroll` | Cloud enroll/status reporter |
| `edge-ota-agent` | Online OTA execution surface |
| PostgreSQL, InfluxDB, Mosquitto | Local data and message infrastructure |

## Quick Start

Use this only for local development or staging simulation. Factory installation
should use the OS Installer USB with factory activation materials
(`activation-bundle.json`). Provisioning USB is retired and is not generated
for new factory media.

```bash
cp env.example .env
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8000/ready
```

Open the API docs locally:

```bash
open http://127.0.0.1:8000/docs
```

If no PLC is available, run the simulator so MQTT/InfluxDB and the dashboard can
see live-looking points:

```bash
python services/edge-daq/simulator.py
```

## Configuration

Important runtime files and generated state:

| Path | Purpose |
|---|---|
| `.env` | Edge runtime environment, generated/updated by activation in production |
| `configs/tags.yaml` | PLC tag map |
| `configs/ai-config.yaml` | AI model configuration |
| `configs/monitor.yaml` | Health and self-heal policy |
| `configs-runtime/plc.yaml` | Cloud-downloaded PLC communication config |
| `/etc/drycontroller/keys/cosign.pub` | OTA image signature trust root |
| `/etc/drycontroller/keys/license.pub` | License verification public key |
| `/etc/drycontroller/license/license.json` | Signed local license |
| `/var/lib/drycontroller/.bootstrapped` | Legacy bootstrap sentinel; new factory activation can complete with `formal_license_active` |

`configs-runtime/plc.yaml` takes precedence over PLC fields in `.env`. This is
intentional: Cloud Ops can update Modbus host/port/slave settings without
changing Ubuntu network interfaces.

## API And WebSocket Notes

Representative local endpoints:

```text
GET  /ready
GET  /api/v1/system/status?line=L1
GET  /api/realtime/latest
GET  /api/history
POST /api/v1/control/set
POST /api/v1/ops/plc-config/apply
WS   /api/ws/realtime
WS   /api/ws/ai
WS   /api/ws/system
```

Do not pass API tokens in WebSocket URLs. Clients must use the authenticated
handshake implemented by `Drycontroller-frontend/packages/utils/src/websocket.ts`.

## Installation Media

DryController uses one mainline installation artifact:

| Artifact | Maintained by | Purpose |
|---|---|---|
| OS Installer USB | This repo | Install Ubuntu, Docker, edge repo, host activation services, and product-path `activation-bundle.json` |

OS Installer USB documentation:

- `docs/ops/edge-os-autoinstall.md`
- `infra/autoinstall/README.md`

Provisioning USB documentation is kept only to mark the retired path. Do not use
it as the normal factory path or as an acceptance condition for new installs.

macOS imaging note: use the raw device, for example `/dev/rdisk2`, not
`/dev/disk2`, when writing installer media with `dd`.

## Bootstrap And Tailnet

The production bootstrap path is activation-bundle first:

High-level sequence:

1. OS Installer copies `activation-bundle.json`.
2. Host Tailscale joins the bootstrap tailnet.
3. `drycontroller-activation.service` calls Cloud Ops factory activation.
4. Cloud Ops auto-approves valid signed factory material and returns the
   production license, `device_id`, `node_name`, and the node enroll token.
5. Host activation binds the current tailnet node, syncs `.env`, starts host
   enroll, and starts the edge stack.
6. Edge preflight accepts `formal_license_active` as the new mainline completion
   signal.

Legacy `tailnet.env` and `factory-license.json` remain accepted only as
development/emergency debug inputs; they are not the product install path.

If tailnet readiness fails, activation must fail closed rather than silently
falling back to public Cloud Ops. The old `drycontroller-provisioning/`
bootstrap path has been removed from active installs.

## Verification

Use repo-local environments where available. Known-good commands from recent
staging/security work include:

```bash
services/edge-api/.venv/bin/python -m pytest services/edge-api/tests -q
bash -n infra/bootstrap/install-edge-host.sh
bash -n infra/bootstrap/drycontroller-host-tailnet.sh
bash -n infra/bootstrap/drycontroller-host-activation.sh
```

Mainline bootstrap/preflight tests are run from the top-level `DryController`
workspace:

```bash
Drycontroller-edge/.venv-test/bin/python -m pytest \
  Drycontroller-edge/infra/bootstrap/tests/test_drycontroller_bootstrap_headscale_first.py -q
```

Run only the relevant subset for a scoped change, but use actual commands before
claiming a path is verified.

## Operational Docs

| Doc | Purpose |
|---|---|
| `docs/ops/edge-os-autoinstall.md` | OS Installer USB operator flow |
| `docs/ops/provisioning-bootstrap.md` | Retired Provisioning USB bootstrap boundary |
| `docs/ops/db-migrations.md` | Edge Alembic migration flow |
| `CONTRIBUTING.md` | Git hooks and contribution rules |
| `QUICKSTART.md` | Older quick-start notes; verify against current audit before relying on them |

## Open Boundaries

Current open work is tracked in the top-level audit, not in this README. As of
the latest audit snapshot, important edge-adjacent items include:

- Finish remaining outbox producers/drainers beyond audit and control-command.
- Productize OTA online update and long-running CI runner.
- Reduce OTA agent Docker socket exposure.
- Complete real hardware acceptance for OS Installer USB.

Do not close or reinterpret those boundaries from this README; update
`../architecture-audit-2026-05-06.md` when verified state changes.
