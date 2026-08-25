from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER_DIR = ROOT / "infra/ota-helper"
ALERTMANAGER_IMAGE = (
    "prom/alertmanager:v0.33.1@"
    "sha256:a89f8d4520954079275441eecdb71444328bd90633dd4eddfc33b9ed657f349b"
)
INFLUXDB_BASE_IMAGE = (
    "influxdb:2.9.1-alpine@"
    "sha256:818eb972f70c4b6b04b8b952b9ef54480221d92f2ee9cadf8f73c180efa69ac7"
)
MOSQUITTO_IMAGE = (
    "eclipse-mosquitto:2.0.22@"
    "sha256:54c90ecc78645241b6aa272b2a5ac8fc20b0eaf02cc4dd431c0cc8d2fd4447dd"
)
POSTGRES_BASE_IMAGE = (
    "postgres:15.18-alpine3.24@"
    "sha256:cae15a3b718f23497a60b7cafdcf205216d7949680972da0584db00fb68bf3e6"
)
PROMETHEUS_IMAGE = (
    "prom/prometheus:v3.13.0@"
    "sha256:0e698e35e50d1ddc2d11a4a55b089fe62eb71358a5c204dfafd21bdf8ffe04b8"
)
RUST_HOST_TOOLCHAIN_IMAGE = (
    "docker.io/library/rust:1.83.0-slim-bookworm@"
    "sha256:0f0dc99eb74e410d9420149cbb2b9be84abe00e8dbed6a4201a591a2c81844df"
)
RUST_MUSL_TARGET_IMAGE = (
    "docker.io/library/rust:1.83.0-alpine@"
    "sha256:4bebaa3d13e2933c751eaf4a2e38d0530807572ea101d9c61f20f51eb7174ee4"
)


def test_alertmanager_image_is_pinned_consistently_for_runtime_and_release():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    release_builder = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")

    assert f"image: {ALERTMANAGER_IMAGE}" in compose
    assert f'alertmanager) printf \'%s\\n\' "{ALERTMANAGER_IMAGE}" ;;' in release_builder
    assert "prom/alertmanager:v0.27.0" not in compose
    assert "prom/alertmanager:v0.27.0" not in release_builder


def test_external_release_images_are_exact_and_vulnerable_binaries_are_rebuilt():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    release_builder = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")
    influxdb = (ROOT / "infra/release-images/influxdb/Dockerfile").read_text(encoding="utf-8")
    postgres = (ROOT / "infra/release-images/postgres/Dockerfile").read_text(encoding="utf-8")
    validation = (ROOT / "scripts/ci/rb8-a1-validation-control.yml").read_text(
        encoding="utf-8"
    )

    for image in (INFLUXDB_BASE_IMAGE, MOSQUITTO_IMAGE, POSTGRES_BASE_IMAGE, PROMETHEUS_IMAGE):
        assert f"image: {image}" in compose

    assert f'mosquitto|mosquitto-init) printf \'%s\\n\' "{MOSQUITTO_IMAGE}" ;;' in release_builder
    assert f'prometheus) printf \'%s\\n\' "{PROMETHEUS_IMAGE}" ;;' in release_builder
    assert 'influxdb) printf \'%s\\n\' "infra/release-images/influxdb/Dockerfile" ;;' in release_builder
    assert 'postgres) printf \'%s\\n\' "infra/release-images/postgres/Dockerfile" ;;' in release_builder
    assert "influxdb) printf" not in release_builder.split("source_image_for()", 1)[1].split("}", 1)[0]
    assert "postgres) printf" not in release_builder.split("source_image_for()", 1)[1].split("}", 1)[0]

    assert f"FROM {INFLUXDB_BASE_IMAGE}" in influxdb
    assert f"FROM {RUST_HOST_TOOLCHAIN_IMAGE} AS rust-host-toolchain" in influxdb
    assert f"FROM {RUST_MUSL_TARGET_IMAGE} AS rust-musl-target" in influxdb
    assert (
        "COPY --from=rust-host-toolchain "
        "/usr/local/rustup/toolchains/1.83.0-x86_64-unknown-linux-gnu /opt/rust-toolchain"
        in influxdb
    )
    assert (
        "COPY --from=rust-musl-target "
        "/usr/local/rustup/toolchains/1.83.0-x86_64-unknown-linux-musl/lib/rustlib/"
        "x86_64-unknown-linux-musl /opt/rust-toolchain/lib/rustlib/x86_64-unknown-linux-musl"
        in influxdb
    )
    assert "COPY --from=rust-host-toolchain /usr/local/cargo" not in influxdb
    assert 'PATH="${RUST_TOOLCHAIN_ROOT}/bin:${PATH}"' in influxdb
    assert 'test "$(rustc --version)" = "rustc 1.83.0 (90b35a623 2024-11-26)"' in influxdb
    assert 'test "$(cargo --version)" = "cargo 1.83.0 (5ffbef321 2024-10-29)"' in influxdb
    assert (
        'RUSTLIB_PATH="${RUST_TOOLCHAIN_ROOT}/lib/rustlib/'
        'x86_64-unknown-linux-musl/lib/self-contained"'
        in influxdb
    )
    assert 'test -f "${RUSTLIB_PATH}/libunwind.a"' in influxdb
    assert "find_rustlib_path" not in influxdb
    assert influxdb.startswith(
        "# syntax=docker/dockerfile:1.7@"
        "sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e\n"
    )
    assert "d4fa1941fd4adb29a556e774de426dfbab72f346" in influxdb
    assert "0d6d47516cd385285e878dac92e04b534667788449cf42e7bf6a96bb050c368f" in influxdb
    ui_source = "https://github.com/influxdata/ui/releases/download/OSS-v2.9.0/build.tar.gz"
    assert "COPY infra/release-images/influxdb/ui-oss-v2.9.0.tar.gz" in influxdb
    assert "9cb2df818f1f5badc23e051f51eef7e8b143540233947606eadad309f395bb0e  /tmp/influxdb-ui.tar.gz" in influxdb
    assert 'test "$(wc -c < /tmp/influxdb-ui.tar.gz)" -eq 19844077' in influxdb
    assert ui_source not in influxdb
    assert "ghproxy.net" not in influxdb
    assert "github.com/buger/jsonparser@v1.1.2" in influxdb
    assert "github.com/go-jose/go-jose/v4@v4.1.4" in influxdb
    assert "go.opentelemetry.io/otel/sdk@v1.43.0" in influxdb
    assert "d19700fce30d9aa15c96cefa0eb1db44330fd4526930cf5dedd860fe6b3b554b  go.mod" in influxdb
    assert "50258d1b217e18c5154c40fc0aaf79238e11872cbf31a0830214d79e891fce61  go.sum" in influxdb
    assert "8cdf40161d7662e942582086853fca948fc1a842" in influxdb
    assert "2026-04-22T18:55:12Z" in influxdb
    assert "e3c6663d78c62f69579d8f9afd00453dc3d01c29" in influxdb
    assert f"FROM {POSTGRES_BASE_IMAGE}" in postgres
    assert postgres.startswith(
        "# syntax=docker/dockerfile:1.7@"
        "sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e\n"
    )
    assert "6456aaa0f3c854d199d0f037f068eb97515b7513" in postgres
    assert "latest" not in influxdb
    assert "latest" not in postgres
    assert "type=cache" not in influxdb
    assert "type=cache" not in postgres
    assert "&& dasel version" in influxdb
    assert "dasel --version" not in influxdb
    assert '--entrypoint dasel "${INFLUX_TEST_IMAGE}" version' in validation
    assert '--entrypoint dasel "${INFLUX_TEST_IMAGE}" --version' not in validation
    assert "GOPROXY=https://goproxy.cn|https://proxy.golang.org|direct" in influxdb
    assert "GOPROXY=https://goproxy.cn|https://proxy.golang.org|direct" in postgres
    assert "GOSUMDB=sum.golang.org" in influxdb
    assert "GOSUMDB=sum.golang.org" in postgres
    assert "GOSUMDB=off" not in influxdb
    assert "GOSUMDB=off" not in postgres


