from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate-autoinstall-offline-apt-input.py"
MANIFEST_CREATOR = ROOT / "scripts" / "create-os-installer-manifest.py"
SNAPSHOT_URI = "https://snapshot.ubuntu.com/ubuntu/20260820T000000Z/"
REQUESTED_PACKAGES = (
    "ca-certificates",
    "curl",
    "python3",
    "python3-venv",
    "tar",
    "iproute2",
    "docker.io",
    "netplan.io",
    "tpm2-tools",
    "docker-compose-v2",
)
RESOLVED_PACKAGES = (*REQUESTED_PACKAGES, "iptables")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_snapshot_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    snapshot = tmp_path / "snapshot"
    packages_path = snapshot / "dists/noble/main/binary-amd64/Packages"
    packages_path.parent.mkdir(parents=True)
    resolved: list[dict[str, object]] = []
    stanzas: list[str] = []
    for index, name in enumerate(RESOLVED_PACKAGES, start=1):
        version = f"1.0.{index}"
        filename = f"pool/main/{name[0]}/{name}/{name}_{version}_amd64.deb"
        raw = f"fixture:{name}:{version}\n".encode()
        package_path = snapshot / filename
        package_path.parent.mkdir(parents=True, exist_ok=True)
        package_path.write_bytes(raw)
        record = {
            "name": name,
            "version": version,
            "architecture": "amd64",
            "filename": filename,
            "size": len(raw),
            "sha256": _sha256(raw),
        }
        resolved.append(record)
        stanzas.append(
            "\n".join(
                (
                    f"Package: {name}",
                    f"Version: {version}",
                    "Architecture: amd64",
                    f"Filename: {filename}",
                    f"Size: {len(raw)}",
                    f"SHA256: {_sha256(raw)}",
                    "Description: offline apt replay fixture",
                    "",
                )
            )
        )
    packages_raw = "\n".join(stanzas).encode()
    packages_path.write_bytes(packages_raw)
    relative_packages = "main/binary-amd64/Packages"
    release_raw = (
        "Suite: noble\n"
        "Codename: noble\n"
        "Architectures: amd64\n"
        "Components: main\n"
        "SHA256:\n"
        f" {_sha256(b'')} 0 main/debian-installer/binary-amd64/Packages\n"
        f" {_sha256(packages_raw)} {len(packages_raw)} {relative_packages}\n"
    ).encode()
    release_path = snapshot / "dists/noble/Release"
    release_path.write_bytes(release_raw)
    lock = {
        "schema": "drycontroller.offline-apt-input.v1",
        "snapshot_uri": SNAPSHOT_URI,
        "suite": "noble",
        "architecture": "amd64",
        "sources": [
            {
                "suite": "noble",
                "codename": "noble",
                "release": {
                    "path": "dists/noble/Release",
                    "size": len(release_raw),
                    "sha256": _sha256(release_raw),
                },
                "packages": [
                    {
                        "component": "main",
                        "path": "dists/noble/main/binary-amd64/Packages",
                        "size": len(packages_raw),
                        "sha256": _sha256(packages_raw),
                    }
                ],
            }
        ],
        "requested_packages": [
            {"name": record["name"], "version": record["version"]}
            for record in resolved
            if record["name"] in REQUESTED_PACKAGES
        ],
        "resolved_packages": resolved,
    }
    lock_path = tmp_path / "offline-apt-input-lock.json"
    lock_path.write_text(
        json.dumps(lock, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return snapshot, lock_path, lock


def _validate(
    tmp_path: Path,
    snapshot: Path,
    lock_path: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "validate",
            "--snapshot-root",
            str(snapshot),
            "--lock",
            str(lock_path),
            "--lock-sha256",
            _sha256(lock_path.read_bytes()),
            "--plan-out",
            str(tmp_path / "plan.json"),
            "--combined-packages-out",
            str(tmp_path / "Packages"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_fixed_snapshot_contract_accepts_exact_noble_amd64_fixture(tmp_path: Path):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)

    result = _validate(tmp_path, snapshot, lock_path)

    assert result.returncode == 0, result.stderr
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan["schema"] == "drycontroller.offline-apt-plan.v1"
    assert plan["lock_sha256"] == _sha256(lock_path.read_bytes())
    assert plan["snapshot_uri"] == SNAPSHOT_URI
    assert plan["requested_packages"] == lock["requested_packages"]
    assert plan["resolved_packages"] == lock["resolved_packages"]


def test_current_or_live_mirror_uri_is_rejected(tmp_path: Path):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    lock["snapshot_uri"] = "https://snapshot.ubuntu.com/ubuntu/current/"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    result = _validate(tmp_path, snapshot, lock_path)

    assert result.returncode == 1
    assert "immutable Ubuntu snapshot URI" in result.stderr


@pytest.mark.parametrize("metadata", ["Release", "Packages"])
def test_release_or_packages_digest_drift_is_rejected(
    tmp_path: Path, metadata: str
):
    snapshot, lock_path, _lock = _write_snapshot_fixture(tmp_path)
    target = (
        snapshot / "dists/noble/Release"
        if metadata == "Release"
        else snapshot / "dists/noble/main/binary-amd64/Packages"
    )
    raw = target.read_bytes()
    target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])

    result = _validate(tmp_path, snapshot, lock_path)

    assert result.returncode == 1
    assert f"{metadata} digest mismatch" in result.stderr


def test_missing_requested_package_version_lock_is_rejected(tmp_path: Path):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    del lock["requested_packages"][0]["version"]
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    result = _validate(tmp_path, snapshot, lock_path)

    assert result.returncode == 1
    assert "requested package fields invalid" in result.stderr


def test_compose_plugin_does_not_satisfy_the_installer_package_contract(tmp_path: Path):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    compose = next(
        item for item in lock["requested_packages"] if item["name"] == "docker-compose-v2"
    )
    compose["name"] = "docker-compose-plugin"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    result = _validate(tmp_path, snapshot, lock_path)

    assert result.returncode == 1
    assert "requested package set does not match" in result.stderr


def test_release_must_bind_the_exact_packages_digest(tmp_path: Path):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    packages_path = snapshot / "dists/noble/main/binary-amd64/Packages"
    packages_path.write_bytes(packages_path.read_bytes() + b"X-Fixture-Drift: yes\n")
    package_index = lock["sources"][0]["packages"][0]
    package_index["size"] = packages_path.stat().st_size
    package_index["sha256"] = _sha256(packages_path.read_bytes())
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    result = _validate(tmp_path, snapshot, lock_path)

    assert result.returncode == 1
    assert "Packages digest/size is not bound by Release" in result.stderr


def test_locked_deb_digest_drift_is_rejected(tmp_path: Path):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    package_path = snapshot / lock["resolved_packages"][0]["filename"]
    raw = package_path.read_bytes()
    package_path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])

    result = _validate(tmp_path, snapshot, lock_path)

    assert result.returncode == 1
    assert "resolved package artifact mismatch" in result.stderr


