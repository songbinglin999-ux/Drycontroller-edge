from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import lzma
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts" / "public-material-scan.py"
SOURCE_VALIDATOR = ROOT / "scripts" / "validate-source-bundle.py"
MANIFEST_CREATOR = ROOT / "scripts" / "create-os-installer-manifest.py"
UBUNTU_24_04_4_SERVER_SHA256 = (
    "e907d92eeec9df64163a7e454cbc8d7755e8ddc7ed42f99dbc80c40f1a138433"
)
TOOLCHAIN = {
    "version": "v3.1.3",
    "linux_amd64_sha256": "sha256:4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
}
HOST_HELPERS = (
    "infra/ota-helper/drycontroller_ota_helper.py",
    "infra/ota-helper/release_engine.py",
    "infra/ota-helper/release_security_contract.py",
    "infra/ota-helper/ota-apply",
    "infra/ota-helper/initialize-active-release",
    "infra/ota-helper/start-active-release",
)
EXECUTABLE_HOST_FILES = {
    "infra/ota-helper/ota-apply",
    "infra/ota-helper/initialize-active-release",
    "infra/ota-helper/start-active-release",
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256(path.read_bytes())


def _tar_bytes(files: dict[str, bytes], *, modes: dict[str, int] | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            member.mode = (modes or {}).get(name, 0o644)
            archive.addfile(member, io.BytesIO(raw))
    return output.getvalue()


def _ar_bytes(files: dict[str, bytes]) -> bytes:
    output = bytearray(b"!<arch>\n")
    for name, raw in files.items():
        header = b"".join(
            (
                f"{name}/".encode().ljust(16),
                b"0".ljust(12),
                b"0".ljust(6),
                b"0".ljust(6),
                b"100644".ljust(8),
                str(len(raw)).encode().ljust(10),
                b"`\n",
            )
        )
        assert len(header) == 60
        output.extend(header)
        output.extend(raw)
        if len(raw) % 2:
            output.extend(b"\n")
    return bytes(output)


def _newc_bytes(files: dict[str, bytes]) -> bytes:
    output = bytearray()

    def add(name: str, raw: bytes, mode: int) -> None:
        encoded_name = name.encode("utf-8") + b"\0"
        fields = (
            1,
            mode,
            0,
            0,
            1,
            0,
            len(raw),
            0,
            0,
            0,
            0,
            len(encoded_name),
            0,
        )
        output.extend(b"070701" + b"".join(f"{value:08x}".encode() for value in fields))
        output.extend(encoded_name)
        output.extend(b"\0" * (-len(output) % 4))
        output.extend(raw)
        output.extend(b"\0" * (-len(output) % 4))

    for name, raw in files.items():
        add(name, raw, stat.S_IFREG | 0o644)
    add("TRAILER!!!", b"", 0)
    return bytes(output)


def _write_tar(path: Path, files: dict[str, bytes], *, gz: bool = False) -> None:
    with tarfile.open(path, mode="w:gz" if gz else "w") as archive:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(raw))


def _fake_cosign(tmp_path: Path) -> Path:
    cosign = tmp_path / "cosign"
    cosign.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cosign.chmod(0o755)
    return cosign


def _seed(path: Path, manifest: dict[str, object]) -> None:
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    path.write_text(
        json.dumps(
            {
                "manifest": manifest,
                "manifest_digest": "sha256:" + _sha256(canonical),
                "manifest_signature": base64.b64encode(b"{}").decode("ascii"),
            }
        ),
        encoding="utf-8",
    )


def _run_scanner(
    tmp_path: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    report = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--report", str(report), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    return result, json.loads(report.read_text(encoding="utf-8"))


def test_source_bundle_blocks_long_lived_token_and_redacts_output(tmp_path: Path):
    secret = "ghp_" + "A" * 36
    bundle = tmp_path / "source.tar.gz"
    _write_tar(bundle, {"config/runtime.env": f"API_TOKEN={secret}\n".encode()}, gz=True)

    result = subprocess.run(
        [sys.executable, str(SOURCE_VALIDATOR), str(bundle)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert not bundle.exists()
    assert secret not in result.stdout
    assert secret not in result.stderr


@pytest.mark.parametrize(
    "credential_path",
    [".docker/config.json", ".aws/credentials", ".kube/config"],
)
def test_root_level_credential_files_fail_closed(tmp_path: Path, credential_path: str):
    bundle = tmp_path / "source.tar.gz"
    _write_tar(bundle, {credential_path: b'{"public-looking":"opaque-base64-material"}'}, gz=True)

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "source",
        "--source",
        str(bundle),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "CREDENTIAL-FILE"
        and finding["path"] == credential_path
        for finding in report["artifacts"]["source"]["findings"]
    )


def test_source_nested_tar_xz_secret_is_recursively_scanned(tmp_path: Path):
    secret = "ghp_" + "X" * 36
    nested = lzma.compress(_tar_bytes({"ordinary-name.txt": secret.encode()}))
    bundle = tmp_path / "source.tar.gz"
    _write_tar(bundle, {"payload/content.tar.xz": nested}, gz=True)

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "source",
        "--source",
        str(bundle),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "KNOWN-API-TOKEN"
        for finding in report["artifacts"]["source"]["findings"]
    )
    assert secret not in result.stderr


def test_source_nested_deb_data_tar_xz_is_recursively_scanned(tmp_path: Path):
    secret = "ghp_" + "P" * 36
    package = _ar_bytes(
        {
            "debian-binary": b"2.0\n",
            "data.tar.xz": lzma.compress(_tar_bytes({"usr/share/ordinary.txt": secret.encode()})),
        }
    )
    bundle = tmp_path / "source.tar.gz"
    _write_tar(bundle, {"offline/package.deb": package}, gz=True)

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "source",
        "--source",
        str(bundle),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "KNOWN-API-TOKEN"
        and "package.deb" in finding["archive"]
        for finding in report["artifacts"]["source"]["findings"]
    )
    assert secret not in result.stderr


def test_source_nested_newc_and_gzip_secret_is_recursively_scanned(tmp_path: Path):
    secret = "ghp_" + "N" * 36
    initrd = _newc_bytes({"payload/content.gz": gzip.compress(secret.encode())})
    bundle = tmp_path / "source.tar.gz"
    _write_tar(bundle, {"casper/initrd": initrd}, gz=True)

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "source",
        "--source",
        str(bundle),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "KNOWN-API-TOKEN"
        and "initrd" in finding["archive"]
        for finding in report["artifacts"]["source"]["findings"]
    )
    assert secret not in result.stderr