def test_influxdb_rust_dependencies_are_checksum_pinned_and_offline():
    dockerfile = (ROOT / "infra/release-images/influxdb/Dockerfile").read_text(encoding="utf-8")
    archive = ROOT / "infra/release-images/influxdb/cargo-vendor-v0.200.0.tar.gz"
    provenance = json.loads(
        (ROOT / "infra/release-images/influxdb/cargo-vendor-v0.200.0.provenance.json").read_text(
            encoding="utf-8"
        )
    )

    expected_archive_sha256 = "deabeee4a940f709b56556632c1fc56abdcd21a6f6b4c54f15308aa182ce8a4f"
    with archive.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == expected_archive_sha256

    assert provenance["schema_version"] == 1
    assert provenance["vendor_archive"] == {
        "path": archive.name,
        "registry_crate_count": 188,
        "sha256": expected_archive_sha256,
    }
    assert provenance["flux_source"] == {
        "cargo_lock_sha256": "04184b5eaaf27711adbcef4023bf89183c6f5c249701bc10e94c6dea82230175",
        "go_module": "github.com/influxdata/flux",
        "go_sum": "h1:r1KAmr0F0eZ3UV6p00HsJYRHbfCyF1ARA9lAwxD2VeM=",
        "origin_commit": "b03b6bce7c27b8accc0cd00b12e32439a8fe19c3",
        "origin_ref": "refs/tags/v0.200.0",
        "origin_url": "https://github.com/influxdata/flux",
        "version": "v0.200.0",
    }
    crates = provenance["registry_crates"]
    assert len(crates) == 188
    assert all(
        crate["source"] == "registry+https://github.com/rust-lang/crates.io-index"
        and re.fullmatch(r"[0-9a-f]{64}", crate["checksum"])
        for crate in crates
    )
    assert {
        "checksum": "2874a2af47a2325c2001a6e6fad9b16a53b802102b528163885171cf92b15976",
        "name": "libc",
        "source": "registry+https://github.com/rust-lang/crates.io-index",
        "version": "0.2.177",
    } in crates

    assert "COPY infra/release-images/influxdb/cargo-vendor-v0.200.0.tar.gz" in dockerfile
    assert expected_archive_sha256 in dockerfile
    assert "ENV CARGO_NET_OFFLINE=true" in dockerfile
    assert 'replace-with = "vendored-sources"' in dockerfile
    assert 'directory = "/opt/cargo-vendor"' in dockerfile
    assert provenance["flux_source"]["cargo_lock_sha256"] in dockerfile
    assert "static.crates.io" not in dockerfile
    assert "CARGO_NET_OFFLINE=false" not in dockerfile


def test_influxdb_ui_asset_is_checksum_pinned_and_offline():
    dockerfile = (ROOT / "infra/release-images/influxdb/Dockerfile").read_text(encoding="utf-8")
    archive = ROOT / "infra/release-images/influxdb/ui-oss-v2.9.0.tar.gz"
    provenance = json.loads(
        (ROOT / "infra/release-images/influxdb/ui-oss-v2.9.0.provenance.json").read_text(
            encoding="utf-8"
        )
    )
    expected_sha256 = "9cb2df818f1f5badc23e051f51eef7e8b143540233947606eadad309f395bb0e"

    assert archive.stat().st_size == 19844077
    with archive.open("rb") as handle:
        assert hashlib.file_digest(handle, "sha256").hexdigest() == expected_sha256
    assert provenance == {
        "schema_version": 1,
        "source_release": "OSS-v2.9.0",
        "source_url": "https://github.com/influxdata/ui/releases/download/OSS-v2.9.0/build.tar.gz",
        "archive": {
            "path": archive.name,
            "size_bytes": 19844077,
            "sha256": expected_sha256,
        },
    }
    assert "COPY infra/release-images/influxdb/ui-oss-v2.9.0.tar.gz" in dockerfile
    assert expected_sha256 in dockerfile
    assert provenance["source_url"] not in dockerfile
    assert "ghproxy.net" not in dockerfile


