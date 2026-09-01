from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, utils


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "infra/ota-helper"
if str(HELPER) not in sys.path:
    sys.path.insert(0, str(HELPER))

import edge_app_release as release  # noqa: E402
from common.license_verifier import canonical_license_bytes  # noqa: E402


pytestmark = pytest.mark.skipif(
    os.environ.get("DRYCONTROLLER_RUN_EDGE_APP_DOCKER_E2E") != "1",
    reason="set DRYCONTROLLER_RUN_EDGE_APP_DOCKER_E2E=1 for the isolated Docker OTA E2E",
)


def _run(command: list[str], *, cwd: Path = ROOT, timeout: int = 1200) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout.strip()


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _manifest_signature(key: ec.EllipticCurvePrivateKey, manifest: dict) -> str:
    digest = hashlib.sha256(release.canonical_json_bytes(manifest)).digest()
    signature = key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    bundle = {
        "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
        "verificationMaterial": {
            "publicKey": {
                "hint": base64.b64encode(hashlib.sha256(public_der).digest()).decode()
            }
        },
        "messageSignature": {
            "messageDigest": {
                "algorithm": "SHA2_256",
                "digest": base64.b64encode(digest).decode(),
            },
            "signature": base64.b64encode(signature).decode(),
        },
    }
    return base64.b64encode(json.dumps(bundle, separators=(",", ":")).encode()).decode()