def test_unsupported_legacy_cpio_fails_closed_as_unknown_archive(tmp_path: Path):
    bundle = tmp_path / "source.tar.gz"
    _write_tar(bundle, {"casper/initrd": b"070707" + b"opaque-cpio"}, gz=True)

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "source",
        "--source",
        str(bundle),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "SCAN-UNKNOWN-ARCHIVE"
        and finding["path"] == "casper/initrd"
        for finding in report["artifacts"]["source"]["findings"]
    )


def test_lz4_newc_member_fails_closed_as_unknown_archive(tmp_path: Path):
    bundle = tmp_path / "source.tar.gz"
    initrd = _newc_bytes({"payload/content.lz4": b"opaque-lz4-payload"})
    _write_tar(bundle, {"casper/initrd": initrd}, gz=True)

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "source",
        "--source",
        str(bundle),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "SCAN-UNKNOWN-ARCHIVE"
        and finding["path"] == "payload/content.lz4"
        for finding in report["artifacts"]["source"]["findings"]
    )


@pytest.mark.skipif(shutil.which("lz4") is None, reason="real LZ4 tool is required")
def test_real_lz4_magic_in_newc_member_fails_closed_without_plaintext_leak(tmp_path: Path):
    secret = "ghp_" + "L" * 36
    plaintext = tmp_path / "plaintext"
    compressed = tmp_path / "compressed"
    plaintext.write_text(secret, encoding="utf-8")
    subprocess.run(
        ["lz4", "-q", "-f", str(plaintext), str(compressed)],
        check=True,
        capture_output=True,
    )
    compressed_bytes = compressed.read_bytes()
    assert secret.encode() not in compressed_bytes
    bundle = tmp_path / "source.tar.gz"
    initrd = _newc_bytes({"payload/content": compressed_bytes})
    _write_tar(bundle, {"casper/initrd": initrd}, gz=True)

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "source",
        "--source",
        str(bundle),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "SCAN-UNKNOWN-ARCHIVE"
        and finding["path"] == "payload/content"
        for finding in report["artifacts"]["source"]["findings"]
    )
    assert secret not in result.stderr
    assert secret not in json.dumps(report)