def test_release_builder_is_complete_and_completion_is_last():
    script = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")
    expected = {
        "ai-ctrl-optimizer",
        "ai-softsensor",
        "alertmanager",
        "edge-api",
        "edge-daq",
        "edge-monitor",
        "influxdb",
        "license-service",
        "mosquitto",
        "mosquitto-init",
        "postgres",
        "prometheus",
    }
    service_block = re.search(r"^SERVICES=\((.*?)^\)", script, flags=re.MULTILINE | re.DOTALL)
    assert service_block is not None
    assert set(re.findall(r'"([a-z0-9-]+)"', service_block.group(1))) == expected
    assert "release_step_resolve_predecessor" in script
    assert "release_step_resolve_changed_services" in script
    assert "release_step_load_inherited_images" in script
    assert "release_step_scan \"${inherited_ref}\"" in script
    assert "release_step_verify_image \"${inherited_ref}\"" in script
    assert "inputs.services" not in script
    assert '"host_release_id": os.environ["HOST_RELEASE_ID"]' in script
    assert '"topology_digest": os.environ["TOPOLOGY_DIGEST"]' in script
    assert "bash scripts/ci/verify-cosign-toolchain.sh" in script
    assert "release_step_resolve_portable_image_id" in script
    assert "docker_archive_config_digest" in script
    assert "docker image inspect --format '{{.Id}}'" not in script

    order = [
        "release_step_build_or_mirror",
        "release_step_scan",
        "release_step_push",
        "release_step_resolve_digest",
        "release_step_sign_image",
        "release_step_verify_image",
        "release_step_sign_manifest",
        "release_step_verify_manifest",
        "release_step_complete_cloud",
    ]
    positions = [script.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_release_cosign_toolchain_gate_checks_platform_version_and_checksum(tmp_path: Path):
    verifier = ROOT / "scripts" / "ci" / "verify-cosign-toolchain.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uname").write_text(
        '#!/bin/sh\n[ "$1" = "-s" ] && echo "${FAKE_UNAME_S}" || echo "${FAKE_UNAME_M}"\n',
        encoding="utf-8",
    )
    (fake_bin / "cosign").write_text(
        '#!/bin/sh\nprintf "GitVersion: %s\\nPlatform: %s\\n" "${FAKE_COSIGN_VERSION}" "${FAKE_COSIGN_PLATFORM}"\n',
        encoding="utf-8",
    )
    (fake_bin / "sha256sum").write_text(
        '#!/bin/sh\nprintf "%s  %s\\n" "${FAKE_COSIGN_SHA256}" "$1"\n',
        encoding="utf-8",
    )
    for path in fake_bin.iterdir():
        path.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UNAME_S": "Linux",
        "FAKE_UNAME_M": "x86_64",
        "FAKE_COSIGN_VERSION": "v3.1.3",
        "FAKE_COSIGN_PLATFORM": "linux/amd64",
        "FAKE_COSIGN_SHA256": "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
    }

    accepted = subprocess.run(["bash", str(verifier)], env=environment, check=False)
    assert accepted.returncode == 0

    rejected_cases = (
        ({"FAKE_COSIGN_SHA256": "0" * 64}, "checksum is not approved"),
        ({"FAKE_COSIGN_VERSION": "v3.1.2"}, "version is not approved"),
        ({"FAKE_COSIGN_PLATFORM": "linux/arm64"}, "platform is not approved"),
        ({"FAKE_UNAME_S": "Darwin"}, "builder must be Linux"),
        ({"FAKE_UNAME_M": "aarch64"}, "builder must be amd64"),
    )
    for overrides, expected_error in rejected_cases:
        rejected = subprocess.run(
            ["bash", str(verifier)],
            env={**environment, **overrides},
            check=False,
            capture_output=True,
            text=True,
        )
        assert rejected.returncode == 1
        assert expected_error in rejected.stderr


def test_release_workflow_uses_the_fixed_builder_after_tests():
    workflow = (ROOT / ".gitea/workflows/edge-software-release.yml").read_text(encoding="utf-8")
    assert "needs: test" in workflow
    assert "bash scripts/ci/build-software-release.sh" in workflow
    assert "HOST_RELEASE_ID: ${{ vars.HOST_RELEASE_ID }}" in workflow
    assert "TOPOLOGY_DIGEST: ${{ vars.TOPOLOGY_DIGEST }}" in workflow
    assert "services:" not in workflow.split("workflow_dispatch:", 1)[1].split("jobs:", 1)[0]


def test_edge_test_workflows_install_bsdtar_before_the_full_suite():
    for relative in (
        ".gitea/workflows/edge-tests.yml",
        ".gitea/workflows/edge-host-release.yml",
        ".gitea/workflows/edge-software-release.yml",
    ):
        workflow = (ROOT / relative).read_text(encoding="utf-8")
        before_tests = workflow.split("bash scripts/test-all.sh", 1)[0]
        assert "apt-get install -y --no-install-recommends libarchive-tools" in before_tests
        assert "command -v bsdtar" in before_tests


