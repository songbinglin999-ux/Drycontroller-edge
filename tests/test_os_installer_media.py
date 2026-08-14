from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MAKER = ROOT / "tools" / "os-installer-usb-maker" / "os_installer_usb_maker.py"


def _module():
    spec = importlib.util.spec_from_file_location("os_installer_usb_maker", MAKER)
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