def test_long_lived_tailscale_api_key_fails_closed(tmp_path: Path):
    secret = "tskey-api-" + "T" * 32
    bundle = tmp_path / "source.tar.gz"
    _write_tar(bundle, {"ordinary.txt": secret.encode()}, gz=True)

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "source",
        "--source",
        str(bundle),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "TAILSCALE-AUTH-KEY"
        for finding in report["artifacts"]["source"]["findings"]
    )
    assert secret not in result.stderr


def test_source_scan_uses_snapshot_even_if_live_path_is_swapped(tmp_path: Path):
    secret = "ghp_" + "R" * 36
    source = tmp_path / "source.tar.gz"
    clean = tmp_path / "clean.tar.gz"
    backup = tmp_path / "malicious-backup.tar.gz"
    _write_tar(source, {"ordinary.txt": secret.encode()}, gz=True)
    _write_tar(clean, {"ordinary.txt": b"public content"}, gz=True)
    expected_digest = _sha256_path(source)
    scanner = runpy.run_path(str(SCANNER))
    original_raw = scanner["_scan_raw_path"]
    original_tar = scanner["_scan_tar_path"]

    def swap_before_raw(candidate: Path, **kwargs: object) -> None:
        source.rename(backup)
        clean.rename(source)
        original_raw(candidate, **kwargs)

    def restore_after_tar(candidate: Path, **kwargs: object) -> None:
        try:
            original_tar(candidate, **kwargs)
        finally:
            source.rename(clean)
            backup.rename(source)

    scanner["scan_source"].__globals__["_scan_raw_path"] = swap_before_raw
    scanner["scan_source"].__globals__["_scan_tar_path"] = restore_after_tar
    result = scanner["scan_source"](source, deadline=time.monotonic() + 30)

    assert result["passed"] is False
    assert result["sha256"] == expected_digest == _sha256_path(source)
    assert any(finding["rule_id"] == "KNOWN-API-TOKEN" for finding in result["findings"])


def test_public_keys_sigstore_digest_and_public_config_pass_exact_content_rules(tmp_path: Path):
    bundle = tmp_path / "public.tar.gz"
    _write_tar(
        bundle,
        {
            "host-release-cosign.pub": b"-----BEGIN PUBLIC KEY-----\nPUBLIC\n-----END PUBLIC KEY-----\n",
            "host-manifest.sigstore.json": b'{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}',
            "identity-wheelhouse/SHA256SUMS": (b"a" * 64) + b"  package.whl\n",
            "public-config.json": json.dumps(
                {
                    "schema": "drycontroller.os-installer-public-config.v3",
                    "cloud_ops_base_url": "https://ops.example.test",
                    "cloud_ops_tailnet_address": "100.100.0.10",
                    "tailscale_login_server": "https://headscale.example.test",
                    "factory_interface": "enp1s0",
                    "factory_address": "169.254.203.2/30",
                }
            ).encode(),
        },
        gz=True,
    )

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "source",
        "--source",
        str(bundle),
    )

    assert result.returncode == 0
    assert report["passed"] is True
    assert report["artifacts"]["source"]["findings"] == []


