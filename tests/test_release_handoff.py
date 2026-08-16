from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "scripts" / "ci" / "prepare-release-handoff.py"
HOST_FILES = (
    "docker-compose.yml",
    "infra/ota-helper/drycontroller_ota_helper.py",
    "infra/ota-helper/release_engine.py",
    "infra/ota-helper/ota-apply",
    "infra/ota-helper/initialize-active-release",
    "infra/ota-helper/start-active-release",
)
TOOLCHAIN = {
    "version": "v3.1.3",
    "linux_amd64_sha256": "sha256:4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
}


def _module():
    spec = importlib.util.spec_from_file_location("prepare_release_handoff", HANDOFF)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _public_key(path: Path) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1()).public_key()
    raw = key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    path.write_bytes(raw)
    return raw


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _fake_cosign(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _release_pair(tmp_path: Path, *, leak: dict[str, object] | None = None) -> dict[str, Path]:
    host_key_path = tmp_path / "host.pub"
    software_key_path = tmp_path / "software.pub"
    host_key = _public_key(host_key_path)
    software_key = _public_key(software_key_path)
    source_commit = "a" * 40
    service = "edge-api"
    compose = (ROOT / "docker-compose.yml").read_bytes()
    topology = {
        "compose_digest": _digest(compose),
        "service_inventory": [service],
        "health_contract": "drycontroller.edge-health.v1",
    }
    topology_digest = _digest(
        json.dumps(topology, sort_keys=True, separators=(",", ":")).encode()
    )
    software_manifest = {
        "schema": "drycontroller.software-release.v1",
        "release_id": "11111111-1111-4111-8111-111111111111",
        "host_release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "topology_digest": topology_digest,
        "sequence": 1,
        "version": "1.0.0",
        "source_commit": source_commit,
        "registry": "registry.example.test:5443",
        "signing_key_sha256": _digest(software_key),
        "cosign": TOOLCHAIN,
        "services": {
            service: "registry.example.test:5443/drycontroller/edge-api@sha256:" + "1" * 64
        },
        "image_ids": {service: "sha256:" + "2" * 64},
    }
    software_manifest_bytes = json.dumps(
        software_manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    host_files = []
    for relative in HOST_FILES:
        raw = (ROOT / relative).read_bytes()
        host_files.append(
            {
                "path": relative,
                "sha256": _digest(raw),
                "size": len(raw),
                "mode": 0o755
                if relative
                in {
                    "infra/ota-helper/ota-apply",
                    "infra/ota-helper/initialize-active-release",
                    "infra/ota-helper/start-active-release",
                }
                else 0o644,
            }
        )
    host_manifest = {
        "schema": "drycontroller.host-release.v1",
        "release_id": software_manifest["host_release_id"],
        "sequence": 1,
        "version": "1.0.0",
        "source_commit": source_commit,
        "registry": software_manifest["registry"],
        "signing_key_sha256": _digest(host_key),
        "cosign": TOOLCHAIN,
        "bundle": {
            "image": "registry.example.test:5443/drycontroller/host-bundle@sha256:" + "3" * 64,
            "image_id": "sha256:" + "4" * 64,
        },
        "topology": topology,
        "topology_digest": topology_digest,
        "software_release_id": software_manifest["release_id"],
        "software_manifest_digest": _digest(software_manifest_bytes),
        "files": host_files,
    }

    def release(manifest: dict[str, object]) -> dict[str, object]:
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        payload = {
            "id": manifest["release_id"],
            "sequence": manifest["sequence"],
            "version": manifest["version"],
            "source_commit": manifest["source_commit"],
            "status": "completed",
            "manifest": manifest,
            "manifest_digest": _digest(canonical),
            "manifest_signature": base64.b64encode(b"{}").decode(),
        }
        if leak:
            payload.update(leak)
        return payload

    host_release = tmp_path / "host-release.json"
    software_release = tmp_path / "software-release.json"
    host_release.write_text(json.dumps(release(host_manifest)), encoding="utf-8")
    software_release.write_text(json.dumps(release(software_manifest)), encoding="utf-8")
    return {
        "host_release": host_release,
        "software_release": software_release,
        "host_key": host_key_path,
        "software_key": software_key_path,
        "cosign": _fake_cosign(tmp_path / "cosign"),
    }


def test_public_key_handoff_is_exact_distinct_digest_bound_and_rescannable(tmp_path: Path):
    module = _module()
    host_key = tmp_path / "host.pub"
    software_key = tmp_path / "software.pub"
    host_raw = _public_key(host_key)
    software_raw = _public_key(software_key)
    output = tmp_path / "handoff"
    output.mkdir()

    module.prepare_keys(
        host_public_key=host_key,
        software_public_key=software_key,
        source_commit="a" * 40,
        output_dir=output,
    )

    assert {path.name for path in output.iterdir()} == {
        "host-release-cosign.pub",
        "software-release-cosign.pub",
        "SHA256SUMS",
        "inventory.json",
    }
    inventory = json.loads((output / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["host_signing_key_sha256"] == _digest(host_raw)
    assert inventory["software_signing_key_sha256"] == _digest(software_raw)
    assert inventory["host_signing_key_sha256"] != inventory["software_signing_key_sha256"]
    assert module.verify_package(output) == module.KEY_KIND


@pytest.mark.parametrize(
    "leak",
    (
        {"registry_password": "not-a-placeholder-credential"},
        {"registry_config": {"auth": "YWxpY2U6c3VwZXItc2VjcmV0"}},
        {
            "leaked_private_key": "-----BEGIN PRIVATE KEY-----\nnot-real\n-----END PRIVATE KEY-----"
        },
    ),
)
def test_completed_pair_handoff_public_scan_rejects_password_registry_auth_and_private_key(
    tmp_path: Path, leak: dict[str, object]
):
    module = _module()
    pair = _release_pair(tmp_path, leak=leak)
    output = tmp_path / "handoff"
    output.mkdir()

    with pytest.raises(ValueError, match="public-material scan"):
        module.prepare_pair(
            host_release=pair["host_release"],
            software_release=pair["software_release"],
            host_public_key=pair["host_key"],
            software_public_key=pair["software_key"],
            cosign=pair["cosign"],
            output_dir=output,
        )
    assert list(output.iterdir()) == []


def test_completed_pair_handoff_contains_only_public_pair_evidence(tmp_path: Path):
    module = _module()
    pair = _release_pair(tmp_path)
    output = tmp_path / "handoff"
    output.mkdir()
    module.prepare_pair(
        host_release=pair["host_release"],
        software_release=pair["software_release"],
        host_public_key=pair["host_key"],
        software_public_key=pair["software_key"],
        cosign=pair["cosign"],
        output_dir=output,
    )

    assert {path.name for path in output.iterdir()} == set(module._expected_files(module.PAIR_KIND))
    assert module.verify_package(output, cosign=pair["cosign"]) == module.PAIR_KIND
    inventory = json.loads((output / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["topology_digest"].startswith("sha256:")
    assert inventory["host_release"]["signing_key_sha256"] != inventory["software_release"][
        "signing_key_sha256"
    ]


def test_cloud_trust_preflight_checks_full_observed_fingerprints_before_readiness(
    tmp_path: Path, monkeypatch
):
    module = _module()
    host_key = tmp_path / "host.pub"
    software_key = tmp_path / "software.pub"
    host_raw = _public_key(host_key)
    software_raw = _public_key(software_key)
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _maximum):
            return b'{"success":true,"ready":true}'

    def urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    module.trust_preflight(
        host_public_key=host_key,
        software_public_key=software_key,
        expected_host_sha256=_digest(host_raw),
        expected_software_sha256=_digest(software_raw),
        acknowledgement="installed-and-verified",
        cloud_url="https://ops.example.com",
    )
    assert calls == [("https://ops.example.com/ready", 15)]

    with pytest.raises(ValueError, match="does not match"):
        module.trust_preflight(
            host_public_key=host_key,
            software_public_key=software_key,
            expected_host_sha256="sha256:" + "0" * 64,
            expected_software_sha256=_digest(software_raw),
            acknowledgement="installed-and-verified",
            cloud_url="https://ops.example.com",
        )
    assert len(calls) == 1


def test_generic_package_publish_uses_job_token_and_writes_inventory_last(
    tmp_path: Path, monkeypatch
):
    module = _module()
    host_key = tmp_path / "host.pub"
    software_key = tmp_path / "software.pub"
    _public_key(host_key)
    _public_key(software_key)
    output = tmp_path / "handoff"
    output.mkdir()
    module.prepare_keys(
        host_public_key=host_key,
        software_public_key=software_key,
        source_commit="a" * 40,
        output_dir=output,
    )
    requests = []

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    class Opener:
        open = staticmethod(urlopen)

    def build_opener(handler):
        assert isinstance(handler, module._RejectRedirects)
        return Opener()

    monkeypatch.setattr(module.urllib.request, "build_opener", build_opener)
    module.publish(output, version="keys-a" * 2, token="job-scoped-token", cosign=None)

    assert requests[-1][0].full_url.endswith("/inventory.json")
    assert all(
        request.headers["Authorization"] == "token job-scoped-token"
        and request.full_url.startswith(
            "https://git.example.com/api/packages/example-org/generic/"
            "drycontroller-edge-release-public-keys/"
        )
        for request, _timeout in requests
    )


def test_generic_package_publish_rejects_redirect_without_forwarding_job_token(
    tmp_path: Path, monkeypatch
):
    import io
    import urllib.request
    import urllib.response
    from email.message import Message

    module = _module()
    host_key = tmp_path / "host.pub"
    software_key = tmp_path / "software.pub"
    _public_key(host_key)
    _public_key(software_key)
    output = tmp_path / "handoff"
    output.mkdir()
    module.prepare_keys(
        host_public_key=host_key,
        software_public_key=software_key,
        source_commit="a" * 40,
        output_dir=output,
    )
    requests = []
    real_build_opener = urllib.request.build_opener

    class RedirectingHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            requests.append(request)
            if len(requests) > 1:
                raise AssertionError("redirect caused a second authenticated request")
            headers = Message()
            headers["Location"] = "https://attacker.invalid/collect"
            response = urllib.response.addinfourl(io.BytesIO(), headers, request.full_url, 302)
            response.msg = "Found"
            return response

    def build_opener(handler):
        assert isinstance(handler, module._RejectRedirects)
        return real_build_opener(handler, RedirectingHTTPSHandler())

    monkeypatch.setattr(module.urllib.request, "build_opener", build_opener)
    with pytest.raises(ValueError) as caught:
        module.publish(
            output,
            version="keys-redirect-test",
            token="job-scoped-token-must-not-leak",
            cosign=None,
        )

    assert len(requests) == 1
    assert requests[0].full_url.startswith("https://git.example.com/")
    assert requests[0].headers["Authorization"] == "token job-scoped-token-must-not-leak"
    error = str(caught.value)
    assert "job-scoped-token-must-not-leak" not in error
    assert "attacker.invalid" not in error


def test_release_workflows_pin_current_actions_and_separate_key_export_from_pair_creation():
    public_keys = (ROOT / ".gitea/workflows/edge-release-public-keys.yml").read_text(
        encoding="utf-8"
    )
    paired = (ROOT / ".gitea/workflows/edge-host-release.yml").read_text(encoding="utf-8")
    edge_tests = (ROOT / ".gitea/workflows/edge-tests.yml").read_text(encoding="utf-8")
    parity = (ROOT / ".gitea/workflows/schema-parity.yml").read_text(encoding="utf-8")
    current_checkout = (
        "https://git.example.com/actions/checkout@"
        "11bd71901bbe5b1630ceea73d27597364c9af683"
    )
    current_setup = (
        "https://git.example.com/actions/setup-python@"
        "a26af69be951a213d495a4c3e4e4022e16d87065"
    )

    assert current_checkout in public_keys and current_checkout in paired
    assert current_checkout in edge_tests and current_checkout in parity
    assert current_setup in edge_tests
    assert "example.com" not in edge_tests
    assert "example.com" not in parity
    assert "packages: write" in public_keys and "packages: write" in paired
    assert "contents: read" in public_keys and "contents: read" in paired
    assert "secrets.GITEA_TOKEN" in public_keys and "secrets.GITEA_TOKEN" in paired
    assert "upload-artifact" not in public_keys and "upload-artifact" not in paired
    assert "CLOUD_OPS_RELEASE_API" not in public_keys
    assert "build-host-release.sh" not in public_keys
    assert "expected_cloud_host_key_sha256" in paired
    assert "expected_cloud_software_key_sha256" in paired
    assert "installed-and-verified" in paired
    assert '"${GITHUB_RUN_ID}" "${GITHUB_RUN_ATTEMPT}"' in public_keys
    assert '"${GITHUB_RUN_ID}" "${GITHUB_RUN_ATTEMPT}"' in paired

    public_publish = public_keys.split("      - name: Publish public-key handoff", 1)[1]
    public_derive = public_keys.split("      - name: Derive and scan public release trust roots", 1)[1].split(
        "      - name: Publish public-key handoff", 1
    )[0]
    assert "COSIGN_PRIVATE_KEY" in public_derive
    assert "COSIGN_PRIVATE_KEY" not in public_publish
    assert "GITEA_TOKEN" not in public_derive

    pair_builder, pair_publish = paired.split(
        "      - name: Publish completed public release handoff", 1
    )
    assert "GITEA_TOKEN" not in pair_builder
    assert "GITEA_TOKEN" in pair_publish


def test_host_builder_runs_trust_preflight_before_any_cloud_release_row_is_created():
    script = (ROOT / "scripts/ci/build-host-release.sh").read_text(encoding="utf-8")
    preflight = script.index("prepare-release-handoff.py trust-preflight")
    create_host = script.index(' + "/api/v1/host-releases"')
    create_software = script.index("bash scripts/ci/build-software-release.sh")
    package = script.index("prepare-release-handoff.py pair")
    complete_host = script.index("HostRelease completed:")

    assert preflight < create_host < create_software < complete_host < package
    assert "EXPECTED_CLOUD_HOST_KEY_SHA256" in script
    assert "EXPECTED_CLOUD_SOFTWARE_KEY_SHA256" in script
    assert "CLOUD_TRUST_ACK" in script
    assert "GITEA_TOKEN" not in script
    scrub = script[complete_host:package]
    assert "unset HOST_COSIGN_PRIVATE_KEY HOST_COSIGN_PASSWORD" in scrub
    assert '"${DOCKER_CONFIG}/config.json"' in scrub


def test_public_key_export_never_prints_or_packages_private_material():
    script = (ROOT / "scripts/ci/export-release-public-keys.sh").read_text(encoding="utf-8")
    after_derivation = script.split("python3 scripts/ci/prepare-release-handoff.py keys", 1)[1]
    assert "set -x" not in script
    assert "echo" not in script
    assert "GITEA_TOKEN" not in script
    assert "PRIVATE_KEY" not in after_derivation
    assert "PASSWORD" not in after_derivation
    assert "--host-public-key" in after_derivation
    assert "--software-public-key" in after_derivation