def test_release_workflow_pins_actions_and_scopes_release_secrets_to_the_builder_step():
    workflow = (ROOT / ".gitea/workflows/edge-software-release.yml").read_text(encoding="utf-8")
    release_job = workflow.split("\n  release:\n", 1)[1]
    before_builder = release_job.split("      - name: Build and complete signed SoftwareRelease", 1)[0]
    builder = release_job.split("      - name: Build and complete signed SoftwareRelease", 1)[1]

    assert "actions/checkout@v4" not in workflow
    assert "actions/setup-python@v5" not in workflow
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in workflow
    for secret in (
        "HARBOR_PASSWORD",
        "COSIGN_PRIVATE_KEY",
        "COSIGN_PASSWORD",
        "CLOUD_OPS_RELEASE_API_KEY",
    ):
        assert secret not in before_builder
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in builder


def test_host_release_workflow_is_manual_serial_and_runs_the_fixed_builder_after_tests():
    workflow = (ROOT / ".gitea/workflows/edge-host-release.yml").read_text(encoding="utf-8")
    trigger = workflow.split("\nconcurrency:\n", 1)[0]

    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "group: edge-release-transaction" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "needs: test" in workflow
    assert "bash scripts/ci/build-host-release.sh" in workflow
    assert "actions/checkout@v4" not in workflow
    assert "actions/setup-python@v5" not in workflow
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in workflow
    resolve_step = workflow.split("      - name: Resolve/bind exact candidate identity", 1)[1].split(
        "      - name: Prime pinned Trivy database", 1
    )[0]
    run_script = resolve_step.split("        run: |", 1)[1]
    assert "INPUT_HOST_VERSION: ${{ inputs.host_version }}" in resolve_step
    assert "INPUT_SOFTWARE_VERSION: ${{ inputs.software_version }}" in resolve_step
    assert "${{ inputs." not in run_script
    assert (
        'test "${CONTROLLER_REF}" = refs/heads/codex/rb8-formal-release-control'
        in run_script
    )
    assert 'test "$(git rev-parse HEAD)" = "${CANDIDATE_SHA}"' in run_script


def test_host_release_workflow_scopes_both_signing_keys_to_the_builder_step():
    workflow = (ROOT / ".gitea/workflows/edge-host-release.yml").read_text(encoding="utf-8")
    release_job = workflow.split("\n  release:\n", 1)[1]
    before_builder, builder = release_job.split("      - name: Build and complete paired HostRelease", 1)

    for secret in (
        "HARBOR_PASSWORD",
        "HOST_COSIGN_PRIVATE_KEY",
        "HOST_COSIGN_PASSWORD",
        "SOFTWARE_COSIGN_PRIVATE_KEY",
        "SOFTWARE_COSIGN_PASSWORD",
        "CLOUD_OPS_RELEASE_API_KEY",
    ):
        assert secret not in before_builder
        expected = secret.removeprefix("SOFTWARE_") if secret.startswith("SOFTWARE_COSIGN") else secret
        assert f"{secret}: ${{{{ secrets.{expected} }}}}" in builder