def _host_archive(tmp_path: Path, private_key: bytes) -> tuple[Path, Path, Path]:
    key = tmp_path / "host.pub"
    key.write_bytes(b"host-public-key")
    files = {"docker-compose.yml": b"services:\n  edge-api:\n    image: local\n"}
    files.update({path: b"public helper\n" for path in HOST_HELPERS})
    files["infra/ota-helper/release_engine.py"] = private_key
    layer_files = {f"host-release/{path}": raw for path, raw in files.items()}
    modes = {
        f"host-release/{path}": 0o755 if path in EXECUTABLE_HOST_FILES else 0o644
        for path in files
    }
    layer = _tar_bytes(layer_files, modes=modes)
    config = b'{"architecture":"amd64"}'
    config_name = _sha256(config) + ".json"
    layer_name = "layer/layer.tar"
    archive_path = tmp_path / "host-image.tar"
    _write_tar(
        archive_path,
        {
            "manifest.json": json.dumps(
                [{"Config": config_name, "RepoTags": ["registry.example.test/host-bundle:1"], "Layers": [layer_name]}]
            ).encode(),
            config_name: config,
            layer_name: layer,
        },
    )
    compose_digest = "sha256:" + _sha256(files["docker-compose.yml"])
    topology = {
        "compose_digest": compose_digest,
        "service_inventory": ["edge-api"],
        "health_contract": "drycontroller.edge-health.v1",
    }
    topology_digest = "sha256:" + _sha256(
        json.dumps(topology, sort_keys=True, separators=(",", ":")).encode()
    )
    manifest = {
        "schema": "drycontroller.host-release.v1",
        "release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "sequence": 1,
        "version": "1.0.0",
        "source_commit": "a" * 40,
        "registry": "registry.example.test",
        "signing_key_sha256": "sha256:" + _sha256(key.read_bytes()),
        "cosign": TOOLCHAIN,
        "bundle": {
            "image": "registry.example.test/host-bundle@sha256:" + "b" * 64,
            "image_id": "sha256:" + _sha256(config),
        },
        "topology": topology,
        "topology_digest": topology_digest,
        "software_release_id": "11111111-1111-4111-8111-111111111111",
        "software_manifest_digest": "sha256:" + "c" * 64,
        "files": [
            {
                "path": path,
                "sha256": "sha256:" + _sha256(raw),
                "size": len(raw),
                "mode": 0o755 if path in EXECUTABLE_HOST_FILES else 0o644,
            }
            for path, raw in files.items()
        ],
    }
    seed = tmp_path / "host-seed.json"
    _seed(seed, manifest)
    return archive_path, seed, key


def test_signed_host_bundle_with_openssh_private_key_fails_closed(tmp_path: Path):
    private_key = (
        b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----\n" + b"not-a-real-key\n" + b"-----END " + b"OPENSSH PRIVATE KEY-----\n"
    )
    archive, seed, key = _host_archive(tmp_path, private_key)
    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "host",
        "--host",
        str(archive),
        "--host-seed",
        str(seed),
        "--host-public-key",
        str(key),
        "--cosign",
        str(_fake_cosign(tmp_path)),
    )

    assert result.returncode == 1
    findings = report["artifacts"]["host"]["findings"]
    assert any(finding["rule_id"] == "PRIVATE-KEY" for finding in findings)
    assert private_key.decode() not in result.stderr
    assert private_key.decode() not in json.dumps(report)


