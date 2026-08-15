#!/usr/bin/env python3
"""Create the adjacent, signed-after-write RB-6 ISO release manifest."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import platform
import runpy
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact(path: Path) -> dict[str, object]:
    return {"path": str(path), "sha256": sha256(path), "size": path.stat().st_size}


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
    host_seed = json.loads(host_seed_path.read_text(encoding="utf-8"))
    software_seed = json.loads(software_seed_path.read_text(encoding="utf-8"))
    dirty = git("status", "--porcelain=v1")
    if dirty:
        raise SystemExit("release manifest refuses a dirty git worktree")
    public_scan = validated_public_scan_evidence(
        args.public_scan_report.resolve(),
        iso=args.iso.resolve(),
        base_iso=args.base_iso.resolve(),
    )
    public_key_digest = sha256(args.installer_public_key)
    payload = {
        "schema": "drycontroller.os-installer-release.v3",
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
            "identity_wheelhouse_sums": artifact(
                ROOT / "dist/autoinstall/identity-wheelhouse/SHA256SUMS"
            ),
            "offline_apt_packages": artifact(ROOT / "dist/autoinstall/drycontroller-apt/Packages"),
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
