from __future__ import annotations

import base64
import datetime as dt

import pytest

from identity_service.host_authorization import HostAuthorizationError, sign_host_release_challenge


class FakeBackend:
    def __init__(self):
        self.challenges = []

    def sign_identity_challenge(self, challenge):
        self.challenges.append(challenge)
        return base64.b64encode(b"signature").decode()


def _challenge(now: dt.datetime) -> dict[str, object]:
    return {
        "schema": "drycontroller.device-identity-challenge.v1",
        "domain": "drycontroller.device-identity",
        "operation": "host_release",
        "device_id": "EDGE-001",
        "challenge_id": "11111111-1111-4111-8111-111111111111",
        "challenge_nonce": base64.b64encode(b"n" * 32).decode(),
        "request_id": "0" * 32,
        "identity_fingerprint": "1" * 64,
        "runtime_credential_hash": "2" * 64,
        "tenant_id": "tenant-a",
        "line_id": "line-a",
        "node_name": "edge-001",
        "node_id": 7,
        "job_id": 9,
        "job_item_id": 11,
        "host_release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "host_manifest_digest": "sha256:" + "3" * 64,
        "software_release_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "software_manifest_digest": "sha256:" + "4" * 64,
        "expires_at": (now + dt.timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
    }


def _active() -> dict[str, str]:
    return {
        "device_id": "EDGE-001",
        "identity_fingerprint": "1" * 64,
        "runtime_credential_hash": "2" * 64,
        "tenant_id": "tenant-a",
        "line_id": "line-a",
        "node_name": "edge-001",
    }


def test_host_authorization_signs_one_fresh_exactly_bound_challenge():
    now = dt.datetime(2026, 8, 13, tzinfo=dt.timezone.utc)
    backend = FakeBackend()
    challenge = _challenge(now)

    result = sign_host_release_challenge(challenge, backend=backend, active=_active(), now=now)

    assert result["challenge_id"] == challenge["challenge_id"]
    assert backend.challenges == [challenge]


@pytest.mark.parametrize("mutation", ["device", "extra", "expired", "too_far"])
def test_host_authorization_rejects_wrong_binding_shape_or_expiry(mutation):
    now = dt.datetime(2026, 8, 13, tzinfo=dt.timezone.utc)
    challenge = _challenge(now)
    if mutation == "device":
        challenge["device_id"] = "EDGE-OTHER"
    elif mutation == "extra":
        challenge["command"] = "id"
    elif mutation == "expired":
        challenge["expires_at"] = (now - dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    else:
        challenge["expires_at"] = (now + dt.timedelta(seconds=61)).isoformat().replace("+00:00", "Z")

    with pytest.raises(HostAuthorizationError):
        sign_host_release_challenge(challenge, backend=FakeBackend(), active=_active(), now=now)