@pytest.mark.parametrize(
    ("old", "new"),
    [(b"Suite: noble", b"Suite: jammy"), (b"Architectures: amd64", b"Architectures: arm64")],
)
def test_release_metadata_suite_or_architecture_drift_is_rejected(
    tmp_path: Path, old: bytes, new: bytes
):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    release_path = snapshot / "dists/noble/Release"
    release_path.write_bytes(release_path.read_bytes().replace(old, new))
    release_record = lock["sources"][0]["release"]
    release_record["size"] = release_path.stat().st_size
    release_record["sha256"] = _sha256(release_path.read_bytes())
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    result = _validate(tmp_path, snapshot, lock_path)

    assert result.returncode == 1
    assert "Release suite/codename/architecture mismatch" in result.stderr


def test_snapshot_package_path_traversal_is_rejected(tmp_path: Path):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    lock["resolved_packages"][0]["filename"] = "pool/../outside.deb"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    result = _validate(tmp_path, snapshot, lock_path)

    assert result.returncode == 1
    assert "resolved package path invalid" in result.stderr


def test_snapshot_package_symlink_is_rejected(tmp_path: Path):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    package_path = snapshot / lock["resolved_packages"][0]["filename"]
    raw = package_path.read_bytes()
    target = tmp_path / "same-bytes.deb"
    target.write_bytes(raw)
    package_path.unlink()
    package_path.symlink_to(target)

    result = _validate(tmp_path, snapshot, lock_path)

    assert result.returncode == 1
    assert "resolved package path invalid" in result.stderr


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("suite", "jammy", "suite must be noble"),
        ("architecture", "arm64", "architecture must be amd64"),
    ],
)
def test_wrong_suite_or_architecture_is_rejected(
    tmp_path: Path, field: str, value: str, message: str
):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    lock[field] = value
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    result = _validate(tmp_path, snapshot, lock_path)

    assert result.returncode == 1
    assert message in result.stderr


