# License Pair Activation

License trust policy and `/etc/drycontroller/license/license.json` are one
release unit. Production activation and rollback use only the root-owned paired
transaction command; the lower-level trust and license APIs are not release
workflows.

```bash
sudo /opt/drycontroller-identity/venv/bin/python \
  /usr/libexec/drycontroller/manage-license-transaction.py activate \
  /root/candidate/license-trust.json \
  /root/candidate/license.json \
  --expected-active license-key-2026-01

sudo /opt/drycontroller-identity/venv/bin/python \
  /usr/libexec/drycontroller/manage-license-transaction.py rollback \
  --expected-active license-key-2026-08
```

The command takes the shared host release lock, verifies the candidate with the
real active-trust loader and Ed25519 verifier, then durably journals each switch.
Any ordinary failure restores the source pair immediately. A process or power
loss leaves the journal in place; the enabled recovery oneshot restores and
verifies the source pair before `docker.service` starts.

Both commands use expected-active compare-and-swap. A mismatch is a failed
operation, not a prompt to retry with a guessed key id. Keep candidate inputs
root-owned regular files. Do not edit the live policy, live license, transaction
state, journal, or immutable trust version directories by hand.

The command fails closed unless the production roots keep their installed
contract: trust and license directories are root-owned mode `0755`; transaction
state, its `/var/lib/drycontroller` parent, and the shared release-lock directory
are root-owned mode `0700`. Policy is mode `0444`, live license is mode `0640`,
and state/journal/lock files are mode `0600`. Inputs and live files must have one
hard link and no group/other write bits. The license service reloads the policy
and license for every verification request, so a verified pair switch does not
require a service restart.

This workflow changes production trust and licensing state. A second agent or a
human must review the candidate pair, command arguments, and verification
evidence before staging or production execution.
