# DryController Edge

**English** · [中文](#中文简介)

An open-source **edge controller runtime for industrial dryer lines**. It runs on
an industrial PC next to the PLC and provides data acquisition over Modbus TCP,
a supervised control path, a local API and dashboards, health monitoring,
TPM-bound device identity and licensing, and signed application OTA updates.

> Status: production-derived code released as a generic reference
> implementation. Site-specific configuration has been replaced with examples.
> Read [SECURITY.md](SECURITY.md) before connecting it to real equipment.

## Features

- **edge-daq** – Modbus TCP/RTU-over-TCP acquisition, MQTT publish, InfluxDB
  writes, signed control-command execution, optional address-preserving HMI
  proxy (writes off by default).
- **edge-api** – FastAPI REST + WebSocket gateway, API-key/HMAC auth, MANUAL /
  AUTO control orchestration, in-process license admission, audit/outbox.
- **edge-monitor** – service health, Prometheus metrics, self-healing policy.
- **device-identity** – TPM 2.0 (ECDSA P-256) device identity, factory
  enrollment over an isolated link, 24h credential rotation.
- **Licensing** – Ed25519-signed, device/TPM-bound licenses with atomic
  trust-policy + license pair activation and crash-safe recovery.
- **OTA** – cosign-signed two-image `edge-app-v1` releases, root-owned helper on
  a UNIX socket, deterministic rollback, DB-migration gate.
- **Tailnet ingress** – optional Tailscale/Headscale-only admin ingress through
  a strict single-request proxy.
- **ops-console** – small Svelte operations console.

## Architecture

```text
            ┌──────────────── Edge IPC (Docker + systemd) ────────────────┐
 PLC ◀─Modbus TCP─▶ edge-daq ──MQTT──▶ Mosquitto ◀── edge-api ◀── HMI / dashboard
                     │                                  │  ▲
                     └──▶ InfluxDB      PostgreSQL ◀────┘  │ REST / WS (API key, HMAC)
                                                           │
   edge-monitor ─▶ Prometheus / Alertmanager                │
   device-identity (TPM) · license verifier · OTA helper (UDS, root) ─┘
            └──────────────── optional tailnet ingress ─▶ companion cloud ┘
```

The companion cloud server (fleet management, activation, release registry) is a
separate project and is **not** included; the edge runtime runs standalone.
Shared cloud↔edge schemas are vendored in `common/schemas/`.

## Quick start (simulator, no hardware)

```bash
cp env.example .env          # replace every change_me_* value
docker compose -f docker-compose.yml -f docker-compose.sim.yml up -d --build \
    postgres influxdb mosquitto modbus-sim edge-api edge-daq
curl -fsS http://127.0.0.1:18080/ready   # host port of edge-api, see docker-compose.yml
```

`docker-compose.sim.yml` starts a Modbus slave simulator
(`services/edge-daq/modbus_slave_sim.py`) with time-varying values for the
example point map in `configs/`. For a real PLC start with the read-only
`docker-compose.field-comm.yml` profile and `PLC_HOST=<your PLC>`; see
[docs/plc/plc-comm-protocol.md](docs/plc/plc-comm-protocol.md).

## Tests

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements-test.txt
python -m pytest -q services common tests infra scripts
ruff check services/ common/ scripts/ --per-file-ignores "**/tests/**:E402"
```

CI (GitHub Actions, Python 3.11) runs the same suite: `.github/workflows/edge-tests.yml`.

## Repository layout

| Path | Contents |
|---|---|
| `services/` | `edge-api`, `edge-daq`, `edge-monitor`, `device-identity` |
| `common/` | shared logging, license verifier, schemas |
| `infra/` | bootstrap/systemd units, OTA helper, mosquitto, Prometheus, release images |
| `configs/` | example line profile (tags, address map, limits, HMI proxy) |
| `scripts/` | release, device-prep and maintenance tooling |
| `ops-console/` | Svelte operations console |
| `docs/` | architecture, PLC integration, ops docs (mostly Chinese) |

## Security

Modbus TCP is unauthenticated. Isolate the PLC/HMI network and keep the HMI
proxy writes disabled unless every address is signed off. See
[SECURITY.md](SECURITY.md) for the threat model and how to report vulnerabilities.

## License

[MIT](LICENSE) © 2025-2026 songbinglin999-ux. Third-party components: [NOTICE](NOTICE).

---

## 中文简介

DryController Edge 是一套面向**工业烘干产线**的开源边缘控制运行时，部署在 PLC 旁的工控机上：

- 通过 Modbus TCP 采集 PLC 数据，经 MQTT / InfluxDB / PostgreSQL 落地；
- 提供 MANUAL / AUTO 受控控制链路、REST/WebSocket API 和运维控制台；
- 内置基于 TPM 的设备身份、设备绑定的签名 License、cosign 签名的 OTA 与确定性回滚；
- 可选 Tailscale/Headscale 运维通道和厂商 HMI 同地址代理（默认禁写）。

快速体验：见上方 *Quick start*（模拟器，无需硬件）。测试：Python 3.11 + `requirements-test.txt`。
接入真实设备前请阅读 [SECURITY.md](SECURITY.md)。配套云端服务为独立项目，不在本仓库。
详细中文文档见 `docs/` 与 [QUICKSTART.md](QUICKSTART.md)。

---

## Design notes

### Current state

The edge stack is in staging-ready shape for software validation, with these
boundaries:

- Edge API, databases, Prometheus, and Alertmanager default to
  loopback/local exposure.
- The retired `license-service`, `ai-softsensor`, and `ai-ctrl-optimizer`
  service trees are absent from this repository; they are not runtime services
  or `edge-app-v1` artifacts.
- Edge API stays on host loopback `127.0.0.1:18080`. Successful Device-bound
  Tailnet provisioning enables a systemd socket bound only to `tailscale0:8000`
  and forwards one HTTP request at a time through the fixed marking proxy to
  loopback. The proxy strips spoofed ingress markers; marked requests require
  the Cloud Ed25519 signature or an API key. It rejects Tailnet WebSockets,
  ambiguous/chunked framing, bodies over 1 MiB, pipelined requests, slow reads,
  and connections above its fixed concurrency limit.
  LAN/PLC interfaces never receive the Cloud admin listener.
- WebSocket realtime and system streams require the current mutual-auth
  handshake: client `hello` nonce, server HMAC proof, client `auth`, then
  `auth_ok`.
- Local audit outbox is active for audit and control-command domains. Other
  edge event classes remain backlog.
- The retired HostRelease/12-service OS Installer path has been removed. 

### Runtime components

| Component | Purpose |
|---|---|
| `edge-api` | FastAPI gateway, REST/WebSocket APIs, in-process License admission, audit/outbox producer |
| `edge-daq` | PLC/Modbus data acquisition, MQTT publish, InfluxDB writes |
| `edge-monitor` | Read-only system/service health, metrics, ops log proxy |
| `edge-enroll` | Per-device credential authenticated Cloud status reporter |
| `drycontroller-ota-helper.service` | Root-owned, operation-ID-only `edge-app-v1` OTA boundary |
| PostgreSQL, InfluxDB, Mosquitto | Local data and message infrastructure |

### Local development notes

Use this only for local development or staging simulation. The retired factory
media path has been removed; this repository currently produces no deployable
OS/USB media.

```bash
cp env.example .env   # replace every change_me_* value first
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:18080/ready
```

Open the API docs locally:

```bash
open http://127.0.0.1:18080/docs
```

If no PLC is available, run the simulator so MQTT/InfluxDB and the dashboard can
see live-looking points:

```bash
python services/edge-daq/simulator.py
```

### Configuration

Important runtime files and generated state:

| Path | Purpose |
|---|---|
| `.env` | Non-secret Edge runtime configuration |
| `configs/tags.yaml` | PLC tag map |
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

The application-release retrofit is deliberately two-stage. On an existing
host that predates the device-identity runtime, first run the create-once
`infra/bootstrap/install-device-identity-runtime.py` with an independently
approved offline wheelhouse manifest. It refuses an existing identity runtime
and installs through an atomic staging directory; it does not configure the
network, Docker, application containers, or device identity state. Then run
`infra/bootstrap/retrofit-edge-app-runtime.sh` to install the isolated
`edge-app-v1` host runtime and retire any installed legacy HostRelease unit.
The script does not pull images, mutate containers, create release state, or
enable the new recovery/helper units. Before reboot, create and independently
sign an approved live two-image baseline with `scripts/create-edge-app-baseline.py`,
initialize it through the installed `initialize-active-release`, then enable
`drycontroller-edge-app-recovery.service` and `drycontroller-ota-helper.service`.
The initializer is safe to rerun only with the byte-identical signed seed and
revalidates both images plus the live containers before rebuilding a missing
journal. An unsigned, mismatched, or incomplete baseline must leave both units
disabled. Manual rollback requires a fixed operation ID plus the expected
active and target previous release IDs; retrying the same bound request returns
the durable result without swapping the slots again.

Daily `edge-app-v1` OTA is application-only. The release workflow compares the
signed source commit with the protected `EDGE_APP_APPROVED_DB_BASELINE_COMMIT`
repository variable and fails closed if the approved range changes any
`services/edge-api/alembic/versions/` file. Database migrations remain an
explicit SSH maintenance and backup operation.

### API and WebSocket notes

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
WS   /api/ws/system
```

Do not pass API tokens in WebSocket URLs. Clients must use the authenticated
handshake (see `services/edge-api/app/core/security.py`).

Control V1 production mode is `MANUAL` / `AUTO`: `MANUAL` lets an operator set
the six frequency targets through `POST /api/v1/control/manual/set`; `AUTO`
uses `POST /api/v1/control/auto/dispatch` for a single authorized AUTO tick.
Per-command confirmation endpoints are not the production AUTO workflow.

### Factory identity (TPM)

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

### Open work

- Finish remaining outbox producers/drainers beyond audit and control-command.
- Keep the signed `edge-app-v1` identity and deterministic rollback contract verified.
- Keep the root OTA helper and its UDS unavailable to every container except `edge-api`.

Operational docs: `docs/ops/db-migrations.md`, `docs/ops/device-prep.md`,
`docs/ops/license-pair-activation.md`, `CONTRIBUTING.md`, `QUICKSTART.md`.
