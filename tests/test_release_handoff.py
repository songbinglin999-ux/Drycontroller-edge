from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

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


def _release_pair(
    tmp_path: Path,
    *,
    leak: dict[str, object] | None = None,
    real_cosign: Path | None = None,
) -> dict[str, Path]:
    signing_config = tmp_path / "cosign-signing-config.json"
    if real_cosign is None:
        host_key_path = tmp_path / "host.pub"
        software_key_path = tmp_path / "software.pub"
        host_key = _public_key(host_key_path)
        software_key = _public_key(software_key_path)
        host_private_key = software_private_key = None
    else:
        password_env = {**os.environ, "COSIGN_PASSWORD": "test-only-password"}
        host_prefix = tmp_path / "host"
        software_prefix = tmp_path / "software"
        for prefix in (host_prefix, software_prefix):
            subprocess.run(
                [
                    str(real_cosign),
                    "generate-key-pair",
                    "--output-key-prefix",
                    str(prefix),
                ],
                check=True,
                capture_output=True,
                env=password_env,
            )
        subprocess.run(
            [
                str(real_cosign),
                "signing-config",
                "create",
                "--no-default-fulcio",
                "--no-default-oidc",
                "--no-default-rekor",
                "--no-default-tsa",
                "--out",
                str(signing_config),
            ],
            check=True,
            capture_output=True,
        )
        host_key_path = host_prefix.with_suffix(".pub")
        software_key_path = software_prefix.with_suffix(".pub")
        host_private_key = host_prefix.with_suffix(".key")
        software_private_key = software_prefix.with_suffix(".key")
        host_key = host_key_path.read_bytes()
        software_key = software_key_path.read_bytes()
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
            service: "registry.example.test:5443/drycontroller/edge-api@sha256:"
            + "1" * 64
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
            "image": "registry.example.test:5443/drycontroller/host-bundle@sha256:"
            + "3" * 64,
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
        signature = b"{}"
        if real_cosign is not None:
            is_host = manifest["schema"] == "drycontroller.host-release.v1"
            manifest_path = tmp_path / (
                "real-host-manifest.json" if is_host else "real-software-manifest.json"
            )
            bundle_path = tmp_path / (
                "real-host-bundle.json" if is_host else "real-software-bundle.json"
            )
            manifest_path.write_bytes(canonical)
            subprocess.run(
                [
                    str(real_cosign),
                    "sign-blob",
                    "--yes",
                    "--signing-config",
                    str(signing_config),
                    "--key",
                    str(host_private_key if is_host else software_private_key),
                    "--bundle",
                    str(bundle_path),
                    str(manifest_path),
                ],
                check=True,
                capture_output=True,
                env={**os.environ, "COSIGN_PASSWORD": "test-only-password"},
            )
            signature = bundle_path.read_bytes()
        payload = {
            "id": manifest["release_id"],
            "sequence": manifest["sequence"],
            "version": manifest["version"],
            "source_commit": manifest["source_commit"],
            "status": "completed",
            "manifest": manifest,
            "manifest_digest": _digest(canonical),
            "manifest_signature": base64.b64encode(signature).decode(),
        }
        if leak:
            payload.update(leak)
        return payload

    host_release = tmp_path / "host-release.json"
    software_release = tmp_path / "software-release.json"
    host_release.write_text(json.dumps(release(host_manifest)), encoding="utf-8")
    software_release.write_text(
        json.dumps(release(software_manifest)), encoding="utf-8"
    )
    return {
        "host_release": host_release,
        "software_release": software_release,
        "host_key": host_key_path,
        "software_key": software_key_path,
        "cosign": real_cosign or _fake_cosign(tmp_path / "cosign"),
    }


def test_public_key_handoff_is_exact_distinct_digest_bound_and_rescannable(
    tmp_path: Path,
):
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
    assert (
        inventory["host_signing_key_sha256"] != inventory["software_signing_key_sha256"]
    )
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

    assert {path.name for path in output.iterdir()} == set(
        module._expected_files(module.PAIR_KIND)
    )
    assert module.verify_package(output, cosign=pair["cosign"]) == module.PAIR_KIND
    inventory = json.loads((output / "inventory.json").read_text(encoding="utf-8"))
    assert inventory["topology_digest"].startswith("sha256:")
    assert (
        inventory["host_release"]["signing_key_sha256"]
        != inventory["software_release"]["signing_key_sha256"]
    )