def _write_license(paths: release.ReleasePaths, identity: dict) -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    trust = {
        "algorithm": "Ed25519",
        "key_id": "license-e2e",
        "public_key_sha256": "sha256:" + hashlib.sha256(spki).hexdigest(),
        "public_key_spki_der_b64": base64.b64encode(spki).decode(),
        "schema": "drycontroller.license-trust.v1",
    }
    trust_raw = json.dumps(trust, sort_keys=True, separators=(",", ":")).encode()
    trust_path = paths.license_trust_policy.parent / "versions/license-e2e/license-trust.json"
    trust_path.parent.mkdir(parents=True)
    trust_path.write_bytes(trust_raw)
    paths.license_trust_policy.write_text(
        json.dumps(
            {
                "active": {
                    "canonical_sha256": "sha256:" + hashlib.sha256(trust_raw).hexdigest(),
                    "key_id": "license-e2e",
                },
                "previous": None,
                "schema": "drycontroller.license-trust-policy.v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    payload = {
        "capabilities": ["cloud-session", "edge-app-ota", "production-control"],
        "device_id": identity["device_id"],
        "expires_at": "2099-01-01T00:00:00Z",
        "identity_fingerprint": identity["identity_fingerprint"],
        "issued_at": "2026-01-01T00:00:00Z",
        "issuer": "drycontroller-cloud-ops",
        "key_id": "license-e2e",
        "license_id": "lic_" + "1" * 32,
        "line_id": identity["line_id"],
        "node_name": identity["node_name"],
        "previous_license_id": None,
        "product": "edge-app-v1",
        "schema": "drycontroller.license.v2",
        "signature_alg": "ed25519",
        "tenant_id": identity["tenant_id"],
    }
    payload["signature"] = base64.b64encode(
        private_key.sign(canonical_license_bytes(payload))
    ).decode()
    paths.license.parent.mkdir(parents=True)
    paths.license.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _wait_http(url: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise release.ReleaseError(f"health timeout for {url}: {last_error}")


def _wait_container_health(docker: str, services: tuple[str, ...], timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    observed: dict[str, str] = {}
    while time.monotonic() < deadline:
        observed = {
            service: _run(
                [docker, "inspect", service, "--format", "{{.State.Health.Status}}"]
            )
            for service in services
        }
        if all(status == "healthy" for status in observed.values()):
            return
        time.sleep(1)
    raise release.ReleaseError(f"container health timeout: {observed}")


def _write_compose(
    path: Path,
    project: str,
    active_images: dict[str, str],
    configs_volume: str,
) -> None:
    path.write_text(
        f"""name: {project}
services:
  postgres:
    image: postgres:15.18-alpine3.24
    environment:
      POSTGRES_USER: edge_user
      POSTGRES_PASSWORD: e2e-postgres-secret-20260830
      POSTGRES_DB: edge_db
    healthcheck:
      test: [\"CMD-SHELL\", \"pg_isready -U edge_user\"]
      interval: 2s
      timeout: 2s
      retries: 30
  influxdb:
    image: influxdb:2.9.1-alpine
    environment:
      DOCKER_INFLUXDB_INIT_MODE: setup
      DOCKER_INFLUXDB_INIT_USERNAME: admin
      DOCKER_INFLUXDB_INIT_PASSWORD: edge-password-123
      DOCKER_INFLUXDB_INIT_ORG: edge-org
      DOCKER_INFLUXDB_INIT_BUCKET: industrial-data
      DOCKER_INFLUXDB_INIT_ADMIN_TOKEN: edge-token
    healthcheck:
      test: [\"CMD\", \"influx\", \"ping\"]
      interval: 2s
      timeout: 2s
      retries: 30
  mosquitto:
    image: eclipse-mosquitto:2.0.22
    entrypoint: ["/bin/sh", "-c"]
    command:
      - |
        printf 'listener 1883\\nallow_anonymous true\\n' >/tmp/mosquitto.conf
        exec mosquitto -c /tmp/mosquitto.conf
    healthcheck:
      test: [\"CMD-SHELL\", \"mosquitto_sub -h localhost -t '$$SYS/broker/uptime' -C 1 -W 3 >/dev/null\"]
      interval: 2s
      timeout: 4s
      retries: 30
  edge-api:
    image: {json.dumps(active_images['edge-api'])}
    container_name: edge-api
    environment:
      POSTGRES_HOST: postgres
      POSTGRES_PORT: \"5432\"
      POSTGRES_USER: edge_user
      POSTGRES_PASSWORD: e2e-postgres-secret-20260830
      POSTGRES_DB: edge_db
      INFLUXDB_HOST: influxdb
      INFLUXDB_PORT: \"8086\"
      INFLUXDB_TOKEN: edge-token
      INFLUXDB_ORG: edge-org
      INFLUXDB_BUCKET: industrial-data
      MQTT_BROKER: mosquitto
      MQTT_PORT: \"1883\"
      MQTT_USER: e2e
      MQTT_PASSWORD: e2e
      CONTROL_SIGNATURE_SECRET: e2e-control-secret
      CONTROL_API_KEYS: '[\"test-control\"]'
      ADMIN_API_KEYS: '[\"test-admin\"]'
      CLOUD_SYNC_OUTBOX_ENABLED: \"false\"
    ports:
      - \"127.0.0.1:18080:8000\"
    depends_on:
      postgres: {{condition: service_healthy}}
      influxdb: {{condition: service_healthy}}
      mosquitto: {{condition: service_healthy}}
  edge-daq:
    image: {json.dumps(active_images['edge-daq'])}
    container_name: edge-daq
    environment:
      USE_SIMULATOR: \"true\"
      MQTT_BROKER: mosquitto
      MQTT_PORT: \"1883\"
      MQTT_USER: e2e
      MQTT_PASSWORD: e2e
      MQTT_ALLOW_INSECURE: \"true\"
      CONTROL_SIGNATURE_SECRET: e2e-control-secret
      INFLUXDB_HOST: influxdb
      INFLUXDB_PORT: \"8086\"
      INFLUXDB_TOKEN: edge-token
      INFLUXDB_ORG: edge-org
      INFLUXDB_BUCKET: industrial-data
      HEALTH_PORT: \"8080\"
    volumes:
      - e2e-configs:/app/configs:ro
    ports:
      - \"127.0.0.1:8082:8080\"
    depends_on:
      influxdb: {{condition: service_healthy}}
      mosquitto: {{condition: service_healthy}}
volumes:
  e2e-configs:
    external: true
    name: {json.dumps(configs_volume)}
"""
    )


def test_real_edge_api_and_edge_daq_ota_success_failure_and_manual_rollback(
    tmp_path: Path, monkeypatch
):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is unavailable")
    if any(
        _run([docker, "ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.Names}}"])
        for name in ("edge-api", "edge-daq")
    ):
        pytest.fail("edge-api or edge-daq container name is already in use")
    for port in (18080, 8082):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                pytest.fail(f"required E2E port is already in use: {port}")

    suffix = uuid.uuid4().hex[:10]
    project = release.COMPOSE_PROJECT_NAME
    resource_prefix = f"drycontroller-edge-e2e-{suffix}"
    registry_name = f"drycontroller-edge-registry-{suffix}"
    configs_volume = f"{resource_prefix}-configs"
    configs_loader = f"{resource_prefix}-configs-loader"
    registry_port = _free_port()
    registry = f"127.0.0.1:{registry_port}"
    built_tags: list[str] = []
    try:
        _run(
            [
                docker,
                "run",
                "-d",
                "--name",
                registry_name,
                "-p",
                f"127.0.0.1:{registry_port}:5000",
                "registry:2",
            ]
        )
        image_sets: dict[int, dict[str, str]] = {}
        for version in (1, 2, 3):
            image_sets[version] = {}
            for service in release.EDGE_SERVICES:
                local = f"drycontroller-e2e/{service}:{suffix}-{version}"
                remote = f"{registry}/drycontroller/{service}:{suffix}-{version}"
                _run(
                    [
                        docker,
                        "build",
                        "--platform",
                        "linux/amd64",
                        "--label",
                        f"drycontroller.e2e.version={version}",
                        "--file",
                        f"services/{service}/Dockerfile",
                        "--tag",
                        local,
                        ".",
                    ],
                    timeout=1200,
                )
                _run([docker, "tag", local, remote])
                _run([docker, "push", remote], timeout=300)
                digest = _run(
                    [
                        docker,
                        "image",
                        "inspect",
                        remote,
                        "--format",
                        "{{range .RepoDigests}}{{println .}}{{end}}",
                    ]
                ).splitlines()[0]
                assert digest.startswith(f"{registry}/drycontroller/{service}@sha256:")
                image_sets[version][service] = digest
                built_tags.extend((local, remote))

        _run([docker, "volume", "create", configs_volume])
        _run(
            [
                docker,
                "create",
                "--name",
                configs_loader,
                "--volume",
                f"{configs_volume}:/dest",
                "alpine:3.23",
                "true",
            ]
        )
        _run([docker, "cp", f"{ROOT / 'configs'}/.", f"{configs_loader}:/dest"])
        _run([docker, "rm", configs_loader])

        compose = tmp_path / "docker-compose.yml"
        _write_compose(compose, project, image_sets[1], configs_volume)
        state = tmp_path / "state"
        state.mkdir(mode=0o700)
        state.chmod(0o700)
        override = state / "edge-app-images.yml"
        override.write_text(
            "services:\n"
            f"  edge-api:\n    image: {json.dumps(image_sets[1]['edge-api'])}\n"
            f"  edge-daq:\n    image: {json.dumps(image_sets[1]['edge-daq'])}\n"
        )
        _run(
            [
                docker,
                "compose",
                "--project-name",
                release.COMPOSE_PROJECT_NAME,
                "--project-directory",
                str(tmp_path),
                "--file",
                str(compose),
                "--file",
                str(override),
                "up",
                "-d",
            ],
            timeout=300,
        )
        _wait_http("http://127.0.0.1:18080/ready")
        _wait_http("http://127.0.0.1:8082/ready")
        _wait_container_health(docker, release.EDGE_SERVICES)

        paths = release.ReleasePaths(
            lock=state / "release.lock",
            journal=state / "release-journal.json",
            request=state / "request.json",
            active=state / "active.json",
            previous=state / "previous.json",
            candidate=state / "candidate.json",
            history=state / "history",
            project=tmp_path,
            compose=compose,
            cosign_pubkey=tmp_path / "cosign.pub",
            device=tmp_path / "device.json",
            tpm_observation=tmp_path / "tpm-observation.json",
            license=tmp_path / "license/license.json",
            license_trust_policy=tmp_path / "license-trust/policy.json",
            cloud_config=tmp_path / "cloud.env",
            registry_auth=state / "registry-auth.json",
        )
        identity = {
            "device_id": "EDGE-E2E123456789",
            "tenant_id": "tenant-e2e",
            "line_id": "LINE-E2E",
            "node_name": "edge-e2e123456789",
            "identity_fingerprint": "1" * 64,
        }
        paths.device.write_text(json.dumps(identity))
        paths.tpm_observation.write_text(
            json.dumps(
                {
                    "schema": "drycontroller.tpm-identity-observation.v1",
                    "status": "ok",
                    "identity_fingerprint": identity["identity_fingerprint"],
                    "observed_at": dt.datetime.now(dt.UTC).isoformat(),
                }
            )
        )
        _write_license(paths, identity)
        manifest_key = ec.generate_private_key(ec.SECP256R1())
        paths.cosign_pubkey.write_bytes(
            manifest_key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

        manifests = {}
        for version in (1, 2, 3):
            manifests[version] = {
                "schema": "drycontroller.app-release.v2",
                "profile": "edge-app-v1",
                "release_id": str(uuid.uuid4()),
                "sequence": version,
                "source_commit": str(version) * 40,
                "deployment_digest": release.deployment_digest(compose),
                "images": image_sets[version],
            }

        image_signature_calls: list[list[str]] = []
        fail_next_runtime = False

        def docker_commands(args: list[str]) -> str:
            nonlocal fail_next_runtime
            if args[:2] == ["/usr/local/bin/cosign", "verify"]:
                image_signature_calls.append(args)
                return "verified"
            if args[0] == "/usr/bin/docker":
                actual = [docker, *args[1:]]
            elif args[0].endswith("docker-compose"):
                actual = [docker, "compose", *args[1:]]
            else:
                raise AssertionError(f"unexpected release command: {args}")
            output = _run(actual, cwd=tmp_path, timeout=300)
            if args[0].endswith("docker-compose") and fail_next_runtime:
                fail_next_runtime = False
                _run([docker, "stop", "edge-daq"], cwd=tmp_path, timeout=30)
            return output

        current_policy: dict = {}
        monkeypatch.setattr(release, "HEALTH_TIMEOUT_SECONDS", 90.0, raising=False)
        monkeypatch.setattr(release, "HEALTH_POLL_SECONDS", 0.2, raising=False)
        monkeypatch.setattr(release, "HEALTH_ATTEMPT_TIMEOUT_SECONDS", 1.0, raising=False)

        engine = release.ReleaseEngine(
            paths=paths,
            command_runner=docker_commands,
            policy_fetcher=lambda _operation_id: current_policy,
        )
        engine.initialize_active(
            {
                "manifest": manifests[1],
                "manifest_digest": release.canonical_manifest_digest(manifests[1]),
                "manifest_signature": _manifest_signature(manifest_key, manifests[1]),
            }
        )

        def apply(version: int) -> dict:
            nonlocal current_policy
            operation_id = f"{version:016x}{version:016x}"
            release.atomic_write(paths.request, {"operation_id": operation_id})
            manifest = manifests[version]
            current_policy = {
                "operation_id": operation_id,
                "release_id": manifest["release_id"],
                "manifest_digest": release.canonical_manifest_digest(manifest),
                "manifest_signature": _manifest_signature(manifest_key, manifest),
                "manifest": manifest,
                "tenant_id": identity["tenant_id"],
                "line_id": identity["line_id"],
                "device_id": identity["device_id"],
                "node_name": identity["node_name"],
                "node_id": version,
            }
            return engine.apply()

        assert apply(2)["status"] == "committed"
        for service in release.EDGE_SERVICES:
            assert _run(
                [docker, "inspect", service, "--format", "{{.Config.Image}}"]
            ) == image_sets[2][service]

        fail_next_runtime = True
        with pytest.raises(release.ReleaseError, match="health check failed"):
            apply(3)
        for service in release.EDGE_SERVICES:
            assert _run(
                [docker, "inspect", service, "--format", "{{.Config.Image}}"]
            ) == image_sets[2][service]

        assert engine.rollback(
            operation_id="00000000000000040000000000000004",
            expected_active_release_id=manifests[2]["release_id"],
            target_release_id=manifests[1]["release_id"],
        )["status"] == "rolled_back"
        for service in release.EDGE_SERVICES:
            assert _run(
                [docker, "inspect", service, "--format", "{{.Config.Image}}"]
            ) == image_sets[1][service]
        assert len(image_signature_calls) >= 6
        assert json.loads(paths.active.read_text())["manifest"]["release_id"] == manifests[1]["release_id"]
    finally:
        subprocess.run(
            [
                docker,
                "compose",
                "--project-name",
                release.COMPOSE_PROJECT_NAME,
                "--project-directory",
                str(tmp_path),
                "--file",
                str(tmp_path / "docker-compose.yml"),
                "--file",
                str(tmp_path / "state/edge-app-images.yml"),
                "down",
                "-v",
                "--remove-orphans",
            ],
            cwd=tmp_path,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=180,
        )
        subprocess.run(
            [docker, "rm", "-f", registry_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            [docker, "rm", "-f", configs_loader],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            [docker, "volume", "rm", "-f", configs_volume],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        for tag in built_tags:
            subprocess.run(
                [docker, "image", "rm", tag],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
