from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MAKER = ROOT / "tools" / "os-installer-usb-maker" / "os_installer_usb_maker.py"
MANIFEST = ROOT / "scripts" / "create-os-installer-manifest.py"


def _module():
    spec = importlib.util.spec_from_file_location("os_installer_usb_maker", MAKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest_module():
    spec = importlib.util.spec_from_file_location("create_os_installer_manifest", MANIFEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_iso_builder_refuses_direct_or_incomplete_invocation(tmp_path, monkeypatch):
    module = _module()
    base = tmp_path / "base.iso"
    base.write_bytes(b"iso")

    with pytest.raises(ValueError, match="build-os-installer-release"):
        module.build_iso(None, {"base_iso": base, "output_iso": tmp_path / "out.iso", "include_images": True})

    monkeypatch.setenv("DRYCONTROLLER_RELEASE_WRAPPER", "1")
    with pytest.raises(ValueError, match="complete signed image pair"):
        module.build_iso(
            None,
            {"base_iso": base, "output_iso": tmp_path / "out.iso", "include_images": False},
        )


def test_final_seed_verifier_uses_canonical_manifest_and_embedded_key(tmp_path, monkeypatch):
    module = _module()
    cosign = tmp_path / "cosign"
    cosign.write_bytes(b"cosign")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    manifest = {"z": 1, "a": "value"}
    module._verify_seed_signature(
        directory=tmp_path,
        prefix="host",
        manifest=manifest,
        signature="e30=",
        public_key=b"public-key",
        cosign=cosign,
    )

    assert (tmp_path / "host-manifest.json").read_bytes() == b'{"a":"value","z":1}'
    assert (tmp_path / "host-manifest.sigstore.json").read_bytes() == b"{}"
    assert (tmp_path / "host-cosign.pub").read_bytes() == b"public-key"
    assert calls[0][0][1:3] == ["verify-blob", "--key"]
    assert calls[0][1]["check"] is False
    assert calls[0][1]["timeout"] == 120


def test_final_seed_verifier_rejects_failed_cosign(tmp_path, monkeypatch):
    module = _module()
    cosign = tmp_path / "cosign"
    cosign.write_bytes(b"cosign")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    with pytest.raises(RuntimeError, match="host release signature invalid"):
        module._verify_seed_signature(
            directory=tmp_path,
            prefix="host",
            manifest={"a": 1},
            signature="e30=",
            public_key=b"public-key",
            cosign=cosign,
        )


def test_final_iso_verifier_can_use_host_cosign_without_materializing_linux_binary(tmp_path):
    module = _module()
    host_cosign = tmp_path / "cosign-host"
    host_cosign.write_bytes(b"host-native-cosign")
    host_cosign.chmod(0o700)

    selected = module._select_signature_verifier(
        directory=tmp_path,
        embedded_linux_cosign=b"linux-amd64-cosign",
        host_verifier=host_cosign,
    )

    assert selected == host_cosign.resolve()
    assert not (tmp_path / "cosign-linux-amd64").exists()


def test_iso_verifier_reuses_runtime_public_config_validator():
    module = _module()

    assert module.validate_public_config.__module__ == "installer_public_config"


def test_installer_host_and_software_signing_roots_must_all_differ(tmp_path):
    module = _manifest_module()
    installer = tmp_path / "installer.pub"
    host = tmp_path / "host.pub"
    software = tmp_path / "software.pub"
    installer.write_bytes(b"installer-root")
    host.write_bytes(b"host-root")
    software.write_bytes(b"software-root")

    module.validate_distinct_signing_roots(installer, host, software)
    software.write_bytes(installer.read_bytes())
    with pytest.raises(SystemExit, match="Installer/Host/Software"):
        module.validate_distinct_signing_roots(installer, host, software)


def test_iso_overlay_and_autoinstall_carry_canonical_license_trust():
    module = _module()
    mappings = dict(module.MEDIA_MAPS)
    user_data = (ROOT / "infra/autoinstall/user-data").read_text(encoding="utf-8")
    post_install = (ROOT / "infra/autoinstall/post-install.sh").read_text(encoding="utf-8")

    assert mappings["dist/autoinstall/license-trust.json"] == "/license-trust.json"
    assert "/cdrom/license-trust.json" in user_data
    assert "--license-trust" in post_install
