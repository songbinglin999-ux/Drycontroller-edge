from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def engines():
    return (
        _load(
            "host_release_engine_for_parity",
            ROOT / "infra" / "host-release-runner" / "host_release_engine.py",
        ),
        _load(
            "software_release_engine_for_parity",
            ROOT / "infra" / "ota-helper" / "release_engine.py",
        ),
    )


def _accepted(call: Callable[[], Any]) -> tuple[bool, Any]:
    try:
        return True, call()
    except Exception:
        return False, None


def _registry_document(secret: bytes = b"secret") -> dict[str, Any]:
    registry = "registry.example.com:5000"
    username = "device"
    return {
        "schema": "drycontroller.registry-auth.v2",
        "auths": {
            registry: {
                "auth": base64.b64encode(username.encode() + b":" + secret).decode()
            }
        },
        "credential": {
            "status": "active",
            "credential_id": "credential-1",
            "registry_host": registry,
            "username": username,
            "secret_fingerprint": hashlib.sha256(secret).hexdigest()[:16],
            "version": 1,
            "transaction_id": "1" * 32,
            "audit_id": 1,
            "updated_at": "2026-08-26T00:00:00Z",
        },
    }


def _write_guarded_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(mode=0o700)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o400)


def test_release_engines_publish_the_same_security_contract_version(engines):
    host, software = engines

    assert host.SHARED_SECURITY_CONTRACT_VERSION == 1
    assert software.SHARED_SECURITY_CONTRACT_VERSION == 1


@pytest.mark.parametrize(
    "pairs, expected",
    [
        ([("a", 1), ("b", 2)], {"a": 1, "b": 2}),
        ([("a", 1), ("a", 2)], None),
    ],
)
def test_strict_json_parity_including_duplicate_key_rejection(engines, pairs, expected):
    host, software = engines

    host_result = _accepted(lambda: host._strict_json_object(pairs))
    software_result = _accepted(lambda: software._strict_json_object(pairs))

    assert host_result == software_result
    assert host_result == (expected is not None, expected)


@pytest.mark.parametrize(
    "service, image_id, accepted",
    [
        ("api", "sha256:" + "a" * 64, True),
        ("API", "sha256:" + "a" * 64, False),
        ("api", "sha256:" + "g" * 64, False),
        ("../api", "sha256:" + "a" * 64, False),
    ],
)
def test_local_image_identity_acceptance_is_identical(
    engines, service, image_id, accepted
):
    host, software = engines

    host_result = _accepted(lambda: host._local_image_reference(service, image_id))
    software_result = _accepted(lambda: software.local_image_reference(service, image_id))

    assert host_result[0] is accepted
    assert software_result[0] is accepted
    if accepted:
        assert host_result[1] == software_result[1]


@pytest.mark.parametrize(
    "args, expected",
    [
        ([], False),
        (["/usr/bin/docker", "images"], False),
        (["/usr/bin/docker", "pull", "example/image"], True),
        (["/usr/local/bin/cosign", "verify", "example/image"], True),
    ],
)
def test_registry_network_command_classification_is_identical(engines, args, expected):
    host, software = engines

    assert host._requires_registry_auth(args) is expected
    assert software._requires_registry_auth(args) is expected


def test_registry_auth_document_parity_covers_negative_mutations(engines, tmp_path):
    host, software = engines
    base = _registry_document()
    cases = [
        base,
        {**base, "schema": "wrong"},
        {**base, "auths": {}},
        {
            **base,
            "credential": {
                **base["credential"],
                "secret_fingerprint": "wrong",
            },
        },
    ]

    for index, payload in enumerate(cases):
        path = tmp_path / f"case-{index}" / "registry-auth.json"
        _write_guarded_json(path, payload)
        host_result = _accepted(lambda path=path: host._read_registry_auth_document(path))
        software_result = _accepted(
            lambda path=path: software._read_registry_auth_document(path)
        )
        assert host_result == software_result

    mode_path = tmp_path / "mode" / "registry-auth.json"
    _write_guarded_json(mode_path, _registry_document())
    mode_path.chmod(0o600)
    assert _accepted(lambda: host._read_registry_auth_document(mode_path))[0] is False
    assert _accepted(lambda: software._read_registry_auth_document(mode_path))[0] is False


def test_identity_file_trust_boundary_acceptance_is_identical(engines, tmp_path):
    host, software = engines
    trust_root = tmp_path / "trust"
    trust_root.mkdir(mode=0o700)
    identity = trust_root / "runtime-tools.json"
    identity.write_text(
        json.dumps({"schema": "drycontroller.runtime-tools.v1", "executables": {}}),
        encoding="utf-8",
    )
    identity.chmod(0o400)
    host._TRUST_ROOT = trust_root
    software._TRUST_ROOT = trust_root

    expected = {"schema": "drycontroller.runtime-tools.v1", "executables": {}}
    assert host._read_identity_file(identity, expected["schema"]) == expected
    assert software._read_identity_file(identity, expected["schema"]) == expected

    identity.chmod(0o600)
    assert _accepted(lambda: host._read_identity_file(identity, expected["schema"]))[0] is False
    assert _accepted(lambda: software._read_identity_file(identity, expected["schema"]))[0] is False

    identity.chmod(0o400)
    os.chmod(trust_root, 0o777)
    assert _accepted(lambda: host._read_identity_file(identity, expected["schema"]))[0] is False
    assert _accepted(lambda: software._read_identity_file(identity, expected["schema"]))[0] is False
