# Remote debug status

RB-5A does not provide a Tailnet bootstrap or shared Cloud credential debug
path. Device identity tests use the explicit `file-test` backend only in CI,
development machines, or isolated VMs. Production configuration selects `tpm`
and fails closed until RB-5B.

Tailnet provisioning and remote-path validation are deferred to RB-7P/RB-7I.
