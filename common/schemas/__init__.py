"""
Shared schemas between cloud-ops and edge-api.

This package is vendored in-repo so the edge runtime is self-contained. A
companion cloud server (separate project, not included) keeps a byte-identical
copy of ``cloud_to_edge.py``; the optional ``scripts/check-shared-schemas-sync.sh``
can verify parity against any raw-file URL you provide.
"""
