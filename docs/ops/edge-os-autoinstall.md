# Edge OS autoinstall status

RB-6 now has an isolated public-material-only implementation candidate. The
media contract contains the immutable host runner and units, separate public H/S
trust roots, an exact signed H1/T1/S1 pair, the Cloud identity public key, a
public HTTPS/factory-NIC configuration, and offline apt/Python dependencies. It
contains no password, batch session, bootstrap token, Tailnet key, registry
credential, production API key, device private key, or signing private key.

Formal release flow is fail closed:

1. `scripts/build-os-installer-release.sh` runs on the controlled Ubuntu 24.04
   amd64 builder, requires `--base-iso-sha256` from the separately approved
   Ubuntu 24.04.4 live-server checksum, requires it to match the reviewed policy
   allowlist, and verifies the ISO bytes before using the base as a diff baseline.
   It also consumes the Cloud `infra/headscale/versions.lock.json` plus its exact
   public Tailscale DEB and rejects version, architecture, URL, package metadata,
   or SHA-256 drift. It then rebuilds all inputs, verifies the final ISO, and
   signs the adjacent release manifest.
2. `scripts/write-os-installer-usb.sh` requires an independently supplied
   installer public key, verifies the signed manifest and ISO again, checks an
   external physical whole disk twice, writes it, and raw-compares the result.

Both entrypoints run the same fail-closed content policy over the source bundle,
signed HostRelease bundle, every SoftwareRelease image config/layer, final ISO,
and extracted installed filesystem. Nested newc/CPIO initrds and compressed
members are scanned recursively; unsupported archives fail closed. ISO and real
SquashFS metadata are counted before extraction, with fixed member, entry,
expanded-byte, recursion, and wall-clock limits. The policy rejects reusable
Tailscale auth/API keys as well as private keys, registry credentials, cloud
access keys, password hashes, and known long-lived tokens.

The public configuration input has exactly this shape; the factory interface is
the one physical NIC reserved for the isolated link-local station channel:

```json
{
  "schema": "drycontroller.os-installer-public-config.v3",
  "cloud_ops_base_url": "https://ops.example.invalid",
  "cloud_ops_tailnet_address": "100.100.0.10",
  "tailscale_login_server": "https://headscale.example.invalid",
  "factory_interface": "enp2s0",
  "factory_address": "169.254.203.2/30"
}
```

The media contains the public `tailscale-client.json` lock and exact amd64 DEB,
but no auth key. First boot verifies the DEB again, installs only the locked
version, enables host `tailscaled`, and requires `/var/lib/tailscale` to remain
a real root-owned mode `0700` directory. Runtime preflight reads actual
`tailscale status --json` plus the local preferences and compares the running
version, login server, online state, node ID, and hostname with the TPM-bound
Device identity. Cloud ingress is a systemd socket bound only to
`tailscale0:8000`, proxying to the loopback-only Edge API at
`127.0.0.1:18080`; its active state is part of the same preflight. A failed remote-channel check is reported as degraded for
local operation and fails strict shipment/OTA preflight. The edge-api container
reads only the sanitized result through the existing fixed-route root UDS; it
never receives the host `tailscaled.sock`.

The candidate has not been built, written, or installed on real hardware. The
workspace audit remains authoritative: RB-5B/RB-5C hardware gates, independent
DANGER review, UEFI blank-disk install, interruption/tamper matrix, real TPM
rebinding, and H1/T1/S1 health must pass before production media is allowed.