def test_cloud_trust_preflight_requires_authenticated_observed_fingerprints(
    tmp_path: Path, monkeypatch
):
    module = _module()
    host_key = tmp_path / "host.pub"
    software_key = tmp_path / "software.pub"
    host_raw = _public_key(host_key)
    software_raw = _public_key(software_key)
    calls = []
    observed = {
        "schema": "drycontroller.release-trust-attestation.v1",
        "host_sha256": _digest(host_raw),
        "software_sha256": _digest(software_raw),
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _maximum):
            return json.dumps(observed).encode()

    def urlopen(request, timeout):
        calls.append((request.full_url, request.headers, timeout))
        return Response()

    class Opener:
        open = staticmethod(urlopen)

    monkeypatch.setattr(
        module.urllib.request, "build_opener", lambda _handler: Opener()
    )
    module.trust_preflight(
        host_public_key=host_key,
        software_public_key=software_key,
        cloud_url="https://ops.example.com",
        cloud_api_key="cloud-release-api-key",
    )
    assert calls == [
        (
            "https://ops.example.com/api/v1/release-trust/attestation",
            {"X-api-key": "cloud-release-api-key"},
            15,
        )
    ]

    observed["host_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="does not match"):
        module.trust_preflight(
            host_public_key=host_key,
            software_public_key=software_key,
            cloud_url="https://ops.example.com",
            cloud_api_key="cloud-release-api-key",
        )
    assert len(calls) == 2


def test_release_publish_creates_digest_tag_and_writes_inventory_last(
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
    created_release = None

    class Response:
        def __init__(self, status, payload=None):
            self.status = status
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _maximum):
            if self.payload is None:
                return b""
            return json.dumps(self.payload).encode()

    def urlopen(request, timeout):
        nonlocal created_release
        requests.append((request, timeout))
        if request.method == "GET" and "/releases/tags/" in request.full_url:
            import io
            import urllib.error

            raise urllib.error.HTTPError(
                request.full_url, 404, "not found", {}, io.BytesIO(b"{}")
            )
        if request.method == "POST" and request.full_url.endswith("/releases"):
            body = json.loads(request.data)
            created_release = {
                **body,
                "id": 17,
                "assets": [],
            }
            return Response(201, created_release)
        assert (
            request.method == "POST" and "/releases/17/assets?name=" in request.full_url
        )
        return Response(201)

    class Opener:
        open = staticmethod(urlopen)

    def build_opener(handler):
        assert isinstance(handler, module._RejectRedirects)
        return Opener()

    monkeypatch.setattr(module.urllib.request, "build_opener", build_opener)
    tag = module.publish(output, token="job-scoped-token", cosign=None)

    assert tag.startswith("drycontroller-release-public-keys-sha256-")
    assert created_release["tag_name"] == tag
    assert created_release["target_commitish"] == "a" * 40
    assert requests[-1][0].full_url.endswith("?name=inventory.json")
    assert all(
        request.headers["Authorization"] == "token job-scoped-token"
        and request.full_url.startswith(
            "https://git.example.com/api/v1/repos/example-org/Drycontroller-edge/releases"
        )
        for request, _timeout in requests
    )


def test_release_publish_resumes_exact_partial_assets_and_rejects_mismatch(
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
    kind = module.verify_package(output)
    tag, source_commit, payloads = module._release_identity(output, kind)
    uploads = []

    class Response:
        def __init__(self, status, raw=b""):
            self.status = status
            self.raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, maximum):
            return self.raw[:maximum]

    release = {
        "id": 19,
        "tag_name": tag,
        "target_commitish": source_commit,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": module.RELEASE_ARCHIVE,
                "size": len(payloads[module.RELEASE_ARCHIVE]),
                "browser_download_url": "https://git.example.com/download/archive",
            }
        ],
    }

    def urlopen(request, timeout):
        assert timeout == 60
        if "/releases/tags/" in request.full_url:
            return Response(200, json.dumps(release).encode())
        if request.full_url.endswith("/download/archive"):
            return Response(200, payloads[module.RELEASE_ARCHIVE])
        uploads.append(request.full_url)
        return Response(201)

    class Opener:
        open = staticmethod(urlopen)

    monkeypatch.setattr(
        module.urllib.request, "build_opener", lambda _handler: Opener()
    )
    assert module.publish(output, token="release-token", cosign=None) == tag
    assert [url.rsplit("=", 1)[-1] for url in uploads] == [
        module.RELEASE_CHECKSUM,
        "inventory.json",
    ]

    release["assets"][0]["size"] += 1
    with pytest.raises(ValueError, match="differs"):
        module.publish(output, token="release-token", cosign=None)