def test_dependency_resolution_set_change_is_rejected(tmp_path: Path):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    validation = _validate(tmp_path, snapshot, lock_path)
    assert validation.returncode == 0, validation.stderr
    solver_output = tmp_path / "solver.txt"
    lines = [
        f"Inst {record['name']} ({record['version']} fixture [amd64])"
        for record in lock["resolved_packages"]
    ]
    lines.append("Inst unexpected-dependency (9.9 fixture [amd64])")
    solver_output.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "verify-solver",
            "--plan",
            str(tmp_path / "plan.json"),
            "--solver-output",
            str(solver_output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "dependency resolution differs from resolved_packages lock" in result.stderr


def test_dependency_resolution_missing_fixed_extra_is_rejected(tmp_path: Path):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    validation = _validate(tmp_path, snapshot, lock_path)
    assert validation.returncode == 0, validation.stderr
    plan_path = tmp_path / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["resolved_packages"].append(
        {
            "name": "tailscale",
            "version": "1.88.0",
            "architecture": "amd64",
            "filename": "pool/tailscale_1.88.0_amd64.deb",
            "size": 1,
            "sha256": "0" * 64,
        }
    )
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    solver_output = tmp_path / "solver.txt"
    solver_output.write_text(
        "\n".join(
            f"Inst {record['name']} ({record['version']} fixture [{record['architecture']}])"
            for record in lock["resolved_packages"]
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "verify-solver",
            "--plan",
            str(plan_path),
            "--solver-output",
            str(solver_output),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "dependency resolution differs from resolved_packages lock" in result.stderr


def test_offline_builder_uses_only_validated_local_snapshot_bytes(tmp_path: Path):
    snapshot, lock_path, lock = _write_snapshot_fixture(tmp_path)
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "build-autoinstall-offline-apt-repo.sh",
        "validate-autoinstall-offline-apt-input.py",
    ):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    apt_log = tmp_path / "apt.log"
    extra_deb = tmp_path / "tailscale_1.88.0_amd64.deb"
    extra_deb.write_bytes(b"fixture:tailscale:1.88.0\n")
    extra_packages = tmp_path / "extra-Packages"
    extra_packages.write_text(
        "\n".join(
            (
                "Package: tailscale",
                "Version: 1.88.0",
                "Architecture: amd64",
                "Depends: ca-certificates",
                f"Filename: pool/{extra_deb.name}",
                f"Size: {extra_deb.stat().st_size}",
                f"SHA256: {_sha256(extra_deb.read_bytes())}",
                "Description: fixed public extra fixture",
                "",
            )
        ),
        encoding="utf-8",
    )
    (fake_bin / "apt-get").write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

with Path(os.environ["APT_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
    for argument in sys.argv[1:]:
        if argument.startswith("Dir::Etc::sourcelist="):
            handle.write(Path(argument.split("=", 1)[1]).read_text(encoding="utf-8"))
if "--simulate" in sys.argv:
    lock = json.loads(Path(os.environ["FIXTURE_LOCK"]).read_text(encoding="utf-8"))
    for record in lock["resolved_packages"]:
        print(f"Inst {record['name']} ({record['version']} fixture [{record['architecture']}])")
    print("Inst tailscale (1.88.0 fixture [amd64])")
""",
        encoding="utf-8",
    )
    (fake_bin / "dpkg-deb").write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[2])
field = sys.argv[3]
if path.name == os.environ["FIXTURE_EXTRA_DEB_NAME"]:
    print({"Package": "tailscale", "Version": "1.88.0", "Architecture": "amd64"}[field])
else:
    lock = json.loads(Path(os.environ["FIXTURE_LOCK"]).read_text(encoding="utf-8"))
    record = next(item for item in lock["resolved_packages"] if Path(item["filename"]).name == path.name)
    print({"Package": record["name"], "Version": record["version"], "Architecture": record["architecture"]}[field])
""",
        encoding="utf-8",
    )
    (fake_bin / "dpkg-scanpackages").write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path

if Path.cwd().name != "extra-repo":
    print(Path(os.environ["FIXTURE_PACKAGES"]).read_text(encoding="utf-8").rstrip())
    print()
print(Path(os.environ["FIXTURE_EXTRA_PACKAGES"]).read_text(encoding="utf-8"), end="")
""",
        encoding="utf-8",
    )
    for command in fake_bin.iterdir():
        command.chmod(0o755)
    packages_fixture = snapshot / "dists/noble/main/binary-amd64/Packages"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "APT_LOG": str(apt_log),
        "FIXTURE_LOCK": str(lock_path),
        "FIXTURE_PACKAGES": str(packages_fixture),
        "FIXTURE_EXTRA_PACKAGES": str(extra_packages),
        "FIXTURE_EXTRA_DEB_NAME": extra_deb.name,
    }

    result = subprocess.run(
        [
            "bash",
            str(scripts / "build-autoinstall-offline-apt-repo.sh"),
            "--snapshot-root",
            str(snapshot),
            "--lock",
            str(lock_path),
            "--lock-sha256",
            _sha256(lock_path.read_bytes()),
            "--extra-deb",
            str(extra_deb),
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    output = repo / "dist/autoinstall/drycontroller-apt"
    assert output.is_dir(), result.stdout + result.stderr
    receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["lock_sha256"] == _sha256(lock_path.read_bytes())
    assert receipt["snapshot_uri"] == SNAPSHOT_URI
    assert receipt["resolved_packages"] == lock["resolved_packages"]
    assert receipt["extra_debs"] == [
        {
            "name": "tailscale",
            "version": "1.88.0",
            "architecture": "amd64",
            "filename": f"pool/{extra_deb.name}",
            "size": extra_deb.stat().st_size,
            "sha256": _sha256(extra_deb.read_bytes()),
        }
    ]
    assert receipt["solver"]["requested_packages"][-1] == {
        "name": "tailscale",
        "version": "1.88.0",
    }
    assert receipt["solver"]["resolved_packages"][-1] == receipt["extra_debs"][0]
    assert (output / "input-lock.json").read_bytes() == lock_path.read_bytes()
    assert sorted(
        path.relative_to(output).as_posix() for path in (output / "pool").rglob("*.deb")
    ) == sorted(
        [
            *(record["filename"] for record in lock["resolved_packages"]),
            f"pool/{extra_deb.name}",
        ]
    )
    apt_calls = apt_log.read_text(encoding="utf-8")
    assert "file:" in apt_calls
    assert "Dir::Etc::parts=/dev/null" in apt_calls
    assert "Dir::Etc::preferences=/dev/null" in apt_calls
    assert "tailscale=1.88.0" in apt_calls
    assert "http://" not in apt_calls
    assert "https://" not in apt_calls


def test_offline_builder_has_no_mirror_or_online_fallback(tmp_path: Path):
    script = ROOT / "scripts/build-autoinstall-offline-apt-repo.sh"
    result = subprocess.run(
        ["bash", str(script), "--mirror", "https://archive.ubuntu.com/ubuntu/"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "unknown argument: --mirror" in result.stderr


def test_formal_wrapper_rejects_the_retired_live_mirror_option():
    wrapper = ROOT / "scripts/build-os-installer-release.sh"
    result = subprocess.run(
        ["bash", str(wrapper), "--apt-mirror", "https://archive.ubuntu.com/ubuntu/"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "unknown argument: --apt-mirror" in result.stderr


def _write_manifest_fixture(
    tmp_path: Path,
    *,
    tailscale_client: dict[str, str] | None = None,
) -> tuple[object, Path, Path, Path, dict[str, object]]:
    root = tmp_path / "repo"
    apt_root = root / "dist/autoinstall/drycontroller-apt"
    apt_root.mkdir(parents=True)
    snapshot, fixture_lock, lock_payload = _write_snapshot_fixture(tmp_path / "fixture")
    lock = apt_root / "input-lock.json"
    packages = apt_root / "Packages"
    packages_gz = apt_root / "Packages.gz"
    lock.write_bytes(fixture_lock.read_bytes())
    packages_raw = (snapshot / "dists/noble/main/binary-amd64/Packages").read_bytes()
    extras: list[dict[str, object]] = []
    pool_dir = apt_root / "pool"
    pool_dir.mkdir()
    for record in lock_payload["resolved_packages"]:
        target = apt_root / record["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot / record["filename"], target)
    if tailscale_client is not None:
        extra_raw = f"fixture:tailscale:{tailscale_client['version']}\n".encode()
        extra_filename = f"pool/{tailscale_client['deb_url'].rsplit('/', 1)[-1]}"
        extra_path = apt_root / extra_filename
        extra_path.write_bytes(extra_raw)
        extras.append(
            {
                "name": "tailscale",
                "version": tailscale_client["version"],
                "architecture": tailscale_client["architecture"],
                "filename": extra_filename,
                "size": len(extra_raw),
                "sha256": _sha256(extra_raw),
            }
        )
        packages_raw = packages_raw.rstrip(b"\n") + b"\n\n" + (
            f"Package: tailscale\nVersion: {tailscale_client['version']}\n"
            f"Architecture: {tailscale_client['architecture']}\n"
            f"Filename: {extra_filename}\nSize: {len(extra_raw)}\n"
            f"SHA256: {_sha256(extra_raw)}\nDescription: fixed public extra fixture\n\n"
        ).encode()
    packages.write_bytes(packages_raw)
    packages_gz.write_bytes(gzip.compress(packages_raw, compresslevel=9, mtime=0))
    release_files = [source["release"] for source in lock_payload["sources"]]
    package_indexes = [
        {"suite": source["suite"], **index}
        for source in lock_payload["sources"]
        for index in source["packages"]
    ]
    receipt = {
        "schema": "drycontroller.offline-apt-receipt.v1",
        "snapshot_uri": SNAPSHOT_URI,
        "suite": "noble",
        "architecture": "amd64",
        "lock_sha256": _sha256(lock.read_bytes()),
        "release_files": release_files,
        "package_indexes": package_indexes,
        "requested_packages": lock_payload["requested_packages"],
        "resolved_packages": lock_payload["resolved_packages"],
        "extra_debs": extras,
        "solver": {
            "requested_packages": [
                *lock_payload["requested_packages"],
                *(
                    {"name": item["name"], "version": item["version"]}
                    for item in extras
                ),
            ],
            "resolved_packages": [*lock_payload["resolved_packages"], *extras],
        },
        "output": {
            "packages_sha256": _sha256(packages.read_bytes()),
            "packages_gzip_sha256": _sha256(packages_gz.read_bytes()),
            "pool": sorted(
                (dict(item) for item in [*lock_payload["resolved_packages"], *extras]),
                key=lambda item: item["filename"],
            ),
        },
    }
    receipt_path = apt_root / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("create_os_installer_manifest", MANIFEST_CREATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, root, receipt_path, packages, receipt


def test_manifest_evidence_rejects_stale_offline_apt_packages(tmp_path: Path):
    module, root, receipt_path, packages, _receipt = _write_manifest_fixture(tmp_path)

    evidence = module.validated_offline_apt_evidence(root=root)
    assert evidence["receipt"]["sha256"] == _sha256(receipt_path.read_bytes())
    packages.write_bytes(b"Package: drifted\n")
    with pytest.raises(SystemExit, match="offline apt Packages evidence is stale"):
        module.validated_offline_apt_evidence(root=root)


def test_manifest_evidence_rejects_receipt_lock_tampering(tmp_path: Path):
    module, root, receipt_path, _packages, receipt = _write_manifest_fixture(tmp_path)
    receipt["lock_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SystemExit, match="receipt does not match the input lock"):
        module.validated_offline_apt_evidence(root=root)


def test_manifest_evidence_rejects_receipt_metadata_tampering(tmp_path: Path):
    module, root, receipt_path, _packages, receipt = _write_manifest_fixture(tmp_path)
    receipt["release_files"][0]["sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SystemExit, match="metadata evidence does not match"):
        module.validated_offline_apt_evidence(root=root)


def test_manifest_evidence_rejects_receipt_pool_tampering(tmp_path: Path):
    module, root, receipt_path, _packages, receipt = _write_manifest_fixture(tmp_path)
    receipt["output"]["pool"][0]["sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SystemExit, match="package inventory does not match"):
        module.validated_offline_apt_evidence(root=root)


def test_manifest_evidence_rejects_packages_gzip_drift_even_when_receipt_is_updated(tmp_path):
    module, root, receipt_path, _packages, receipt = _write_manifest_fixture(tmp_path)
    packages_gz = root / "dist/autoinstall/drycontroller-apt/Packages.gz"
    packages_gz.write_bytes(gzip.compress(b"drifted\n", compresslevel=9, mtime=0))
    receipt["output"]["packages_gzip_sha256"] = _sha256(packages_gz.read_bytes())
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SystemExit, match="Packages.gz does not match Packages"):
        module.validated_offline_apt_evidence(root=root)


def test_manifest_evidence_rejects_final_packages_inventory_drift(tmp_path):
    module, root, receipt_path, packages, receipt = _write_manifest_fixture(tmp_path)
    packages.write_bytes(packages.read_bytes().replace(b"Package: curl\n", b"Package: drifted\n"))
    packages_gz = root / "dist/autoinstall/drycontroller-apt/Packages.gz"
    packages_gz.write_bytes(gzip.compress(packages.read_bytes(), compresslevel=9, mtime=0))
    receipt["output"]["packages_sha256"] = _sha256(packages.read_bytes())
    receipt["output"]["packages_gzip_sha256"] = _sha256(packages_gz.read_bytes())
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SystemExit, match="final Packages inventory does not match the pool"):
        module.validated_offline_apt_evidence(root=root)


def test_manifest_evidence_binds_the_only_extra_deb_to_the_tailscale_lock(tmp_path):
    tailscale_client = {
        "schema": "drycontroller.tailscale-client.v1",
        "version": "1.88.0",
        "architecture": "amd64",
        "deb_url": "https://pkgs.tailscale.com/stable/ubuntu/pool/tailscale_1.88.0_amd64.deb",
        "deb_sha256": _sha256(b"fixture:tailscale:1.88.0\n"),
    }
    module, root, _receipt_path, _packages, _receipt = _write_manifest_fixture(
        tmp_path, tailscale_client=tailscale_client
    )

    module.validated_offline_apt_evidence(root=root, tailscale_client=tailscale_client)
    with pytest.raises(SystemExit, match="does not match its client lock"):
        module.validated_offline_apt_evidence(
            root=root,
            tailscale_client={**tailscale_client, "deb_sha256": "0" * 64},
        )