def test_release_builder_only_rebuilds_directly_changed_services(tmp_path: Path):
    script = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")
    match = re.search(
        r"release_step_resolve_changed_services\(\) \{.*?<<'PY'\n(.*?)\nPY\n\}",
        script,
        flags=re.DOTALL,
    )
    assert match is not None
    resolver = match.group(1)
    services = (
        "ai-ctrl-optimizer",
        "ai-softsensor",
        "alertmanager",
        "edge-api",
        "edge-daq",
        "edge-monitor",
        "influxdb",
        "license-service",
        "mosquitto",
        "mosquitto-init",
        "postgres",
        "prometheus",
    )

    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "release-test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Release Test"], cwd=repository, check=True)
    (repository / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
    initial = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

    service_dir = repository / "services" / "edge-api"
    service_dir.mkdir(parents=True)
    (service_dir / "app.py").write_text("changed = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "services/edge-api/app.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "change service"], cwd=repository, check=True)
    service_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()

    predecessor_path = tmp_path / "predecessor.json"
    output_path = tmp_path / "changed.txt"

    def resolve(previous_commit: str, current_commit: str) -> list[str]:
        import sys

        sys.path.insert(0, str(HELPER_DIR))
        from release_engine import COSIGN_TOOLCHAIN

        manifest = {
            "schema": "drycontroller.software-release.v1",
            "release_id": "11111111-1111-4111-8111-111111111111",
            "host_release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "topology_digest": "sha256:" + "c" * 64,
            "sequence": 1,
            "version": "1.0.0",
            "source_commit": previous_commit,
            "registry": "registry.example.com",
            "signing_key_sha256": "sha256:" + "a" * 64,
            "cosign": COSIGN_TOOLCHAIN,
            "services": {
                service: f"registry.example.com/drycontroller/{service}@sha256:{index:064x}"
                for index, service in enumerate(services, start=1)
            },
            "image_ids": {
                service: f"sha256:{index:064x}"
                for index, service in enumerate(services, start=1)
            },
        }
        predecessor_path.write_text(
            json.dumps(
                {
                    "id": manifest["release_id"],
                    "sequence": 1,
                    "version": "1.0.0",
                    "source_commit": previous_commit,
                    "status": "completed",
                    "manifest": manifest,
                    "manifest_digest": "sha256:" + "b" * 64,
                    "manifest_signature": "c2ln",
                }
            ),
            encoding="utf-8",
        )
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "infra/ota-helper"),
            "HARBOR_HOST": "registry.example.com",
            "SIGNING_KEY_SHA256": "sha256:" + "a" * 64,
            "RELEASE_SOURCE_COMMIT": current_commit,
            "HOST_RELEASE_ID": manifest["host_release_id"],
            "TOPOLOGY_DIGEST": manifest["topology_digest"],
            "RELEASE_SERVICE_INVENTORY": "\n".join(services),
        }
        subprocess.run(
            ["python3", "-c", resolver, str(predecessor_path), str(output_path)],
            cwd=repository,
            env=environment,
            check=True,
        )
        return output_path.read_text(encoding="utf-8").splitlines()

    assert resolve(initial, service_commit) == ["edge-api"]

    docs_dir = repository / "docs"
    docs_dir.mkdir()
    (docs_dir / "release.md").write_text("docs only\n", encoding="utf-8")
    subprocess.run(["git", "add", "docs/release.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "docs only"], cwd=repository, check=True)
    docs_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert resolve(service_commit, docs_commit) == []

    common_dir = repository / "common"
    common_dir.mkdir()
    (common_dir / "shared.py").write_text("shared = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "common/shared.py"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "shared change"], cwd=repository, check=True)
    shared_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert set(resolve(docs_commit, shared_commit)) == set(services)


def test_new_host_binding_requires_completed_pair_and_full_rebuild(tmp_path: Path):
    script = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")
    assert 'fetch("host-releases")' in script
    assert 'predecessor["paired_host_release"]' in script
    match = re.search(
        r"release_step_load_inherited_images\(\) \{.*?<<'PY'\n(.*?)\nPY\n",
        script,
        flags=re.DOTALL,
    )
    assert match is not None
    loader = match.group(1)

    runner_dir = ROOT / "infra/host-release-runner"
    sys.path.insert(0, str(runner_dir))
    from host_release_engine import (  # noqa: PLC0415
        COSIGN_TOOLCHAIN,
        HOST_HEALTH_CONTRACT,
        canonical_digest,
        canonical_topology_digest,
    )

    services = (
        "ai-ctrl-optimizer",
        "ai-softsensor",
        "alertmanager",
        "edge-api",
        "edge-daq",
        "edge-monitor",
        "influxdb",
        "license-service",
        "mosquitto",
        "mosquitto-init",
        "postgres",
        "prometheus",
    )
    old_host_id = "11111111-1111-4111-8111-111111111111"
    new_host_id = "22222222-2222-4222-8222-222222222222"
    software_id = "33333333-3333-4333-8333-333333333333"
    source_commit = "a" * 40
    registry = "registry.example.com"
    compose = b"services:\n  edge-api:\n    image: edge-api:3\n"
    topology = {
        "compose_digest": "sha256:" + hashlib.sha256(compose).hexdigest(),
        "service_inventory": sorted(services),
        "health_contract": HOST_HEALTH_CONTRACT,
    }
    topology_digest = canonical_topology_digest(topology)
    software_signing_key = "sha256:" + "b" * 64
    software_manifest = {
        "schema": "drycontroller.software-release.v1",
        "release_id": software_id,
        "host_release_id": old_host_id,
        "topology_digest": topology_digest,
        "sequence": 3,
        "version": "1.0.0-rb8.3",
        "source_commit": source_commit,
        "registry": registry,
        "signing_key_sha256": software_signing_key,
        "cosign": COSIGN_TOOLCHAIN,
        "services": {
            service: f"{registry}/drycontroller/{service}@sha256:{index:064x}"
            for index, service in enumerate(services, start=1)
        },
        "image_ids": {
            service: f"sha256:{index + 100:064x}"
            for index, service in enumerate(services, start=1)
        },
    }
    helper_files = {
        "infra/ota-helper/drycontroller_ota_helper.py": (b"helper\n", 0o644),
        "infra/ota-helper/release_engine.py": (b"engine\n", 0o644),
        "infra/ota-helper/ota-apply": (b"#!/usr/bin/python3\n", 0o755),
        "infra/ota-helper/initialize-active-release": (b"#!/usr/bin/python3\n", 0o755),
        "infra/ota-helper/start-active-release": (b"#!/usr/bin/python3\n", 0o755),
    }
    files = {"docker-compose.yml": (compose, 0o644), **helper_files}
    host_manifest = {
        "schema": "drycontroller.host-release.v1",
        "release_id": old_host_id,
        "sequence": 3,
        "version": "1.0.0-rb8.3",
        "source_commit": source_commit,
        "registry": registry,
        "signing_key_sha256": "sha256:" + "c" * 64,
        "cosign": COSIGN_TOOLCHAIN,
        "bundle": {
            "image": f"{registry}/drycontroller/host-bundle@sha256:{999:064x}",
            "image_id": "sha256:" + "d" * 64,
        },
        "topology": topology,
        "topology_digest": topology_digest,
        "software_release_id": software_id,
        "software_manifest_digest": canonical_digest(software_manifest),
        "files": [
            {
                "path": path,
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "mode": mode,
            }
            for path, (data, mode) in sorted(files.items())
        ],
    }
    predecessor = {
        "id": software_id,
        "sequence": 3,
        "version": "1.0.0-rb8.3",
        "source_commit": source_commit,
        "status": "completed",
        "manifest": software_manifest,
        "manifest_digest": canonical_digest(software_manifest),
        "manifest_signature": "c2ln",
        "paired_host_release": {
            "id": old_host_id,
            "sequence": 3,
            "version": "1.0.0-rb8.3",
            "source_commit": source_commit,
            "status": "completed",
            "manifest": host_manifest,
            "manifest_digest": canonical_digest(host_manifest),
            "manifest_signature": "aG9zdC1zaWc=",
            "software_release_id": software_id,
        },
    }
    predecessor_path = tmp_path / "predecessor.json"
    predecessor_manifest_path = tmp_path / "predecessor-manifest.json"
    predecessor_signature_path = tmp_path / "predecessor-manifest.sigstore.json"
    changed_services_path = tmp_path / "changed-services.txt"
    inherited_images_path = tmp_path / "inherited-images.tsv"
    predecessor_path.write_text(json.dumps(predecessor), encoding="utf-8")
    changed_services_path.write_text("".join(f"{service}\n" for service in services), encoding="utf-8")

    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(HELPER_DIR), str(runner_dir))),
        "RELEASE_SERVICE_INVENTORY": "\n".join(services),
        "HOST_RELEASE_ID": new_host_id,
        "TOPOLOGY_DIGEST": topology_digest,
        "SIGNING_KEY_SHA256": software_signing_key,
    }
    command = [
        "python3",
        "-c",
        loader,
        str(predecessor_path),
        str(predecessor_manifest_path),
        str(predecessor_signature_path),
        str(changed_services_path),
        str(inherited_images_path),
    ]
    subprocess.run(command, cwd=ROOT, env=environment, check=True)

    assert inherited_images_path.read_text(encoding="utf-8") == ""
    assert predecessor_manifest_path.read_bytes()
    assert predecessor_signature_path.read_bytes()

    predecessor["status"] = "building"
    predecessor_path.write_text(json.dumps(predecessor), encoding="utf-8")
    assert subprocess.run(command, cwd=ROOT, env=environment, capture_output=True).returncode != 0
    predecessor["status"] = "completed"

    predecessor["paired_host_release"]["status"] = "building"
    predecessor_path.write_text(json.dumps(predecessor), encoding="utf-8")
    assert subprocess.run(command, cwd=ROOT, env=environment, capture_output=True).returncode != 0
    predecessor["paired_host_release"]["status"] = "completed"

    predecessor["paired_host_release"]["software_release_id"] = new_host_id
    predecessor_path.write_text(json.dumps(predecessor), encoding="utf-8")
    assert subprocess.run(command, cwd=ROOT, env=environment, capture_output=True).returncode != 0
    predecessor["paired_host_release"]["software_release_id"] = software_id

    predecessor_path.write_text(json.dumps(predecessor), encoding="utf-8")
    changed_services_path.write_text(
        "".join(f"{service}\n" for service in services[:-1]),
        encoding="utf-8",
    )
    assert subprocess.run(command, cwd=ROOT, env=environment, capture_output=True).returncode != 0


