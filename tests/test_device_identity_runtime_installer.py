from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "infra/bootstrap/install-device-identity-runtime.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "device_identity_installer", INSTALLER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheelhouse(tmp_path: Path) -> tuple[Path, str]:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir(mode=0o755)
    wheels = {
        "first-1.0-py3-none-any.whl": b"first-wheel",
        "second-2.0-py3-none-any.whl": b"second-wheel",
    }
    lines = []
    for name, content in wheels.items():
        (wheelhouse / name).write_bytes(content)
        lines.append(f"{hashlib.sha256(content).hexdigest()}  ./{name}")
    manifest = ("\n".join(lines) + "\n").encode()
    (wheelhouse / "SHA256SUMS").write_bytes(manifest)
    return wheelhouse, hashlib.sha256(manifest).hexdigest()


def _fake_runner(calls: list[tuple[list[str], dict[str, str]]]):
    def run(args: list[str], environment: dict[str, str]) -> None:
        calls.append((args, environment))
        if args[1:3] == ["-m", "venv"]:
            python = Path(args[3]) / "bin/python"
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\nexit 0\n")
            python.chmod(0o755)
        elif "install" in args:
            python = Path(args[0])
            uvicorn = python.with_name("uvicorn")
            uvicorn.write_text(f"#!{python}\nexit 0\n")
            uvicorn.chmod(0o755)

    return run


def _inputs(tmp_path: Path) -> dict[str, object]:
    wheelhouse, manifest_sha256 = _wheelhouse(tmp_path)
    return {
        "repo_source": ROOT,
        "wheelhouse": wheelhouse,
        "approved_wheelhouse_manifest_sha256": manifest_sha256,
        "owner_uid": os.geteuid(),
        "owner_gid": os.getegid(),
    }


def test_installer_creates_only_the_identity_runtime_from_approved_wheels(
    tmp_path: Path,
):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir(mode=0o755)
    calls: list[tuple[list[str], dict[str, str]]] = []

    module.install_device_identity_runtime(
        **_inputs(tmp_path),
        root=destination,
        command_runner=_fake_runner(calls),
    )

    installed = destination / "opt/drycontroller-identity"
    assert installed.is_dir()
    assert not (installed / "wheelhouse").exists()
    for source, (target, _mode) in module.RUNTIME_FILES.items():
        assert (installed / target).read_bytes() == (ROOT / source).read_bytes()
    assert [call[0][1:3] for call in calls] == [
        ["-m", "venv"],
        ["-m", "pip"],
        ["-m", "pip"],
        ["-c", "from identity_service.backend import TpmIdentityBackend"],
        ["--version"],
    ]
    assert "--no-index" in calls[1][0]
    staging = Path(calls[0][0][3]).parent
    assert staging.name.startswith(module.STAGING_PREFIX)
    assert calls[3][1]["PYTHONPATH"] == str(installed)
    assert (installed / "venv/bin/uvicorn").read_text().splitlines()[0] == (
        f"#!{installed}/venv/bin/python"
    )
    assert not staging.exists()


def test_installer_rejects_unapproved_manifest_before_writing(tmp_path: Path):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir(mode=0o755)
    inputs = _inputs(tmp_path)
    inputs["approved_wheelhouse_manifest_sha256"] = "0" * 64

    with pytest.raises(module.RuntimeInstallError, match="manifest is not approved"):
        module.install_device_identity_runtime(
            **inputs,
            root=destination,
            command_runner=_fake_runner([]),
        )

    assert list(destination.iterdir()) == []


def test_installer_rejects_symlinked_wheelhouse_before_writing(tmp_path: Path):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir(mode=0o755)
    inputs = _inputs(tmp_path)
    wheelhouse_link = tmp_path / "wheelhouse-link"
    wheelhouse_link.symlink_to(inputs["wheelhouse"], target_is_directory=True)
    inputs["wheelhouse"] = wheelhouse_link

    with pytest.raises(module.RuntimeInstallError, match="directory is unsafe"):
        module.install_device_identity_runtime(
            **inputs,
            root=destination,
            command_runner=_fake_runner([]),
        )

    assert list(destination.iterdir()) == []


def test_installer_rejects_symlinked_repo_source_before_writing(tmp_path: Path):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir(mode=0o755)
    inputs = _inputs(tmp_path)
    repo_link = tmp_path / "repo-link"
    repo_link.symlink_to(ROOT, target_is_directory=True)
    inputs["repo_source"] = repo_link

    with pytest.raises(module.RuntimeInstallError, match="source directory is unsafe"):
        module.install_device_identity_runtime(
            **inputs,
            root=destination,
            command_runner=_fake_runner([]),
        )

    assert list(destination.iterdir()) == []


def test_installer_rejects_existing_runtime_without_modifying_it(tmp_path: Path):
    module = _load_installer()
    destination = tmp_path / "root"
    existing = destination / "opt/drycontroller-identity"
    existing.mkdir(parents=True)
    marker = existing / "marker"
    marker.write_text("existing")

    with pytest.raises(module.RuntimeInstallError, match="already exists"):
        module.install_device_identity_runtime(
            **_inputs(tmp_path),
            root=destination,
            command_runner=_fake_runner([]),
        )

    assert marker.read_text() == "existing"


def test_installer_failure_removes_only_its_staging_directory(tmp_path: Path):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir(mode=0o755)

    def fail(_args: list[str], _environment: dict[str, str]) -> None:
        raise module.RuntimeInstallError("expected test failure")

    with pytest.raises(module.RuntimeInstallError, match="expected test failure"):
        module.install_device_identity_runtime(
            **_inputs(tmp_path),
            root=destination,
            command_runner=fail,
        )

    assert not (destination / "opt/drycontroller-identity").exists()
    assert not any(
        path.name.startswith(module.STAGING_PREFIX)
        for path in (destination / "opt").iterdir()
    )


def test_installer_directory_fsync_failure_removes_renamed_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_installer()
    destination = tmp_path / "root"
    destination.mkdir(mode=0o755)

    def fail_fsync(_path: Path) -> None:
        raise OSError("expected directory fsync failure")

    monkeypatch.setattr(module, "_fsync_directory", fail_fsync)
    with pytest.raises(OSError, match="expected directory fsync failure"):
        module.install_device_identity_runtime(
            **_inputs(tmp_path),
            root=destination,
            command_runner=_fake_runner([]),
        )

    assert not (destination / "opt/drycontroller-identity").exists()
    assert not any(
        path.name.startswith(module.STAGING_PREFIX)
        for path in (destination / "opt").iterdir()
    )
