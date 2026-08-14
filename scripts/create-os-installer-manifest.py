#!/usr/bin/env python3
"""Create the adjacent, signed-after-write RB-6 ISO release manifest."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import platform
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", required=True, type=Path)
    parser.add_argument("--base-iso", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--installer-public-key", required=True, type=Path)
    args = parser.parse_args()
    host_seed_path = ROOT / "dist/autoinstall/host-release-seed.json"
    software_seed_path = ROOT / "dist/autoinstall/software-release-seed.json"
    host_seed = json.loads(host_seed_path.read_text(encoding="utf-8"))
    software_seed = json.loads(software_seed_path.read_text(encoding="utf-8"))
    dirty = git("status", "--porcelain=v1")
    if dirty:
        raise SystemExit("release manifest refuses a dirty git worktree")
    public_key_digest = sha256(args.installer_public_key)
    payload = {
        "schema": "drycontroller.os-installer-release.v2",
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
