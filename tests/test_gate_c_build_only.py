from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ci" / "gate_c_receipt.py"
BUILD = ROOT / "scripts" / "ci" / "build-edge-gate-c.sh"
WORKFLOW = ROOT / ".gitea" / "workflows" / "edge-gate-c-build.yml"
SPEC = importlib.util.spec_from_file_location("gate_c_receipt", HELPER)
assert SPEC and SPEC.loader
gate_c = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate_c)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gate_c.canonical_bytes(value))


def _manifest(edge: dict[str, str]) -> dict[str, object]:
    return {
        "frontend": "excluded",
        "gate_a": {"edge": {**edge, "review_receipt_sha256": "5" * 64}},
        "gate_sequence": ["A", "B", "C", "D", "E", "F"],
        "schema": gate_c.MANIFEST_SCHEMA,
    }


def _approval(root: Path, manifest_sha: str) -> dict[str, object]:
    run_id = "edge-gate-c-0001"
    return {
        "controller_commit": "9" * 40,
        "evidence_root": str(root),
        "gate_b_manifest_sha256": manifest_sha,
        "registry": {
            "namespace": f"dry/rb8-gate-c-edge/{manifest_sha}/{run_id}",
            "origin": "registry.example.test:5443",
        },
        "run_id": run_id,
        "runner": {"arch": "X64", "name": "gate-c-runner", "os": "Linux"},
        "schema": gate_c.APPROVAL_SCHEMA,
        "signing_keys": {
            "host": "sha256:" + "1" * 64,
            "receipt": "sha256:" + "3" * 64,
            "software": "sha256:" + "2" * 64,
        },
        "toolchain": {
            "buildkit_image": "moby/buildkit@sha256:" + "4" * 64,
            "buildkit_version": "v0.24.0",
            "buildx_version": "buildx v0.27.0",
            "cosign": gate_c.COSIGN_TOOLCHAIN,
            "docker_client_version": "28.3.3",
            "docker_server_version": "28.3.3",
            "git_version": "git version 2.50.1",
            "python_version": "Python 3.11.13",
            "tar_version": "tar (GNU tar) 1.35",
            "trivy": {"sha256": "5" * 64, "version": "0.70.0"},
            "trivy_db": {
                "metadata_sha256": "6" * 64,
                "package_sha256": "7" * 64,
                "package_url": "https://packages.example.test/trivy-db.tar.gz",
            },
            "uname": "Linux 6.8.0 x86_64 GNU/Linux",
        },
    }


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_source_identity_binds_exact_gate_b_object_and_cleanliness(tmp_path: Path):
    repository = tmp_path / "candidate"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "gate-c@example.test")
    _git(repository, "config", "user.name", "Gate C")
    source = repository / "source.txt"
    for value in ("parent\n", "candidate\n"):
        source.write_text(value, encoding="utf-8")
        _git(repository, "add", "source.txt")
        _git(repository, "commit", "-m", value.strip())
    commit = _git(repository, "rev-parse", "HEAD")
    archive = subprocess.run(
        ("git", "archive", "--format=tar", commit),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    edge = {
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "commit": commit,
        "parent": _git(repository, "rev-parse", "HEAD^"),
        "tree": _git(repository, "rev-parse", "HEAD^{tree}"),
    }
    assert gate_c.source_identity(repository, _manifest(edge)) == edge
    source.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(gate_c.GateCError, match="not clean"):
        gate_c.source_identity(repository, _manifest(edge))


def test_canonical_inputs_and_run_namespace_fail_closed(tmp_path: Path):
    with pytest.raises(gate_c.GateCError, match="not canonical"):
        gate_c.parse_canonical(b'{"b":2, "a":1}', "input")
    with pytest.raises(gate_c.GateCError, match="duplicate"):
        gate_c.parse_canonical(b'{"a":1,"a":2}', "input")
    approval = _approval(tmp_path, "8" * 64)
    approval["registry"]["namespace"] = "dry/shared"
    with pytest.raises(gate_c.GateCError, match="not isolated"):
        gate_c.validate_approval(approval, "8" * 64, approval["run_id"])

    environment = {
        **os.environ,
        "GATE_B_MANIFEST_BASE64": "not-base64",
        "GATE_B_MANIFEST_SHA256": "8" * 64,
        "GATE_C_APPROVAL_SHA256": "9" * 64,
        "GATE_C_EVIDENCE_ROOT": str(tmp_path),
        "GATE_C_RUN_ID": "edge-gate-c-failed",
    }
    result = subprocess.run(
        ("python3", str(HELPER), "prepare"), env=environment, capture_output=True
    )
    failure_raw = (tmp_path / ("8" * 64) / "edge-gate-c-failed" / "failure.json").read_bytes()
    assert result.returncode == 1
    assert failure_raw == gate_c.canonical_bytes(json.loads(failure_raw))


def test_artifact_inventory_rejects_mutable_high_and_incomplete(tmp_path: Path):
    approval = _approval(tmp_path, "8" * 64)
    keys = {role: {"sha256": digest} for role, digest in approval["signing_keys"].items()}
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    inventory = evidence / "artifacts.tsv"
    prefix = f"{approval['registry']['origin']}/{approval['registry']['namespace']}"
    row = ["software", "alertmanager", f"{prefix}/alertmanager:latest", "sha256:" + "4" * 64, "scans/a.json", "signatures/a.json", "-"]
    inventory.write_text("\t".join(row) + "\n", encoding="utf-8")
    with pytest.raises(gate_c.GateCError, match="mutable"):
        gate_c.artifact_inventory(evidence, approval, keys)

    row[2] = f"{prefix}/alertmanager@sha256:" + "5" * 64
    _write(evidence / "scans" / "a.json", {"Results": []})
    (evidence / "signatures").mkdir()
    (evidence / "signatures" / "a.json").write_text("[]", encoding="utf-8")
    inventory.write_text("\t".join(row) + "\n", encoding="utf-8")
    with pytest.raises(gate_c.GateCError, match="incomplete"):
        gate_c.artifact_inventory(evidence, approval, keys)

    _write(
        evidence / "scans" / "a.json",
        {"Results": [{"Vulnerabilities": [{"Severity": "HIGH"}]}]},
    )
    with pytest.raises(gate_c.GateCError, match="scan failed"):
        gate_c.artifact_inventory(evidence, approval, keys)


def test_receipt_binds_policy_and_requires_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    evidence = tmp_path / "evidence"
    edge = {"archive_sha256": "a" * 64, "commit": "b" * 40, "parent": "c" * 40, "tree": "d" * 40}
    manifest = _manifest(edge)
    manifest_sha = hashlib.sha256(gate_c.canonical_bytes(manifest)).hexdigest()
    approval = _approval(tmp_path, manifest_sha)
    approval_sha = hashlib.sha256(gate_c.canonical_bytes(approval)).hexdigest()
    _write(evidence / "inputs" / "integration-manifest.json", manifest)
    _write(evidence / "inputs" / "gate-c-approval.json", approval)
    _write(
        evidence / "prepared.json",
        {
            "approval_sha256": approval_sha,
            "controller_commit": approval["controller_commit"],
            "gate_b_manifest_sha256": manifest_sha,
            "run_id": approval["run_id"],
            "source_commit": edge["commit"],
            "workflow_run_id": "123-1",
        },
    )
    cleanup = {name: True for name in ("builder_absent", "containers_absent", "credentials_absent", "local_images_absent", "temporary_archives_absent", "work_namespace_absent")}
    _write(evidence / "cleanup.json", cleanup)
    _write(evidence / "topology.json", {"service_inventory": list(gate_c.SERVICES)})
    monkeypatch.setattr(gate_c, "source_identity", lambda *_: edge)
    monkeypatch.setattr(gate_c, "actual_toolchain", lambda *_: approval["toolchain"])
    monkeypatch.setattr(gate_c, "key_identities", lambda *_: approval["signing_keys"])
    monkeypatch.setattr(gate_c, "artifact_inventory", lambda *_: ["artifacts"])
    for name, value in approval["runner"].items():
        monkeypatch.setenv(f"RUNNER_{name.upper()}", value)
    receipt = gate_c.build_receipt(evidence, tmp_path, "v0.24.0", "6" * 64)
    assert receipt["run"]["zero_inheritance"] is True
    assert receipt["source"] == edge
    assert receipt["forbidden_mutations"] == {
        "cloud_api": False,
        "deploy": False,
        "gitea_handoff": False,
        "gitea_release": False,
    }
    cleanup["local_images_absent"] = False
    _write(evidence / "cleanup.json", cleanup)
    with pytest.raises(gate_c.GateCError, match="cleanup postconditions"):
        gate_c.build_receipt(evidence, tmp_path, "v0.24.0", "6" * 64)


def test_formal_entry_is_isolated_build_only_and_signs_after_cleanup():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    script = BUILD.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow and "push:" not in workflow
    assert "contents: read" in workflow and "releases: write" not in workflow
    assert "actions/upload-artifact" not in workflow
    assert "build-host-release.sh" not in workflow
    assert "build-software-release.sh" not in workflow
    forbidden = ("CLOUD_OPS_RELEASE_API", "prepare-release-handoff", "complete-cloud", "docker compose up", "kubectl")
    assert all(value not in workflow and value not in script for value in forbidden)
    assert "--no-cache" in script and "--pull" in script
    assert script.index("verify-image") < script.index('failure_step="software_${service}_push_sign"')
    assert script.index('failure_step="cleanup"') < script.index('failure_step="canonical_receipt"') < script.index("cosign sign-blob")