def _software_archive(
    tmp_path: Path,
    *,
    env_value: str,
    layer_value: str,
    delete_in_later_layer: bool = False,
    layer_payloads: list[bytes] | None = None,
) -> tuple[Path, Path, Path]:
    key = tmp_path / "software.pub"
    key.write_bytes(b"software-public-key")
    config = json.dumps(
        {
            "architecture": "amd64",
            "config": {"Env": [f"SHARED_PASSWORD={env_value}"], "Labels": {}},
            "history": [{"created_by": "RUN public build"}],
        },
        separators=(",", ":"),
    ).encode()
    image_id = "sha256:" + _sha256(config)
    tag = "drycontroller.local/edge-api:sha256-" + image_id.removeprefix("sha256:")
    config_name = _sha256(config) + ".json"
    archive_files = {config_name: config}
    if layer_payloads is None:
        layer = _tar_bytes({"app/runtime.env": f"API_TOKEN={layer_value}\n".encode()})
        layer_name = "layer/layer.tar"
        layer_names = [layer_name]
        archive_files[layer_name] = layer
        if delete_in_later_layer:
            whiteout_name = "layer-2/layer.tar"
            layer_names.append(whiteout_name)
            archive_files[whiteout_name] = _tar_bytes({"app/.wh.runtime.env": b""})
    else:
        layer_names = [f"layer-{index}/layer.tar" for index in range(len(layer_payloads))]
        archive_files.update(dict(zip(layer_names, layer_payloads, strict=True)))
    archive = tmp_path / "software-images.tar"
    archive_files["manifest.json"] = json.dumps(
        [{"Config": config_name, "RepoTags": [tag], "Layers": layer_names}]
    ).encode()
    _write_tar(
        archive,
        archive_files,
    )
    manifest = {
        "schema": "drycontroller.software-release.v1",
        "release_id": "11111111-1111-4111-8111-111111111111",
        "host_release_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "topology_digest": "sha256:" + "d" * 64,
        "sequence": 1,
        "version": "1.0.0",
        "source_commit": "a" * 40,
        "registry": "registry.example.test",
        "signing_key_sha256": "sha256:" + _sha256(key.read_bytes()),
        "cosign": TOOLCHAIN,
        "services": {"edge-api": "registry.example.test/edge-api@sha256:" + "e" * 64},
        "image_ids": {"edge-api": image_id},
    }
    seed = tmp_path / "software-seed.json"
    _seed(seed, manifest)
    return archive, seed, key


def test_digest_correct_software_layer_and_config_env_secrets_fail_closed(tmp_path: Path):
    layer_secret = "ghp_" + "B" * 36
    env_secret = "shared-credential-value"
    archive, seed, key = _software_archive(
        tmp_path, env_value=env_secret, layer_value=layer_secret
    )
    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "software",
        "--software",
        str(archive),
        "--software-seed",
        str(seed),
        "--software-public-key",
        str(key),
        "--cosign",
        str(_fake_cosign(tmp_path)),
    )

    assert result.returncode == 1
    findings = report["artifacts"]["software"]["findings"]
    assert any(finding["rule_id"] == "SECRET-ASSIGNMENT" for finding in findings)
    assert any(finding["rule_id"] == "KNOWN-API-TOKEN" for finding in findings)
    combined_output = result.stdout + result.stderr + json.dumps(report)
    assert env_secret not in combined_output
    assert layer_secret not in combined_output


def test_secret_deleted_by_later_software_layer_still_fails_historical_layer_scan(tmp_path: Path):
    layer_secret = "ghp_" + "D" * 36
    archive, seed, key = _software_archive(
        tmp_path,
        env_value="${SHARED_PASSWORD}",
        layer_value=layer_secret,
        delete_in_later_layer=True,
    )

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "software",
        "--software",
        str(archive),
        "--software-seed",
        str(seed),
        "--software-public-key",
        str(key),
        "--cosign",
        str(_fake_cosign(tmp_path)),
    )

    assert result.returncode == 1
    findings = report["artifacts"]["software"]["findings"]
    assert any(
        finding["rule_id"] == "KNOWN-API-TOKEN"
        and "layer/layer.tar" in finding["archive"]
        for finding in findings
    )
    assert layer_secret not in result.stderr


def test_software_merged_rootfs_preserves_safe_symlink_paths(tmp_path: Path):
    first_layer = io.BytesIO()
    with tarfile.open(fileobj=first_layer, mode="w") as archive_file:
        directory = tarfile.TarInfo(".kube")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive_file.addfile(directory)
        link = tarfile.TarInfo("alias")
        link.type = tarfile.SYMTYPE
        link.linkname = ".kube"
        archive_file.addfile(link)
    second_layer = _tar_bytes({"alias/config": b"opaque-base64-private-material"})
    archive, seed, key = _software_archive(
        tmp_path,
        env_value="${SHARED_PASSWORD}",
        layer_value="${API_TOKEN}",
        layer_payloads=[first_layer.getvalue(), second_layer],
    )

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "software",
        "--software",
        str(archive),
        "--software-seed",
        str(seed),
        "--software-public-key",
        str(key),
        "--cosign",
        str(_fake_cosign(tmp_path)),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "CREDENTIAL-FILE"
        and finding["archive"].endswith(":merged-rootfs")
        and finding["path"] == ".kube/config"
        for finding in report["artifacts"]["software"]["findings"]
    )