def test_release_publish_rejects_redirect_without_forwarding_job_token(
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
            response = urllib.response.addinfourl(
                io.BytesIO(), headers, request.full_url, 302
            )
            response.msg = "Found"
            return response

    def build_opener(handler):
        assert isinstance(handler, module._RejectRedirects)
        return real_build_opener(handler, RedirectingHTTPSHandler())

    monkeypatch.setattr(module.urllib.request, "build_opener", build_opener)
    with pytest.raises(ValueError) as caught:
        module.publish(
            output,
            token="job-scoped-token-must-not-leak",
            cosign=None,
        )

    assert len(requests) == 1
    assert requests[0].full_url.startswith("https://git.example.com/")
    assert (
        requests[0].headers["Authorization"] == "token job-scoped-token-must-not-leak"
    )
    error = str(caught.value)
    assert "job-scoped-token-must-not-leak" not in error
    assert "attacker.invalid" not in error


def test_release_workflows_pin_current_actions_and_separate_key_export_from_pair_creation():
    public_keys = (ROOT / ".gitea/workflows/edge-release-public-keys.yml").read_text(
        encoding="utf-8"
    )
    paired = (ROOT / ".gitea/workflows/edge-host-release.yml").read_text(
        encoding="utf-8"
    )
    recovery = (ROOT / ".gitea/workflows/edge-release-handoff-recovery.yml").read_text(
        encoding="utf-8"
    )
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
    assert current_checkout in recovery
    assert current_checkout in edge_tests and current_checkout in parity
    assert current_setup in edge_tests
    assert "example.com" not in edge_tests
    assert "example.com" not in parity
    assert "packages: write" not in public_keys + paired + recovery
    assert all(
        "releases: write" in workflow and "contents: read" in workflow
        for workflow in (public_keys, paired, recovery)
    )
    assert "secrets.GITEA_TOKEN" in public_keys and "secrets.GITEA_TOKEN" in paired
    assert "secrets.GITEA_TOKEN" in recovery
    assert "upload-artifact" not in public_keys and "upload-artifact" not in paired
    assert "CLOUD_OPS_RELEASE_API" not in public_keys
    assert "build-host-release.sh" not in public_keys
    assert "expected_cloud_host_key_sha256" not in paired
    assert "expected_cloud_software_key_sha256" not in paired
    assert "installed-and-verified" not in paired
    assert "GITHUB_RUN_ATTEMPT" not in public_keys + paired

    public_publish = public_keys.split("      - name: Publish public-key handoff", 1)[1]
    public_derive = public_keys.split(
        "      - name: Derive and scan public release trust roots", 1
    )[1].split("      - name: Publish public-key handoff", 1)[0]
    assert "COSIGN_PRIVATE_KEY" in public_derive
    assert "COSIGN_PRIVATE_KEY" not in public_publish
    assert "GITEA_TOKEN" not in public_derive

    pair_builder, pair_publish = paired.split(
        "      - name: Publish completed public release handoff", 1
    )
    assert "GITEA_TOKEN" not in pair_builder
    assert "GITEA_TOKEN" in pair_publish
    recovery_builder, recovery_publish = recovery.split(
        "      - name: Idempotently publish missing Release assets", 1
    )
    assert "recover-release-handoff.sh" in recovery_builder
    assert "build-host-release.sh" not in recovery
    assert "GITEA_TOKEN" not in recovery_builder
    assert "GITEA_TOKEN" in recovery_publish


def test_real_cosign_verifies_completed_pair_and_rejects_resigned_digest_tamper(
    tmp_path: Path,
):
    cosign = shutil.which("cosign")
    if cosign is None:
        pytest.skip("cosign is unavailable")
    module = _module()
    pair = _release_pair(tmp_path, real_cosign=Path(cosign))
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
    assert module.verify_package(output, cosign=pair["cosign"]) == module.PAIR_KIND

    host_release = json.loads(pair["host_release"].read_text(encoding="utf-8"))
    host_release["manifest"]["bundle"]["image_id"] = "sha256:" + "9" * 64
    host_release["manifest_digest"] = _digest(
        json.dumps(
            host_release["manifest"], sort_keys=True, separators=(",", ":")
        ).encode()
    )
    pair["host_release"].write_text(json.dumps(host_release), encoding="utf-8")
    tampered = tmp_path / "tampered"
    tampered.mkdir()
    with pytest.raises(ValueError, match="signature verification failed"):
        module.prepare_pair(
            host_release=pair["host_release"],
            software_release=pair["software_release"],
            host_public_key=pair["host_key"],
            software_public_key=pair["software_key"],
            cosign=pair["cosign"],
            output_dir=tampered,
        )
    assert list(tampered.iterdir()) == []


def test_recovery_fetches_exact_rows_after_preflight_without_release_side_effects():
    script = (ROOT / "scripts/ci/recover-release-handoff.sh").read_text(
        encoding="utf-8"
    )
    preflight = script.index("prepare-release-handoff.py trust-preflight")
    fetch = script.index('("host-releases", os.environ["HOST_RELEASE_ID"]')
    package = script.index("prepare-release-handoff.py pair")

    assert preflight < fetch < package
    assert (
        'payload.get("source_commit") != os.environ["RELEASE_SOURCE_COMMIT"]' in script
    )
    assert 'payload.get("status") != "completed"' in script
    assert 'method="GET"' in script
    assert 'method="POST"' not in script
    assert '/api/v1/host-releases"' not in script
    assert "build-software-release.sh" not in script
    assert "RejectRedirects" in script
    assert "unset HOST_COSIGN_PRIVATE_KEY HOST_COSIGN_PASSWORD" in script
    assert "unset CLOUD_OPS_RELEASE_API_KEY" in script


def test_host_builder_runs_trust_preflight_before_any_cloud_release_row_is_created():
    script = (ROOT / "scripts/ci/build-host-release.sh").read_text(encoding="utf-8")
    preflight = script.index("prepare-release-handoff.py trust-preflight")
    create_host = script.index(' + "/api/v1/host-releases"')
    create_software = script.index("bash scripts/ci/build-software-release.sh")
    package = script.index("prepare-release-handoff.py pair")
    complete_host = script.index("HostRelease completed:")

    assert preflight < create_host < create_software < complete_host < package
    assert "--cloud-api-key" in script
    assert "EXPECTED_CLOUD_HOST_KEY_SHA256" not in script
    assert "EXPECTED_CLOUD_SOFTWARE_KEY_SHA256" not in script
    assert "CLOUD_TRUST_ACK" not in script
    assert "GITEA_TOKEN" not in script
    scrub = script[complete_host:package]
    assert "unset HOST_COSIGN_PRIVATE_KEY HOST_COSIGN_PASSWORD" in scrub
    assert '"${DOCKER_CONFIG}/config.json"' in scrub


def test_public_key_export_never_prints_or_packages_private_material():
    script = (ROOT / "scripts/ci/export-release-public-keys.sh").read_text(
        encoding="utf-8"
    )
    after_derivation = script.split(
        "python3 scripts/ci/prepare-release-handoff.py keys", 1
    )[1]
    assert "set -x" not in script
    assert "echo" not in script
    assert "GITEA_TOKEN" not in script
    assert "PRIVATE_KEY" not in after_derivation
    assert "PASSWORD" not in after_derivation
    assert "--host-public-key" in after_derivation
    assert "--software-public-key" in after_derivation
