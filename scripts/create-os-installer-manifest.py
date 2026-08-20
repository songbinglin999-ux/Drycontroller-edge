#!/usr/bin/env python3
"""Create the adjacent, signed-after-write RB-6 ISO release manifest."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import platform
import runpy
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "infra/bootstrap"))

from tailscale_client_lock import validate_tailscale_client_lock  # noqa: E402
from common.license_verifier import load_license_trust  # noqa: E402


MAX_PACKAGES_BYTES = 64 * 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact(path: Path) -> dict[str, object]:
    return {"path": str(path), "sha256": sha256(path), "size": path.stat().st_size}


def validate_distinct_signing_roots(
    installer_public_key: Path,
    host_public_key: Path,
    software_public_key: Path,
) -> None:
    digests = {
        sha256(installer_public_key),
        sha256(host_public_key),
        sha256(software_public_key),
    }
    if len(digests) != 3:
        raise SystemExit("Installer/Host/Software signing roots must all differ")


def validated_offline_apt_evidence(
    root: Path = ROOT,
    *,
    tailscale_client: dict[str, str] | None = None,
) -> dict[str, object]:
    apt_root = root / "dist/autoinstall/drycontroller-apt"
    lock_path = apt_root / "input-lock.json"
    receipt_path = apt_root / "receipt.json"
    packages_path = apt_root / "Packages"
    packages_gzip_path = apt_root / "Packages.gz"
    for label, path in (
        ("input lock", lock_path),
        ("receipt", receipt_path),
        ("Packages", packages_path),
        ("Packages.gz", packages_gzip_path),
    ):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise SystemExit(f"offline apt {label} is missing") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise SystemExit(f"offline apt {label} must be a regular file")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("offline apt lock or receipt is invalid JSON") from exc

    lock_fields = {
        "schema",
        "snapshot_uri",
        "suite",
        "architecture",
        "sources",
        "requested_packages",
        "resolved_packages",
    }
    receipt_fields = {
        "schema",
        "snapshot_uri",
        "suite",
        "architecture",
        "lock_sha256",
        "release_files",
        "package_indexes",
        "requested_packages",
        "resolved_packages",
        "extra_debs",
        "solver",
        "output",
    }
    if (
        not isinstance(lock, dict)
        or set(lock) != lock_fields
        or lock.get("schema") != "drycontroller.offline-apt-input.v1"
    ):
        raise SystemExit("offline apt input lock fields invalid")
    if (
        not isinstance(receipt, dict)
        or set(receipt) != receipt_fields
        or receipt.get("schema") != "drycontroller.offline-apt-receipt.v1"
    ):
        raise SystemExit("offline apt receipt fields invalid")
    validator = runpy.run_path(
        str(ROOT / "scripts/validate-autoinstall-offline-apt-input.py")
    )
    contract_error = validator["ContractError"]
    try:
        snapshot_uri = validator["_validate_snapshot_uri"](lock.get("snapshot_uri"))
        requested = validator["_requested_packages"](lock.get("requested_packages"))
        resolved = validator["_resolved_packages"](
            lock.get("resolved_packages"), apt_root
        )
    except contract_error as exc:
        raise SystemExit(f"offline apt package lock evidence invalid: {exc}") from exc
    if lock.get("suite") != "noble" or lock.get("architecture") != "amd64":
        raise SystemExit("offline apt snapshot/suite/architecture evidence invalid")
    if (
        receipt.get("snapshot_uri") != snapshot_uri
        or receipt.get("suite") != lock["suite"]
        or receipt.get("architecture") != lock["architecture"]
        or receipt.get("lock_sha256") != sha256(lock_path)
        or receipt.get("requested_packages") != requested
        or receipt.get("resolved_packages") != resolved
    ):
        raise SystemExit("offline apt receipt does not match the input lock")

    sources = lock.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SystemExit("offline apt source evidence invalid")
    expected_release_files: list[object] = []
    expected_package_indexes: list[object] = []
    source_suites: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "suite",
            "codename",
            "release",
            "packages",
        }:
            raise SystemExit("offline apt source evidence invalid")
        suite = source["suite"]
        if (
            not isinstance(suite, str)
            or validator["SOURCE_SUITE"].fullmatch(suite) is None
            or suite in source_suites
            or source["codename"] != "noble"
        ):
            raise SystemExit("offline apt source evidence invalid")
        source_suites.add(suite)
        try:
            release = validator["_metadata_record"](
                source["release"], label="Release"
            )
        except contract_error as exc:
            raise SystemExit(f"offline apt source evidence invalid: {exc}") from exc
        if release["path"] != f"dists/{suite}/Release":
            raise SystemExit("offline apt source evidence invalid")
        expected_release_files.append(release)
        indexes = source["packages"]
        if not isinstance(indexes, list) or not indexes:
            raise SystemExit("offline apt package index evidence invalid")
        components: set[str] = set()
        for index in indexes:
            if not isinstance(index, dict) or set(index) != {
                "component",
                "path",
                "size",
                "sha256",
            }:
                raise SystemExit("offline apt package index evidence invalid")
            component = index["component"]
            if (
                not isinstance(component, str)
                or component not in validator["COMPONENTS"]
                or component in components
            ):
                raise SystemExit("offline apt package index evidence invalid")
            components.add(component)
            try:
                metadata = validator["_metadata_record"](
                    {key: index[key] for key in ("path", "size", "sha256")},
                    label="Packages",
                )
            except contract_error as exc:
                raise SystemExit(
                    f"offline apt package index evidence invalid: {exc}"
                ) from exc
            if metadata["path"] != (
                f"dists/{suite}/{component}/binary-amd64/Packages"
            ):
                raise SystemExit("offline apt package index evidence invalid")
            expected_package_indexes.append(
                {"suite": suite, "component": component, **metadata}
            )
    if "noble" not in source_suites:
        raise SystemExit("offline apt source evidence invalid")
    if (
        receipt.get("release_files") != expected_release_files
        or receipt.get("package_indexes") != expected_package_indexes
    ):
        raise SystemExit("offline apt metadata evidence does not match the input lock")

    output = receipt.get("output")
    if not isinstance(output, dict) or set(output) != {
        "packages_sha256",
        "packages_gzip_sha256",
        "pool",
    }:
        raise SystemExit("offline apt output evidence invalid")
    if output.get("packages_sha256") != sha256(packages_path):
        raise SystemExit("offline apt Packages evidence is stale")
    if output.get("packages_gzip_sha256") != sha256(packages_gzip_path):
        raise SystemExit("offline apt Packages.gz evidence is stale")
    if packages_gzip_path.stat().st_size > MAX_PACKAGES_BYTES:
        raise SystemExit("offline apt Packages evidence is too large")
    with packages_path.open("rb") as stream:
        packages_raw = stream.read(MAX_PACKAGES_BYTES + 1)
    if len(packages_raw) > MAX_PACKAGES_BYTES:
        raise SystemExit("offline apt Packages evidence is too large")
    try:
        with gzip.open(packages_gzip_path, "rb") as stream:
            packages_gzip_raw = stream.read(MAX_PACKAGES_BYTES + 1)
    except (EOFError, OSError) as exc:
        raise SystemExit("offline apt Packages.gz evidence is invalid") from exc
    if len(packages_gzip_raw) > MAX_PACKAGES_BYTES:
        raise SystemExit("offline apt Packages.gz evidence is too large")
    if packages_gzip_raw != packages_raw:
        raise SystemExit("offline apt Packages.gz does not match Packages")
    try:
        package_index_records = validator["_package_index_records"](
            packages_raw, label="final Packages"
        )
    except contract_error as exc:
        raise SystemExit(f"offline apt final Packages inventory invalid: {exc}") from exc
    packages_inventory: list[dict[str, object]] = []
    for record in package_index_records:
        size_raw = record["Size"]
        try:
            size = int(size_raw)
        except ValueError as exc:
            raise SystemExit("offline apt final Packages inventory invalid") from exc
        if (
            str(size) != size_raw
            or size < 1
            or validator["PACKAGE_NAME"].fullmatch(record["Package"]) is None
            or validator["PACKAGE_VERSION"].fullmatch(record["Version"]) is None
            or record["Architecture"] not in {"amd64", "all"}
            or validator["LOWER_SHA256"].fullmatch(record["SHA256"]) is None
        ):
            raise SystemExit("offline apt final Packages inventory invalid")
        packages_inventory.append(
            {
                "name": record["Package"],
                "version": record["Version"],
                "architecture": record["Architecture"],
                "filename": record["Filename"],
                "size": size,
                "sha256": record["SHA256"],
            }
        )
    resolved = receipt.get("resolved_packages")
    extras = receipt.get("extra_debs")
    pool = output.get("pool")
    if not all(isinstance(value, list) for value in (resolved, extras, pool)):
        raise SystemExit("offline apt package inventory evidence invalid")
    expected_pool = sorted(
        [*resolved, *extras],
        key=lambda item: str(item.get("filename")) if isinstance(item, dict) else "",
    )
    if pool != expected_pool:
        raise SystemExit("offline apt package inventory does not match the lock and extras")
    solver = receipt.get("solver")
    expected_solver = {
        "requested_packages": [
            *requested,
            *(
                {"name": item["name"], "version": item["version"]}
                for item in extras
                if isinstance(item, dict) and "name" in item and "version" in item
            ),
        ],
        "resolved_packages": [*resolved, *extras],
    }
    if solver != expected_solver:
        raise SystemExit("offline apt solver evidence does not match the final package set")
    actual_pool_names: set[str] = set()
    for record in pool:
        if not isinstance(record, dict) or set(record) != {
            "name",
            "version",
            "architecture",
            "filename",
            "size",
            "sha256",
        }:
            raise SystemExit("offline apt package inventory evidence invalid")
        filename = record.get("filename")
        pure_filename = PurePosixPath(filename) if isinstance(filename, str) else None
        if (
            not isinstance(filename, str)
            or pure_filename is None
            or len(pure_filename.parts) < 2
            or pure_filename.parts[0] != "pool"
            or pure_filename.suffix != ".deb"
            or pure_filename.as_posix() != filename
            or any(part in {"", ".", ".."} for part in pure_filename.parts)
            or "\\" in filename
            or filename in actual_pool_names
        ):
            raise SystemExit("offline apt package inventory evidence invalid")
        actual_pool_names.add(filename)
        package_path = apt_root / filename
        try:
            metadata = package_path.lstat()
        except OSError as exc:
            raise SystemExit("offline apt package artifact is missing") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or record.get("size") != metadata.st_size
            or record.get("sha256") != sha256(package_path)
        ):
            raise SystemExit("offline apt package artifact evidence is stale")
    pool_root = apt_root / "pool"
    try:
        pool_metadata = pool_root.lstat()
        pool_entries = list(pool_root.rglob("*"))
    except OSError as exc:
        raise SystemExit("offline apt package pool is missing") from exc
    if not stat.S_ISDIR(pool_metadata.st_mode):
        raise SystemExit("offline apt package pool is invalid")
    disk_pool: set[str] = set()
    for path in pool_entries:
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise SystemExit("offline apt package pool contains invalid artifacts")
        disk_pool.add(path.relative_to(apt_root).as_posix())
    if disk_pool != actual_pool_names:
        raise SystemExit("offline apt package pool contains unrecorded artifacts")
    if sorted(packages_inventory, key=lambda item: str(item["filename"])) != pool:
        raise SystemExit("offline apt final Packages inventory does not match the pool")
    if tailscale_client is None:
        if extras:
            raise SystemExit("offline apt extra DEB is not bound to a Tailscale client lock")
    else:
        try:
            tailscale_client = validate_tailscale_client_lock(tailscale_client)
        except ValueError as exc:
            raise SystemExit("Tailscale client lock evidence is invalid") from exc
        expected_filename = f"pool/{tailscale_client['deb_url'].rsplit('/', 1)[-1]}"
        if len(extras) != 1 or not isinstance(extras[0], dict):
            raise SystemExit("offline apt must contain one Tailscale extra DEB")
        extra = extras[0]
        if (
            extra.get("name") != "tailscale"
            or extra.get("version") != tailscale_client["version"]
            or extra.get("architecture") != tailscale_client["architecture"]
            or extra.get("filename") != expected_filename
            or extra.get("sha256") != tailscale_client["deb_sha256"]
        ):
            raise SystemExit("offline apt Tailscale extra DEB does not match its client lock")

    return {
        "schema": receipt["schema"],
        "snapshot_uri": snapshot_uri,
        "suite": lock["suite"],
        "architecture": lock["architecture"],
        "input_lock": artifact(lock_path),
        "receipt": artifact(receipt_path),
        "release_files": receipt["release_files"],
        "package_indexes": receipt["package_indexes"],
        "requested_packages": receipt["requested_packages"],
        "resolved_packages": receipt["resolved_packages"],
        "extra_debs": receipt["extra_debs"],
        "solver": receipt["solver"],
        "output": {
            "packages": artifact(packages_path),
            "packages_gzip": artifact(packages_gzip_path),
            "pool": pool,
        },
    }


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def validated_public_scan_evidence(
    report_path: Path,
    *,
    iso: Path,
    base_iso: Path,
    root: Path = ROOT,
    scanner_path: Path | None = None,
) -> dict[str, object]:
    scanner = runpy.run_path(str(scanner_path or root / "scripts/public-material-scan.py"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    expected_top = {"schema", "scanner_version", "policy_sha256", "passed", "artifacts"}
    if not isinstance(report, dict) or set(report) != expected_top:
        raise SystemExit("public material scan report fields invalid")
    if (
        report.get("schema") != scanner["SCHEMA"]
        or report.get("scanner_version") != scanner["SCANNER_VERSION"]
        or report.get("policy_sha256") != scanner["POLICY_SHA256"]
        or report.get("passed") is not True
    ):
        raise SystemExit("public material scan report is not an approved passing policy")
    artifacts = report.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(scanner["SURFACES"]):
        raise SystemExit("public material scan report does not cover all five surfaces")

    paths = {
        "source": {
            "sha256": root / "dist/drycontroller-edge.tar.gz",
        },
        "host": {
            "sha256": root / "dist/host-image.tar",
            "seed_sha256": root / "dist/autoinstall/host-release-seed.json",
            "public_key_sha256": root / "dist/autoinstall/host-release-cosign.pub",
        },
        "software": {
            "sha256": root / "dist/software-images.tar",
            "seed_sha256": root / "dist/autoinstall/software-release-seed.json",
            "public_key_sha256": root / "dist/autoinstall/software-release-cosign.pub",
        },
        "iso": {
            "sha256": iso,
            "base_iso_sha256": base_iso,
        },
        "filesystem": {
            "iso_sha256": iso,
        },
    }
    evidence_artifacts: dict[str, object] = {}
    for surface, digest_paths in paths.items():
        record = artifacts.get(surface)
        expected_fields = {"passed", "findings", *digest_paths}
        if (
            not isinstance(record, dict)
            or set(record) != expected_fields
            or record.get("passed") is not True
            or record.get("findings") != []
        ):
            raise SystemExit(f"public material {surface} evidence invalid")
        for field, path in digest_paths.items():
            if record.get(field) != sha256(path.resolve()):
                raise SystemExit(f"public material {surface} evidence is stale")
        evidence_artifacts[surface] = {
            key: value for key, value in record.items() if key != "findings"
        }
    return {
        "schema": report["schema"],
        "scanner_version": report["scanner_version"],
        "policy_sha256": report["policy_sha256"],
        "report_sha256": sha256(report_path),
        "artifacts": evidence_artifacts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", required=True, type=Path)
    parser.add_argument("--base-iso", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--installer-public-key", required=True, type=Path)
    parser.add_argument("--public-scan-report", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("release manifest output already exists")
    host_seed_path = ROOT / "dist/autoinstall/host-release-seed.json"
    software_seed_path = ROOT / "dist/autoinstall/software-release-seed.json"
    host_public_key_path = ROOT / "dist/autoinstall/host-release-cosign.pub"
    software_public_key_path = ROOT / "dist/autoinstall/software-release-cosign.pub"
    license_trust_path = ROOT / "dist/autoinstall/license-trust.json"
    host_seed = json.loads(host_seed_path.read_text(encoding="utf-8"))
    software_seed = json.loads(software_seed_path.read_text(encoding="utf-8"))
    license_trust = load_license_trust(license_trust_path)
    tailscale_lock_path = ROOT / "dist/autoinstall/tailscale-client.json"
    tailscale_client = validate_tailscale_client_lock(
        json.loads(tailscale_lock_path.read_text(encoding="utf-8"))
    )
    tailscale_deb_path = (
        ROOT
        / "dist/autoinstall/drycontroller-apt/pool"
        / tailscale_client["deb_url"].rsplit("/", 1)[-1]
    )
    offline_apt = validated_offline_apt_evidence(tailscale_client=tailscale_client)
    tailscale_extra = offline_apt["extra_debs"][0]
    dirty = git("status", "--porcelain=v1")
    if dirty:
        raise SystemExit("release manifest refuses a dirty git worktree")
    public_scan = validated_public_scan_evidence(
        args.public_scan_report.resolve(),
        iso=args.iso.resolve(),
        base_iso=args.base_iso.resolve(),
    )
    public_key_digest = sha256(args.installer_public_key)
    validate_distinct_signing_roots(
        args.installer_public_key,
        host_public_key_path,
        software_public_key_path,
    )
    payload = {
        "schema": "drycontroller.os-installer-release.v4",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "release": {
            "channel": args.channel,
            "architecture": "linux/amd64",
            "public_material_only": True,
            "include_images": True,
        },
        "git": {
            "branch": git("branch", "--show-current"),
            "commit": git("rev-parse", "HEAD"),
            "dirty": False,
        },
        "builder": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "os_release_sha256": sha256(Path("/etc/os-release")),
            "docker": subprocess.check_output(["docker", "--version"], text=True).strip(),
            "cosign": subprocess.check_output(["cosign", "version"], text=True).splitlines()[0],
        },
        "installer_signing_key_sha256": "sha256:" + public_key_digest,
        "public_material_scan": public_scan,
        "artifacts": {
            "iso": artifact(args.iso.resolve()),
            "base_iso": artifact(args.base_iso.resolve()),
            "source_bundle": artifact(ROOT / "dist/drycontroller-edge.tar.gz"),
            "host_image": artifact(ROOT / "dist/host-image.tar"),
            "software_images": artifact(ROOT / "dist/software-images.tar"),
            "host_seed": {
                **artifact(host_seed_path),
                "release_id": host_seed["manifest"]["release_id"],
                "manifest_digest": host_seed["manifest_digest"],
                "topology_digest": host_seed["manifest"]["topology_digest"],
            },
            "software_seed": {
                **artifact(software_seed_path),
                "release_id": software_seed["manifest"]["release_id"],
                "manifest_digest": software_seed["manifest_digest"],
                "topology_digest": software_seed["manifest"]["topology_digest"],
            },
            "public_config": artifact(ROOT / "dist/autoinstall/public-config.json"),
            "license_trust": {
                **artifact(license_trust_path),
                "canonical_sha256": license_trust.canonical_sha256,
                "key_id": license_trust.key_id,
                "public_key_sha256": "sha256:"
                + hashlib.sha256(license_trust.public_key_spki_der).hexdigest(),
            },
            "tailscale_client_lock": artifact(tailscale_lock_path),
            "tailscale_client_deb": {
                "path": str(tailscale_deb_path),
                "sha256": tailscale_extra["sha256"],
                "size": tailscale_extra["size"],
                "version": tailscale_client["version"],
                "architecture": tailscale_client["architecture"],
            },
            "identity_wheelhouse_sums": artifact(
                ROOT / "dist/autoinstall/identity-wheelhouse/SHA256SUMS"
            ),
            "offline_apt": offline_apt,
        },
        "checks": {
            "public_material_scan": {
                "passed": True,
                "checker": "public-material-scan.py",
                "policy_sha256": public_scan["policy_sha256"],
            },
            "final_iso_release_checklist": {
                "passed": True,
                "checker": "os_installer_usb_maker.py::verify_final_iso",
            },
            "signed_initial_pair": {
                "passed": True,
                "checker": "prepare-initial-release.py",
            },
        },
    }
    if host_seed["manifest"]["topology_digest"] != software_seed["manifest"]["topology_digest"]:
        raise SystemExit("initial H/S topology mismatch")
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