def _formal_pair_rows(sequence: int, source_commit: str):
    runner_dir = ROOT / "infra/host-release-runner"
    sys.path.insert(0, str(runner_dir))
    from host_release_engine import (  # noqa: PLC0415
        COSIGN_TOOLCHAIN,
        HOST_HEALTH_CONTRACT,
        canonical_digest,
        canonical_topology_digest,
    )

    services = (
        "ai-ctrl-optimizer",
        "ai-softsensor",
        "alertmanager",
        "edge-api",
        "edge-daq",
        "edge-monitor",
        "influxdb",
        "license-service",
        "mosquitto",
        "mosquitto-init",
        "postgres",
        "prometheus",
    )
    registry = "registry.example.com"
    software_id = f"00000000-0000-4000-8000-{sequence:012d}"
    host_id = f"10000000-0000-4000-8000-{sequence:012d}"
    version = f"1.0.0-rb8.{sequence}"
    compose = b"services:\n  edge-api:\n    image: edge-api:3\n"
    topology = {
        "compose_digest": "sha256:" + hashlib.sha256(compose).hexdigest(),
        "service_inventory": sorted(services),
        "health_contract": HOST_HEALTH_CONTRACT,
    }
    topology_digest = canonical_topology_digest(topology)
    software_manifest = {
        "schema": "drycontroller.software-release.v1",
        "release_id": software_id,
        "host_release_id": host_id,
        "topology_digest": topology_digest,
        "sequence": sequence,
        "version": version,
        "source_commit": source_commit,
        "registry": registry,
        "signing_key_sha256": "sha256:" + "b" * 64,
        "cosign": COSIGN_TOOLCHAIN,
        "services": {
            service: f"{registry}/drycontroller/{service}@sha256:{index:064x}"
            for index, service in enumerate(services, start=1)
        },
        "image_ids": {
            service: f"sha256:{index + 100:064x}"
            for index, service in enumerate(services, start=1)
        },
    }
    files = {
        "docker-compose.yml": (compose, 0o644),
        "infra/ota-helper/drycontroller_ota_helper.py": (b"helper\n", 0o644),
        "infra/ota-helper/release_engine.py": (b"engine\n", 0o644),
        "infra/ota-helper/ota-apply": (b"#!/usr/bin/python3\n", 0o755),
        "infra/ota-helper/initialize-active-release": (b"#!/usr/bin/python3\n", 0o755),
        "infra/ota-helper/start-active-release": (b"#!/usr/bin/python3\n", 0o755),
    }
    host_manifest = {
        "schema": "drycontroller.host-release.v1",
        "release_id": host_id,
        "sequence": sequence,
        "version": version,
        "source_commit": source_commit,
        "registry": registry,
        "signing_key_sha256": "sha256:" + "c" * 64,
        "cosign": COSIGN_TOOLCHAIN,
        "bundle": {
            "image": f"{registry}/drycontroller/host-bundle@sha256:{sequence:064x}",
            "image_id": "sha256:" + "d" * 64,
        },
        "topology": topology,
        "topology_digest": topology_digest,
        "software_release_id": software_id,
        "software_manifest_digest": canonical_digest(software_manifest),
        "files": [
            {
                "path": path,
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "mode": mode,
            }
            for path, (data, mode) in sorted(files.items())
        ],
    }
    software_row = {
        "id": software_id,
        "sequence": sequence,
        "version": version,
        "source_commit": source_commit,
        "status": "completed",
        "manifest": software_manifest,
        "manifest_digest": canonical_digest(software_manifest),
        "manifest_signature": "c29mdHdhcmUtc2ln",
    }
    host_row = {
        "id": host_id,
        "sequence": sequence,
        "version": version,
        "source_commit": source_commit,
        "status": "completed",
        "manifest": host_manifest,
        "manifest_digest": canonical_digest(host_manifest),
        "manifest_signature": "aG9zdC1zaWc=",
        "software_release_id": software_id,
    }
    return services, software_row, host_row