@pytest.mark.parametrize("nested", [False, True])
def test_iso_plain_or_nested_tar_secret_fails_both_iso_and_filesystem_surfaces(
    tmp_path: Path, nested: bool
):
    secret = "ghp_" + "C" * 36
    if nested:
        payload = _tar_bytes({"ordinary-name.txt": f"API_KEY={secret}\n".encode()})
        files = {"payloads/content.tar": payload}
    else:
        files = {"ordinary-name.txt": f"API_KEY={secret}\n".encode()}
    iso = tmp_path / "installer.iso"
    _write_tar(iso, files)

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "iso",
        "filesystem",
        "--iso",
        str(iso),
    )

    assert result.returncode == 1
    assert report["artifacts"]["iso"]["passed"] is False
    assert report["artifacts"]["filesystem"]["passed"] is False
    assert secret not in result.stderr
    assert secret not in json.dumps(report)


def test_user_data_password_hash_fails_closed(tmp_path: Path):
    password_hash = "$6$" + "salt" + "$" + "A" * 64
    iso = tmp_path / "installer.iso"
    _write_tar(iso, {"user-data": f"password: '{password_hash}'\n".encode()})

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "iso",
        "filesystem",
        "--iso",
        str(iso),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "PASSWORD-HASH"
        for finding in report["artifacts"]["filesystem"]["findings"]
    )
    assert password_hash not in result.stderr


@pytest.mark.skipif(
    shutil.which("mksquashfs") is None or shutil.which("unsquashfs") is None,
    reason="real SquashFS tools are required",
)
@pytest.mark.parametrize("contains_secret", [False, True])
def test_filesystem_surface_expands_real_squashfs_installed_root(
    tmp_path: Path, contains_secret: bool
):
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    password_hash = "$6$" + "salt" + "$" + "S" * 64
    content = (
        f"root:{password_hash}:1:2:3:4:5:6:7\n".encode()
        if contains_secret
        else b"public operating system content\n"
    )
    (rootfs / "etc" / ("shadow" if contains_secret else "issue")).write_bytes(content)
    squashfs = tmp_path / "filesystem.squashfs"
    subprocess.run(
        [
            "mksquashfs",
            str(rootfs),
            str(squashfs),
            "-noappend",
            "-no-progress",
            "-processors",
            "1",
        ],
        check=True,
        capture_output=True,
    )
    iso = tmp_path / "installer.iso"
    _write_tar(iso, {"casper/filesystem.squashfs": squashfs.read_bytes()})

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "iso",
        "filesystem",
        "--iso",
        str(iso),
    )

    assert result.returncode == (1 if contains_secret else 0)
    findings = report["artifacts"]["filesystem"]["findings"]
    if contains_secret:
        assert any(
            finding["rule_id"] == "PASSWORD-HASH"
            and "filesystem.squashfs" in finding["archive"]
            and finding["path"] == "etc/shadow"
            for finding in findings
        )
        assert password_hash not in result.stderr
    else:
        assert findings == []


