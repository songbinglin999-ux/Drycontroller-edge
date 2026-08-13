import base64
import hashlib
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from identity_service.backend import TpmIdentityBackend
from identity_service.contract import CHALLENGE_DOMAIN, CHALLENGE_SCHEMA, canonical_challenge


@pytest.mark.skipif(
    os.getenv("DRYCONTROLLER_TEST_REAL_TPM") != "1",
    reason="set DRYCONTROLLER_TEST_REAL_TPM=1 only on the isolated target IPC",
)
def test_real_tpm_primary_is_stable_and_signs_the_frozen_contract():
    first = TpmIdentityBackend()
    second = TpmIdentityBackend()
    public_spki = first.public_key()
    assert second.public_key() == public_spki
    fingerprint = hashlib.sha256(base64.b64decode(public_spki, validate=True)).hexdigest()
    expected = os.getenv("DRYCONTROLLER_EXPECTED_TPM_FINGERPRINT")
    if expected:
        assert fingerprint == expected

    challenge = {
        "schema": CHALLENGE_SCHEMA,
        "domain": CHALLENGE_DOMAIN,
        "operation": "rotate",
        "device_id": "EDGE-ABCDEF123456",
        "challenge_id": "00000000-0000-4000-8000-000000000005",
        "challenge_nonce": base64.b64encode(b"h" * 32).decode("ascii"),
        "request_id": "0123456789abcdef0123456789abcdef",
        "identity_fingerprint": fingerprint,
        "runtime_credential_hash": "ab" * 32,
        "expires_at": "2026-08-13T01:02:03Z",
    }
    signature = base64.b64decode(first.sign_identity_challenge(challenge), validate=True)
    public_key = serialization.load_der_public_key(base64.b64decode(public_spki, validate=True))
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert isinstance(public_key.curve, ec.SECP256R1)
    public_key.verify(signature, canonical_challenge(challenge), ec.ECDSA(hashes.SHA256()))
    print(f"RB5B_TPM_FINGERPRINT={fingerprint}")