def _run_predecessor_resolver(
    tmp_path: Path,
    software_rows: list[dict],
    host_rows: list[dict],
    current_software: dict,
    current_host_id: str,
):
    script = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")
    match = re.search(
        r"release_step_resolve_predecessor\(\) \{.*?<<'PY'\n(.*?)\nPY\n",
        script,
        flags=re.DOTALL,
    )
    assert match is not None
    predecessor_path = tmp_path / "predecessor.json"
    resumable_path = tmp_path / "resumable-release.json"
    wrapper = """
import json
import os
import urllib.request

payloads = json.loads(os.environ["FAKE_CLOUD_PAYLOADS"])

class Response:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *_args):
        return False
    def read(self, _limit):
        return json.dumps(self.payload).encode()

def urlopen(request, timeout):
    return Response(payloads[request.full_url.rsplit("/", 1)[-1]])

urllib.request.urlopen = urlopen
exec(compile(os.environ["RESOLVER_SOURCE"], "<resolver>", "exec"))
"""
    manifest = current_software["manifest"]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            (str(HELPER_DIR), str(ROOT / "infra/host-release-runner"))
        ),
        "CLOUD_OPS_RELEASE_API_URL": "https://ops.example.com",
        "CLOUD_OPS_RELEASE_API_KEY": "test-key",
        "RELEASE_SERVICE_INVENTORY": "\n".join(sorted(manifest["services"])),
        "RELEASE_VERSION": current_software["version"],
        "RELEASE_SOURCE_COMMIT": current_software["source_commit"],
        "HOST_RELEASE_ID": current_host_id,
        "TOPOLOGY_DIGEST": manifest["topology_digest"],
        "HARBOR_HOST": manifest["registry"],
        "SIGNING_KEY_SHA256": manifest["signing_key_sha256"],
        "FAKE_CLOUD_PAYLOADS": json.dumps(
            {"software-releases": software_rows, "host-releases": host_rows}
        ),
        "RESOLVER_SOURCE": match.group(1),
    }
    result = subprocess.run(
        ["python3", "-c", wrapper, str(predecessor_path), str(resumable_path)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    predecessor = json.loads(predecessor_path.read_text(encoding="utf-8")) if predecessor_path.exists() else None
    resumable_bytes = resumable_path.read_bytes() if resumable_path.exists() else b""
    resumable = json.loads(resumable_bytes) if resumable_bytes else None
    return result, predecessor, resumable


def test_resolver_selects_latest_completed_pair_and_preserves_resumable_orphans(tmp_path: Path):
    _, paired_software, paired_host = _formal_pair_rows(3, "3" * 40)
    _, current_software, current_host = _formal_pair_rows(5, "5" * 40)
    _, standalone_software, _ = _formal_pair_rows(9, "9" * 40)
    standalone_software["manifest"]["host_release_id"] = paired_host["id"]
    standalone_software["manifest"]["topology_digest"] = paired_software["manifest"][
        "topology_digest"
    ]
    runner_dir = ROOT / "infra/host-release-runner"
    sys.path.insert(0, str(runner_dir))
    from host_release_engine import canonical_digest  # noqa: PLC0415

    standalone_software["manifest_digest"] = canonical_digest(
        standalone_software["manifest"]
    )
    current_host = {key: current_host[key] for key in ("id", "sequence", "version", "source_commit")}
    current_host["version"] = "2.0.0-host.5"
    current_host["status"] = "building"

    result, predecessor, resumable = _run_predecessor_resolver(
        tmp_path,
        [standalone_software, current_software, paired_software],
        [current_host, paired_host],
        current_software,
        current_host["id"],
    )

    assert result.returncode == 0, result.stderr
    assert predecessor["id"] == paired_software["id"]
    assert predecessor["paired_host_release"]["id"] == paired_host["id"]
    assert resumable["id"] == current_software["id"]


def test_resolver_writes_an_empty_resume_marker_for_a_new_release(tmp_path: Path):
    _, paired_software, paired_host = _formal_pair_rows(3, "3" * 40)
    _, current_software, current_host = _formal_pair_rows(5, "5" * 40)
    current_host = {
        key: current_host[key]
        for key in ("id", "sequence", "version", "source_commit")
    }
    current_host["status"] = "building"

    result, predecessor, resumable = _run_predecessor_resolver(
        tmp_path,
        [paired_software],
        [current_host, paired_host],
        current_software,
        current_host["id"],
    )

    assert result.returncode == 0, result.stderr
    assert predecessor["id"] == paired_software["id"]
    assert resumable is None
    assert (tmp_path / "resumable-release.json").read_bytes() == b""


def test_resolver_rejects_malformed_completed_pair_instead_of_falling_back(tmp_path: Path):
    _, older_software, older_host = _formal_pair_rows(3, "3" * 40)
    _, malformed_software, malformed_host = _formal_pair_rows(6, "6" * 40)
    malformed_host["manifest"]["software_release_id"] = older_software["id"]
    runner_dir = ROOT / "infra/host-release-runner"
    sys.path.insert(0, str(runner_dir))
    from host_release_engine import canonical_digest  # noqa: PLC0415

    malformed_host["manifest_digest"] = canonical_digest(malformed_host["manifest"])
    result, _, _ = _run_predecessor_resolver(
        tmp_path,
        [older_software, malformed_software],
        [older_host, malformed_host],
        malformed_software,
        malformed_host["id"],
    )

    assert result.returncode != 0


def test_completed_software_retry_revalidates_exact_release_before_resuming(tmp_path: Path):
    services, software_row, host_row = _formal_pair_rows(5, "5" * 40)
    script = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")
    function = script.split("release_step_resume_completed_release() {", 1)[1].split(
        "\n}\n\nrelease_step_sign_manifest", 1
    )[0]
    match = re.search(r"<<'PY'\n(.*?)\nPY\n", function, flags=re.DOTALL)
    assert match is not None
    resumable_path = tmp_path / "resumable.json"
    manifest_path = tmp_path / "manifest.json"
    signature_path = tmp_path / "manifest.sigstore.json"
    images_path = tmp_path / "images.tsv"
    resumable_path.write_text(json.dumps(software_row), encoding="utf-8")
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            (str(HELPER_DIR), str(ROOT / "infra/host-release-runner"))
        ),
        "RELEASE_SERVICE_INVENTORY": "\n".join(services),
        "RELEASE_VERSION": software_row["version"],
        "RELEASE_SOURCE_COMMIT": software_row["source_commit"],
        "HOST_RELEASE_ID": host_row["id"],
        "TOPOLOGY_DIGEST": software_row["manifest"]["topology_digest"],
        "HARBOR_HOST": software_row["manifest"]["registry"],
        "HARBOR_PROJECT": "drycontroller",
        "SIGNING_KEY_SHA256": software_row["manifest"]["signing_key_sha256"],
    }
    command = [
        "python3",
        "-c",
        match.group(1),
        str(resumable_path),
        str(manifest_path),
        str(signature_path),
        str(images_path),
    ]
    subprocess.run(command, cwd=ROOT, env=environment, check=True)

    sys.path.insert(0, str(HELPER_DIR))
    from release_engine import canonical_manifest_bytes  # noqa: PLC0415

    assert manifest_path.read_bytes() == canonical_manifest_bytes(software_row["manifest"])
    assert signature_path.read_bytes() == b"software-sig"
    assert images_path.read_text(encoding="utf-8").splitlines() == [
        "\t".join(
            (
                service,
                software_row["manifest"]["services"][service],
                software_row["manifest"]["image_ids"][service],
            )
        )
        for service in services
    ]
    for gate in (
        "cosign verify-blob",
        'release_step_scan "${resumed_ref}"',
        'release_step_verify_image "${resumed_ref}"',
        'docker pull --platform linux/amd64 "${resumed_ref}"',
        'release_step_resolve_portable_image_id "${resumed_ref}"',
        "python_base_image_contract.py verify-image",
        "python_base_image_contract.py verify-receipts",
        'json.dumps(row, sort_keys=True, separators=(",", ":"))',
    ):
        assert gate in function
    resume_guard = script.rindex('if [[ -s "${resumable_release_path}" ]]; then')
    resume_call = script.rindex("  release_step_resume_completed_release\n")
    create_call = script.rindex("release_step_create_cloud\n")
    assert resume_guard < resume_call < create_call

    wrong_source = json.loads(json.dumps(software_row))
    wrong_source["source_commit"] = "6" * 40
    resumable_path.write_text(json.dumps(wrong_source), encoding="utf-8")
    assert subprocess.run(command, cwd=ROOT, env=environment, capture_output=True).returncode != 0


