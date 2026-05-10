"""
Shared schemas between cloud-ops and edge-api.

P1-8 phase B (decision: 'c' — each repo keeps an identical copy, CI hash-checks
parity). The canonical mirror lives at
Drycontroller-cloudserver/services/cloud-ops/app/common/schemas/. Whichever side
the schema is edited on, the change must be copied byte-for-byte to the other
side; CI hash-check (P1-8 phase D) will enforce this once Gitea runner is live.

See Drycontroller-cloudserver/docs/architecture/shared-schemas.md.
"""