def test_manifest_generation_rejects_artifact_changed_after_scan(tmp_path: Path):
    root = tmp_path / "repo"
    autoinstall = root / "dist" / "autoinstall"
    autoinstall.mkdir(parents=True)
    paths = {
        "source": root / "dist" / "drycontroller-edge.tar.gz",
        "host": root / "dist" / "host-image.tar",
        "software": root / "dist" / "software-images.tar",
        "host_seed": autoinstall / "host-release-seed.json",
        "host_key": autoinstall / "host-release-cosign.pub",
        "software_seed": autoinstall / "software-release-seed.json",
        "software_key": autoinstall / "software-release-cosign.pub",
        "iso": root / "dist" / "installer.iso",
        "base": root / "dist" / "base.iso",
    }
    for name, path in paths.items():
        path.write_bytes(f"artifact-{name}".encode())
    scanner = runpy.run_path(str(SCANNER))
    report = {
        "schema": scanner["SCHEMA"],
        "scanner_version": scanner["SCANNER_VERSION"],
        "policy_sha256": scanner["POLICY_SHA256"],
        "passed": True,
        "artifacts": {
            "source": {"sha256": _sha256_path(paths["source"]), "passed": True, "findings": []},
            "host": {
                "sha256": _sha256_path(paths["host"]),
                "seed_sha256": _sha256_path(paths["host_seed"]),
                "public_key_sha256": _sha256_path(paths["host_key"]),
                "passed": True,
                "findings": [],
            },
            "software": {
                "sha256": _sha256_path(paths["software"]),
                "seed_sha256": _sha256_path(paths["software_seed"]),
                "public_key_sha256": _sha256_path(paths["software_key"]),
                "passed": True,
                "findings": [],
            },
            "iso": {
                "sha256": _sha256_path(paths["iso"]),
                "base_iso_sha256": _sha256_path(paths["base"]),
                "passed": True,
                "findings": [],
            },
            "filesystem": {
                "iso_sha256": _sha256_path(paths["iso"]),
                "passed": True,
                "findings": [],
            },
        },
    }
    report_path = root / "dist" / "scan.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    creator = runpy.run_path(str(MANIFEST_CREATOR))
    creator["validated_public_scan_evidence"](
        report_path,
        iso=paths["iso"],
        base_iso=paths["base"],
        root=root,
        scanner_path=SCANNER,
    )

    paths["software"].write_bytes(b"modified after scan")
    with pytest.raises(SystemExit, match="software evidence is stale"):
        creator["validated_public_scan_evidence"](
            report_path,
            iso=paths["iso"],
            base_iso=paths["base"],
            root=root,
            scanner_path=SCANNER,
        )


def test_unknown_nested_archive_fails_closed(tmp_path: Path):
    bundle = tmp_path / "source.tar.gz"
    _write_tar(bundle, {"payload/broken.tar": b"not a tar archive"}, gz=True)

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "source",
        "--source",
        str(bundle),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "SCAN-UNKNOWN-ARCHIVE"
        for finding in report["artifacts"]["source"]["findings"]
    )


@pytest.mark.parametrize(
    "supplied_digest",
    ["0" * 64, UBUNTU_24_04_4_SERVER_SHA256],
)
def test_base_iso_requires_matching_approved_digest_before_extraction(
    tmp_path: Path, supplied_digest: str
):
    base = tmp_path / "base.iso"
    final = tmp_path / "final.iso"
    _write_tar(base, {"base.txt": b"public base"})
    _write_tar(final, {"base.txt": b"public base", "overlay.txt": b"public overlay"})
    scanner = runpy.run_path(str(SCANNER))
    assert scanner["APPROVED_BASE_ISO_SHA256S"] == {UBUNTU_24_04_4_SERVER_SHA256}
    extracted = False

    def extraction_must_not_run(*args: object, **kwargs: object) -> None:
        nonlocal extracted
        extracted = True

    scanner["scan_iso_surfaces"].__globals__["_extract_iso"] = extraction_must_not_run
    result = scanner["scan_iso_surfaces"](
        final,
        base_iso=base,
        approved_base_iso_sha256=supplied_digest,
        requested={"iso"},
        deadline=time.monotonic() + 30,
    )

    assert extracted is False
    assert result["iso"]["passed"] is False
    assert any(
        finding["rule_id"] == "SCAN-VERIFICATION-FAILED"
        for finding in result["iso"]["findings"]
    )


def test_unsupported_archive_candidate_fails_closed(tmp_path: Path):
    bundle = tmp_path / "source.tar.gz"
    _write_tar(bundle, {"payload/content.7z": b"7z\xbc\xaf\x27\x1copaque"}, gz=True)

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "source",
        "--source",
        str(bundle),
    )

    assert result.returncode == 1
    assert any(
        finding["rule_id"] == "SCAN-UNKNOWN-ARCHIVE"
        and finding["path"] == "payload/content.7z"
        for finding in report["artifacts"]["source"]["findings"]
    )