def test_completed_software_retry_propagates_shell_gate_failure_without_output(tmp_path: Path):
    _, software_row, host_row = _formal_pair_rows(5, "5" * 40)
    script = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")
    function_match = re.search(
        r"(release_step_resume_completed_release\(\) \{.*?\n\})"
        r"\n\nrelease_step_sign_manifest",
        script,
        flags=re.DOTALL,
    )
    assert function_match is not None
    main = script.rsplit("release_step_resolve_predecessor\n", 1)[1].split(
        "release_step_resolve_changed_services", 1
    )[0]
    resumable_path = tmp_path / "resumable.json"
    output_path = tmp_path / "software-output.json"
    resumable_path.write_text(json.dumps(software_row), encoding="utf-8")
    harness = f"""
set -euo pipefail
release_step_resolve_predecessor() {{ :; }}
python3() {{
  if [[ "${{1:-}}" == scripts/ci/python_base_image_contract.py ]]; then
    return 0
  fi
  command "${{REAL_PYTHON}}" "$@"
}}
cosign() {{ return 0; }}
docker() {{ return 0; }}
release_step_scan() {{ return 42; }}
release_step_verify_image() {{ return 0; }}
release_step_resolve_portable_image_id() {{ printf 'sha256:%064d\n' 0; }}
declare -a LOCAL_TARGETS=()
{function_match.group(1)}
{main}
exit 88
"""
    manifest = software_row["manifest"]
    environment = {
        **os.environ,
        "REAL_PYTHON": sys.executable,
        "PYTHONPATH": os.pathsep.join(
            (str(HELPER_DIR), str(ROOT / "infra/host-release-runner"))
        ),
        "resumable_release_path": str(resumable_path),
        "manifest_path": str(tmp_path / "manifest.json"),
        "manifest_signature": str(tmp_path / "manifest.sigstore.json"),
        "inherited_images_path": str(tmp_path / "images.tsv"),
        "signing_pub": str(tmp_path / "cosign.pub"),
        "python_base_receipt_dir": str(tmp_path / "receipts"),
        "python_base_receipt_path": str(tmp_path / "receipt.json"),
        "SOFTWARE_RELEASE_OUTPUT": str(output_path),
        "RELEASE_SERVICE_INVENTORY": "\n".join(manifest["services"]),
        "RELEASE_VERSION": software_row["version"],
        "RELEASE_SOURCE_COMMIT": software_row["source_commit"],
        "HOST_RELEASE_ID": host_row["id"],
        "TOPOLOGY_DIGEST": manifest["topology_digest"],
        "HARBOR_HOST": manifest["registry"],
        "HARBOR_PROJECT": "drycontroller",
        "SIGNING_KEY_SHA256": manifest["signing_key_sha256"],
    }
    result = subprocess.run(
        ["bash", "-c", harness],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not output_path.exists()
