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
- Edge API stays on host loopback `127.0.0.1:18080`. Successful Device-bound
  Tailnet provisioning enables a systemd socket bound only to `tailscale0:8000`
  and forwards one HTTP request at a time through the fixed marking proxy to
  loopback. The proxy strips spoofed ingress markers; marked requests require
  the Cloud Ed25519 signature or an API key. It rejects Tailnet WebSockets,
  ambiguous/chunked framing, bodies over 1 MiB, pipelined requests, slow reads,
  and connections above its fixed concurrency limit.
  LAN/PLC interfaces never receive the Cloud admin listener.
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
| `edge-monitor` | Read-only system/service health, metrics, ops log proxy |
| `edge-enroll` | Per-device credential authenticated Cloud status reporter |
| `drycontroller-ota-helper.service` | Root-owned, operation-ID-only host OTA boundary |
| PostgreSQL, InfluxDB, Mosquitto | Local data and message infrastructure |

## Quick Start

Use this only for local development or staging simulation. RB-5A deliberately
does not produce deployable factory media; the retired shared factory credential
path has been removed and production media stays fail-closed until RB-5B/RB-6.

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
| `.env` | Non-secret Edge runtime configuration |
| `configs/tags.yaml` | PLC tag map |
| `configs/ai-config.yaml` | AI model configuration |
| `configs/monitor.yaml` | Health and self-heal policy |
| `configs-runtime/plc.yaml` | Cloud-downloaded PLC communication config |
| `/etc/drycontroller/keys/cosign.pub` | OTA image signature trust root |
| `/etc/drycontroller/keys/license-trust/policy.json` | Atomic selector for the active canonical License trust root |
| `/etc/drycontroller/license/license.json` | Signed local license |
| `/var/lib/drycontroller/device.json` | Root-owned `0600` active per-device runtime identity and 24-hour credential |
| `/var/lib/drycontroller-identity/` | Root-only identity backend and candidate state; never mounted into business containers |

`configs-runtime/plc.yaml` takes precedence over PLC fields in `.env`. This is
intentional: Cloud Ops can update Modbus host/port/slave settings without
changing Ubuntu network interfaces.

## API And WebSocket Notes

Representative local endpoints:

```text
GET  /ready
GET  /api/v1/system/status?line=L1
GET  /api/v1/data/latest
GET  /api/v1/data/history
GET  /api/v1/control/recommendations
POST /api/v1/control/manual/set
POST /api/v1/control/auto/dispatch
POST /api/v1/ops/plc-config/apply
WS   /api/ws/realtime
WS   /api/ws/ai
WS   /api/ws/system
```

Do not pass API tokens in WebSocket URLs. Clients must use the authenticated
handshake implemented by `Drycontroller-frontend/packages/utils/src/websocket.ts`.

Control V1 production mode is `MANUAL` / `AUTO`: `MANUAL` lets an operator set
the six frequency targets through `POST /api/v1/control/manual/set`; `AUTO`
uses `POST /api/v1/control/auto/dispatch` for a single authorized AUTO tick.
Per-command confirmation endpoints are not the production AUTO workflow.

## Factory Identity (RB-5A / RB-5B)

An administrator authorizes one Cloud batch. A root-only factory station then
connects to exactly one unregistered IPC over the fixed isolated Ethernet tuple
`169.254.203.1/30 -> 169.254.203.2:8765`, creates one Device, transfers the
15-minute one-use bootstrap material in memory, and confirms the Edge-generated
runtime credential hash. The credential plaintext exists only in the Edge
root-owned state file; Cloud stores only its hash.

RB-5A has an explicit `file-test` key backend for CI/development. The formal
service and credential-rotation units select only the RB-5B `tpm` backend and
allow only `/dev/tpmrm0`. That backend recreates one owner-hierarchy primary on
every operation from a single fixed ECDSA P-256/SHA-256 template. TPM primary
derivation therefore returns the same DER SPKI on the same TPM without a
persistent handle or disk private key. Its temporary context is root-only,
deleted after use, while the kernel TPM resource manager releases the transient
object when each short-lived tool exits. It never becomes a portable private key.

A service or IPC restart and an SSD reinstall keep the fingerprint when the TPM
hierarchy seed is preserved. A TPM clear changes that seed and therefore creates
a new hardware identity; firmware maintenance that preserves the seed does not.
Persistent-handle conflicts do not apply because this implementation does not
claim a persistent handle. Missing/locked/cleared TPM state or signing failure
blocks registration and credential rotation without selecting `file-test` or
stopping the already installed active release and local control plane.
Rotation failure also appends a root-only persistent alarm event; the running
Edge API commits its local alarm/outbox before Cloud delivery, and a later
successful rotation appends the matching resolution without overwriting an
unconsumed failure.

Production media remains fail-closed: RB-6, not RB-5B, restores the OS Installer
with `tpm2-tools`, public trust material, and the reviewed identity units. No
device private key, shared bootstrap token, or production file-key fallback is
permitted in that media.

Tailnet key issuance and product integration are separate RB-7P/RB-7I work and
are not an identity fallback.

## Verification

Use repo-local environments where available. Known-good commands from recent
staging/security work include:

```bash
services/edge-api/.venv/bin/python -m pytest services/edge-api/tests -q
bash -n infra/bootstrap/install-edge-host.sh
../scripts/agent-test-env.sh pytest services/device-identity/tests services/edge-api/tests -q
```

Run only the relevant subset for a scoped change, but use actual commands before
claiming a path is verified.

## Operational Docs

| Doc | Purpose |
|---|---|
| `docs/ops/edge-os-autoinstall.md` | OS Installer USB operator flow |
| `docs/ops/db-migrations.md` | Edge Alembic migration flow |
| `docs/archive/ops/provisioning-bootstrap.md` | Retired Provisioning USB bootstrap boundary |
| `CONTRIBUTING.md` | Git hooks and contribution rules |
| `QUICKSTART.md` | Older quick-start notes; verify against current audit before relying on them |

## Open Boundaries

Current open work is tracked in the top-level audit, not in this README. As of
the latest audit snapshot, important edge-adjacent items include:

- Finish remaining outbox producers/drainers beyond audit and control-command.
- Complete RB-4 signed release identity and deterministic rollback before OTA deployment.
- Keep the root OTA helper and its UDS unavailable to every container except `edge-api`.
- Complete real hardware acceptance for OS Installer USB.

Do not close or reinterpret those boundaries from this README; update
`../architecture-audit-2026-05-06.md` when verified state changes.