def test_iso_extraction_does_not_follow_symlink_outside_temporary_root(tmp_path: Path):
    escaped = tmp_path / "escaped.txt"
    iso = tmp_path / "installer.iso"
    with tarfile.open(iso, mode="w") as archive:
        link = tarfile.TarInfo("redirect")
        link.type = tarfile.SYMTYPE
        link.linkname = str(tmp_path)
        archive.addfile(link)
        raw = b"must stay inside scanner"
        member = tarfile.TarInfo("redirect/escaped.txt")
        member.size = len(raw)
        archive.addfile(member, io.BytesIO(raw))

    result, report = _run_scanner(
        tmp_path,
        "--surfaces",
        "filesystem",
        "--iso",
        str(iso),
    )

    assert result.returncode == 1
    assert report["artifacts"]["filesystem"]["passed"] is False
    assert not escaped.exists()


def test_timeout_and_resource_limits_fail_closed(tmp_path: Path):
    bundle = tmp_path / "source.tar.gz"
    _write_tar(bundle, {"ordinary.txt": b"public content"}, gz=True)

    timed_scanner = runpy.run_path(str(SCANNER))
    timed = timed_scanner["scan_source"](
        bundle,
        deadline=time.monotonic() - 1,
    )
    assert timed["passed"] is False
    assert any(finding["rule_id"] == "SCAN-TIMEOUT" for finding in timed["findings"])

    limited_scanner = runpy.run_path(str(SCANNER))
    limited_scanner["scan_source"].__globals__["MAX_MEMBER_BYTES"] = 1
    limited = limited_scanner["scan_source"](
        bundle,
        deadline=time.monotonic() + 30,
    )
    assert limited["passed"] is False
    assert any(finding["rule_id"] == "SCAN-RESOURCE-LIMIT" for finding in limited["findings"])


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    [
        ("MAX_ENTRIES", 1),
        ("MAX_MEMBER_BYTES", 3),
        ("MAX_TOTAL_BYTES", 7),
    ],
)
def test_iso_declared_resource_limits_are_checked_before_extraction(
    tmp_path: Path, limit_name: str, limit_value: int
):
    iso = tmp_path / "installer.iso"
    _write_tar(iso, {"one.txt": b"1234", "two.txt": b"5678"})
    scanner = runpy.run_path(str(SCANNER))
    extract_iso = scanner["_extract_iso"]
    extract_iso.__globals__[limit_name] = limit_value
    state = scanner["ScanState"](deadline=time.monotonic() + 30)
    root = tmp_path / "extracted"
    root.mkdir()

    with pytest.raises(scanner["LimitExceeded"], match="SCAN-RESOURCE-LIMIT"):
        extract_iso(iso, root, state=state, archive="iso")

    assert list(root.iterdir()) == []


def test_iso_timeout_is_checked_before_extraction(tmp_path: Path):
    iso = tmp_path / "installer.iso"
    _write_tar(iso, {"ordinary.txt": b"public content"})
    scanner = runpy.run_path(str(SCANNER))
    state = scanner["ScanState"](deadline=time.monotonic() - 1)
    root = tmp_path / "extracted"
    root.mkdir()

    with pytest.raises(scanner["LimitExceeded"], match="SCAN-TIMEOUT"):
        scanner["_extract_iso"](iso, root, state=state, archive="iso")

    assert list(root.iterdir()) == []


def test_archive_lister_that_closes_stdout_then_hangs_is_killed_at_deadline():
    scanner = runpy.run_path(str(SCANNER))
    started = time.monotonic()

    with pytest.raises(scanner["LimitExceeded"], match="SCAN-TIMEOUT"):
        scanner["_run_command_lines"](
            [
                sys.executable,
                "-c",
                "import os,time; os.close(1); time.sleep(5)",
            ],
            deadline=time.monotonic() + 0.2,
            consume=lambda _line: None,
        )

    assert time.monotonic() - started < 2
