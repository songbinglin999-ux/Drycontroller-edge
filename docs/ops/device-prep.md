# Factory device prep (vanilla Ubuntu → lane-ready)

Audit item `FACTORY-DEVICE-PREP-20260903`. Turns a brand-new IPC running a
stock Ubuntu Server 24.04 into an **unregistered, lane-ready** factory Edge
device. Identity, License and Tailnet binding happen later, only on the
factory lane (the cloud-side factory lane belongs to the companion cloud server (separate project, not included in this repository)).

## OS install convention (the only worker skill required)

Install stock **Ubuntu Server 24.04** from the official ISO:

- check **`Install OpenSSH server`** on the SSH Setup page;
- user `dryops` + the unified rescue password (password manager);
- hostname `dry-edge-<asset number>`;
- everything else default (DHCP, guided full-disk).

Keep the rescue password: after prep SSH is key-only, the password keeps
working on the physical console for rescue.

## Office prep (one command per device)

```bash
scripts/prep-device-from-mac.sh dryops@<device-ip> --interface <lane-nic> [--shutdown]
```

The wrapper assembles a **secret-free** bundle and pushes it to the device:

| Bundle input | Source |
|---|---|
| exact repo tree | `git archive HEAD` of a **clean** edge checkout |
| identity wheelhouse | built on a Linux build host (`edge-build-host`, configurable), cached locally per `requirements.txt` + HEAD |
| tailscale `.deb` | pinned version + sha256 from a `versions.lock.json` you provide (`--versions-lock`) |
| cloud identity **public** key | derived read-only inside the cloud-ops container, checked against the cloud-recorded fingerprint |
| `public-config.json` | generated from flags (cloud URL, tailnet address, login server, lane NIC) |
| operator SSH key | `--authorized-keys` (default `~/.ssh/id_ed25519.pub`) |

On the device `infra/bootstrap/prep-factory-device.sh` then verifies the
bundle, installs the pinned tailscale client (without joining), installs the
cloud trust public key, runs `initialize-runtime-config.py` (per-device random
secrets are generated **on the device**), installs the verified device
identity runtime, enables `drycontroller-factory-identity.service` on
`169.254.203.2/30`, switches SSH to key-only for `dryops`, and self-checks
`http://169.254.203.2:8765/health` + `/factory-identity` before reporting
PASS (with the device TPM fingerprint).

## Refusals (fail closed)

- the device already carries activation state (`factory-closed.json` or
  `/var/lib/drycontroller/device.json`) — re-provisioning a registered device
  is a human decision, not a prep rerun;
- an existing `/opt/drycontroller-identity` that fails its TPM backend check;
- checksum mismatches (tailscale deb, wheelhouse manifest, cloud public key),
  a non-physical lane interface, missing `dryops` user, or a dirty edge
  checkout on the Mac.

## After PASS

1. Cable the lane NIC to the factory station lane, power on.
2. The station watch (audit `FACTORY-STATION-WATCH-20260903`) activates the
   device zero-touch.
3. After activation, install the app runtime remotely over Tailnet with the
   existing audited `retrofit-edge-app-runtime.sh` path (Docker and the OTA
   executor are deliberately **not** part of prep; the lane-exposed
   unregistered device stays minimal).

Out of scope by audit boundary: OS images/remasters/USB writes (HARD RULE in
the root `AGENTS.md`), offline on-site bundles, autoinstall seeds, HMI hosts.
