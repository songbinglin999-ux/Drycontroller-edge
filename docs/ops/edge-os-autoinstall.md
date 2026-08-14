# Edge OS autoinstall status

RB-6 now has an isolated public-material-only implementation candidate. The
media contract contains the immutable host runner and units, separate public H/S
trust roots, an exact signed H1/T1/S1 pair, the Cloud identity public key, a
public HTTPS/factory-NIC configuration, and offline apt/Python dependencies. It
contains no password, batch session, bootstrap token, Tailnet key, registry
credential, production API key, device private key, or signing private key.

Formal release flow is fail closed:

1. `scripts/build-os-installer-release.sh` runs on the controlled Ubuntu 24.04
   amd64 builder, rebuilds all inputs, verifies the final ISO, and signs the
   adjacent release manifest.
2. `scripts/write-os-installer-usb.sh` requires an independently supplied
   installer public key, verifies the signed manifest and ISO again, checks an
   external physical whole disk twice, writes it, and raw-compares the result.

The public configuration input has exactly this shape; the factory interface is
the one physical NIC reserved for the isolated link-local station channel:

```json
{
  "schema": "drycontroller.os-installer-public-config.v1",
  "cloud_ops_base_url": "https://ops.example.invalid",
  "factory_interface": "enp2s0",
  "factory_address": "169.254.203.2/30"
}
```

The candidate has not been built, written, or installed on real hardware. The
workspace audit remains authoritative: RB-5B/RB-5C hardware gates, independent
DANGER review, UEFI blank-disk install, interruption/tamper matrix, real TPM
rebinding, and H1/T1/S1 health must pass before production media is allowed.
