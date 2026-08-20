from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = ROOT / "scripts/ci/verify-trivy-executable.py"
EXPECTED_PATH = Path("/usr/local/bin/trivy")
EXPECTED_VERSION = "0.70.0"
EXPECTED_SHA256 = "379d59f24a4a828c55de5f0b91b6805cc35d13580180b658820e648611256166"


def _load_verifier():
    assert VERIFIER_PATH.is_file(), "formal release Trivy identity verifier is missing"
    spec = importlib.util.spec_from_file_location("verify_trivy_executable", VERIFIER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_trivy(path: Path, *, version: str = EXPECTED_VERSION, exit_code: int = 0) -> None:
    path.write_text(
        "#!/bin/sh\n"
        f"printf 'Version: {version}\\n'\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _bind_test_identity(module, binary: Path, *, sha256: str | None = None) -> None:
    module.TRIVY_BINARY = binary
    module.TRIVY_REQUIRED_UID = os.getuid()
    module.TRIVY_REQUIRED_GID = os.getgid()
    module.TRIVY_REQUIRED_MODE = 0o755
    module.TRIVY_BINARY_SHA256 = sha256 or hashlib.sha256(binary.read_bytes()).hexdigest()


def test_wrong_trivy_executable_is_rejected(tmp_path: Path):
    module = _load_verifier()
    binary = tmp_path / "trivy"
    _fake_trivy(binary, exit_code=9)
    _bind_test_identity(module, binary)

    with pytest.raises(ValueError, match="version check failed"):
        module.verify_trivy_executable()


def test_trivy_symlink_is_rejected(tmp_path: Path):
    module = _load_verifier()
    target = tmp_path / "real-trivy"
    binary = tmp_path / "trivy"
    _fake_trivy(target)
    binary.symlink_to(target)
    _bind_test_identity(module, target)
    module.TRIVY_BINARY = binary

    with pytest.raises(ValueError, match="regular file"):
        module.verify_trivy_executable()


def test_group_writable_trivy_mode_is_rejected(tmp_path: Path):
    module = _load_verifier()
    binary = tmp_path / "trivy"
    _fake_trivy(binary)
    binary.chmod(0o775)
    _bind_test_identity(module, binary)

    with pytest.raises(ValueError, match="owner or mode"):
        module.verify_trivy_executable()


def test_wrong_trivy_owner_is_rejected(tmp_path: Path):
    module = _load_verifier()
    binary = tmp_path / "trivy"
    _fake_trivy(binary)
    _bind_test_identity(module, binary)
    module.TRIVY_REQUIRED_UID = os.getuid() + 1

    with pytest.raises(ValueError, match="owner or mode"):
        module.verify_trivy_executable()


def test_wrong_trivy_version_is_rejected(tmp_path: Path):
    module = _load_verifier()
    binary = tmp_path / "trivy"
    _fake_trivy(binary, version="0.69.1")
    _bind_test_identity(module, binary)

    with pytest.raises(ValueError, match="version is not approved"):
        module.verify_trivy_executable()


def test_wrong_trivy_hash_is_rejected(tmp_path: Path):
    module = _load_verifier()
    binary = tmp_path / "trivy"
    _fake_trivy(binary)
    _bind_test_identity(module, binary, sha256="0" * 64)

    with pytest.raises(ValueError, match="SHA-256 is not approved"):
        module.verify_trivy_executable()


def test_path_shadow_cannot_replace_fixed_trivy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_verifier()
    binary = tmp_path / "approved-trivy"
    _fake_trivy(binary)
    _bind_test_identity(module, binary)

    marker = tmp_path / "shadow-ran"
    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir()
    shadow = shadow_dir / "trivy"
    shadow.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n", encoding="utf-8")
    shadow.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shadow_dir}:{os.environ['PATH']}")

    receipt = module.verify_trivy_executable()

    assert receipt["path"] == str(binary)
    assert receipt["version"] == EXPECTED_VERSION
    assert receipt["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert not marker.exists()


def test_formal_builders_share_fixed_trivy_contract_and_ignore_path_shadow():
    host = (ROOT / "scripts/ci/build-host-release.sh").read_text(encoding="utf-8")
    software = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")

    for builder in (host, software):
        verify = builder.index("python3 scripts/ci/verify-trivy-executable.py")
        first_scan = builder.index('"${TRIVY_BINARY}" image')
        assert verify < first_scan
        assert f'TRIVY_BINARY="{EXPECTED_PATH}"' in builder
        assert "command -v trivy" not in builder
        assert "\ntrivy image" not in builder
        assert "\n  trivy image" not in builder


def test_formal_workflows_establish_same_identity_and_clean_trivy_state_on_failure():
    for relative in (
        ".gitea/workflows/edge-host-release.yml",
        ".gitea/workflows/edge-software-release.yml",
    ):
        workflow = (ROOT / relative).read_text(encoding="utf-8")
        establish = workflow.index("Establish pinned Trivy executable")
        checkout = workflow.index("      - name: Checkout", establish)
        prime = workflow.index("Prime pinned Trivy database")
        builder = workflow.index("bash scripts/ci/build-")
        cleanup = workflow.index("Remove pinned Trivy state")

        assert establish < checkout < prime < builder < cleanup
        cleanup_block = workflow[cleanup:]
        assert "if: ${{ always() }}" in cleanup_block
        assert 'rm -rf -- "${TRIVY_CACHE_DIR}"' in cleanup_block
        assert 'rm -f -- "${TRIVY_DB_PACKAGE}"' in cleanup_block
        assert 'test ! -e "${TRIVY_CACHE_DIR}"' in cleanup_block
        assert 'test ! -e "${TRIVY_DB_PACKAGE}"' in cleanup_block
        assert f"{EXPECTED_SHA256}  {EXPECTED_PATH}" in workflow
        assert f"Version: {EXPECTED_VERSION}" in workflow
        establish_block = workflow[establish:prime]
        assert f"test -f {EXPECTED_PATH}" in establish_block
        assert f"test ! -L {EXPECTED_PATH}" in establish_block
        assert f"/usr/bin/chown root:root {EXPECTED_PATH}" in establish_block
        assert f"/usr/bin/chmod 0755 {EXPECTED_PATH}" in establish_block

        parsed = yaml.safe_load(workflow)
        builder_step = next(
            step
            for step in parsed["jobs"]["release"]["steps"]
            if step.get("name", "").startswith("Build and complete")
        )
        assert builder_step["env"]["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def test_formal_workflow_failure_cleanup_removes_pinned_trivy_state(tmp_path: Path):
    for name in ("edge-host-release.yml", "edge-software-release.yml"):
        workflow = yaml.safe_load(
            (ROOT / ".gitea/workflows" / name).read_text(encoding="utf-8")
        )
        cleanup = next(
            step
            for step in workflow["jobs"]["release"]["steps"]
            if step.get("name") == "Remove pinned Trivy state"
        )
        cache_dir = tmp_path / name / "cache"
        cache_dir.mkdir(parents=True)
        (cache_dir / "residue").write_text("db", encoding="utf-8")
        package = tmp_path / name / "trivy-db.tar.gz"
        package.write_text("package", encoding="utf-8")

        completed = subprocess.run(
            ["/bin/bash", "-c", cleanup["run"]],
            check=False,
            capture_output=True,
            env={
                **os.environ,
                "TRIVY_CACHE_DIR": str(cache_dir),
                "TRIVY_DB_PACKAGE": str(package),
            },
            text=True,
        )

        assert completed.returncode == 0, completed.stderr
        assert not cache_dir.exists()
        assert not package.exists()
