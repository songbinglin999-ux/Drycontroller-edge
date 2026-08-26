#!/usr/bin/env python3
"""Canonical identity and receipt checks for the RB-8 Edge Gate C lane."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra" / "host-release-runner"))
from host_release_engine import COSIGN_TOOLCHAIN, canonical_json_bytes  # noqa: E402

MANIFEST_SCHEMA = "drycontroller.rb8.integration-manifest.v1"
APPROVAL_SCHEMA = "drycontroller.rb8.gate-c.edge-approval.v1"
RECEIPT_SCHEMA = "drycontroller.rb8.gate-c.edge-receipt.v1"
FAILURE_SCHEMA = "drycontroller.rb8.gate-c.edge-failure.v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{7,63}$")
REGISTRY_ORIGIN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?$")
REGISTRY_NAMESPACE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?"
    r"(?:/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)*$"
)
SERVICES = (
    "ai-ctrl-optimizer",
    "ai-softsensor",
    "alertmanager",
    "edge-api",
    "edge-daq",
    "edge-monitor",
    "influxdb",
    "license-service",
    "mosquitto",
    "mosquitto-init",
    "postgres",
    "prometheus",
)
PYTHON_SERVICES = {
    "ai-ctrl-optimizer",
    "ai-softsensor",
    "edge-api",
    "edge-daq",
    "edge-monitor",
    "license-service",
}
PYTHON_BASE_DIGEST = (
    "sha256:ff05d1a05204fb9f7444c435db8e8ec104e587a413280dc9ffc27a4797554182"
)
TOOLCHAIN_FIELDS = {
    "buildkit_image",
    "buildkit_version",
    "buildx_version",
    "cosign",
    "docker_client_version",
    "docker_server_version",
    "git_version",
    "python_version",
    "tar_version",
    "trivy",
    "trivy_db",
    "uname",
}
MAX_JSON_BYTES = 4 * 1024 * 1024


class GateCError(ValueError):
    pass


def canonical_bytes(value: object) -> bytes:
    return canonical_json_bytes(value)


def _need(condition: object, message: str) -> None:
    if not condition:
        raise GateCError(message)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _need(key not in result, "JSON input contains a duplicate key")
        result[key] = value
    return result


def _json_value(raw: bytes, label: str) -> Any:
    _need(0 < len(raw) <= MAX_JSON_BYTES, f"{label} is missing or too large")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateCError(f"{label} is not valid UTF-8 JSON") from exc


def _json(raw: bytes, label: str, *, canonical: bool) -> dict[str, Any]:
    value = _json_value(raw, label)
    _need(isinstance(value, dict), f"{label} must be one JSON object")
    _need(
        not canonical or raw == canonical_bytes(value),
        f"{label} bytes are not canonical",
    )
    return value


def parse_canonical(raw: bytes, label: str) -> dict[str, Any]:
    return _json(raw, label, canonical=True)


def _read(path: Path, label: str, *, canonical: bool = True) -> dict[str, Any]:
    try:
        return _json(path.read_bytes(), label, canonical=canonical)
    except OSError as exc:
        raise GateCError(f"{label} is unavailable") from exc


def _read_json_value(path: Path, label: str) -> Any:
    try:
        return _json_value(path.read_bytes(), label)
    except OSError as exc:
        raise GateCError(f"{label} is unavailable") from exc


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_sha(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _write_new(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(canonical_bytes(value))
    except FileExistsError as exc:
        raise GateCError(f"refusing to overwrite {path.name}") from exc
    path.chmod(0o600)


def validate_manifest(manifest: Mapping[str, Any]) -> dict[str, str]:
    _need(
        manifest.get("schema") == MANIFEST_SCHEMA, "Gate B manifest schema is invalid"
    )
    gate_a = manifest.get("gate_a")
    edge = gate_a.get("edge") if isinstance(gate_a, dict) else None
    fields = {"archive_sha256", "commit", "parent", "review_receipt_sha256", "tree"}
    _need(
        isinstance(edge, dict) and set(edge) == fields,
        "Gate B Edge identity is invalid",
    )
    for name in ("archive_sha256", "review_receipt_sha256"):
        _need(
            isinstance(edge[name], str) and SHA256.fullmatch(edge[name]),
            f"invalid Edge {name}",
        )
    for name in ("commit", "parent", "tree"):
        _need(
            isinstance(edge[name], str) and COMMIT.fullmatch(edge[name]),
            f"invalid Edge {name}",
        )
    _need(manifest.get("frontend") == "excluded", "Gate B Frontend boundary is invalid")
    _need(
        manifest.get("gate_sequence") == ["A", "B", "C", "D", "E", "F"],
        "Gate B sequence is invalid",
    )
    return {name: edge[name] for name in fields}


def validate_approval(
    approval: Mapping[str, Any], manifest_sha: str, run_id: str
) -> None:
    fields = {
        "controller_commit",
        "evidence_root",
        "gate_b_manifest_sha256",
        "registry",
        "run_id",
        "runner",
        "schema",
        "signing_keys",
        "toolchain",
    }
    _need(
        set(approval) == fields and approval.get("schema") == APPROVAL_SCHEMA,
        "invalid approval",
    )
    _need(
        approval.get("gate_b_manifest_sha256") == manifest_sha,
        "approval manifest mismatch",
    )
    _need(
        approval.get("run_id") == run_id and RUN_ID.fullmatch(run_id), "invalid run ID"
    )
    _need(
        COMMIT.fullmatch(str(approval.get("controller_commit", ""))),
        "invalid controller",
    )
    root = approval.get("evidence_root")
    _need(isinstance(root, str) and Path(root).is_absolute(), "invalid evidence root")
    runner = approval.get("runner")
    _need(
        isinstance(runner, dict)
        and set(runner) == {"arch", "name", "os"}
        and all(isinstance(value, str) and value for value in runner.values()),
        "invalid runner approval",
    )
    registry = approval.get("registry")
    _need(
        isinstance(registry, dict) and set(registry) == {"namespace", "origin"},
        "invalid registry",
    )
    origin, namespace = registry["origin"], registry["namespace"]
    _need(
        isinstance(origin, str) and REGISTRY_ORIGIN.fullmatch(origin),
        "invalid registry origin",
    )
    _need(
        isinstance(namespace, str) and REGISTRY_NAMESPACE.fullmatch(namespace),
        "invalid namespace",
    )
    suffix = f"rb8-gate-c-edge/{manifest_sha}/{run_id}"
    _need(
        namespace == suffix or namespace.endswith("/" + suffix),
        "namespace is not isolated",
    )
    keys = approval.get("signing_keys")
    _need(
        isinstance(keys, dict) and set(keys) == {"host", "receipt", "software"},
        "invalid keys",
    )
    _need(
        all(
            isinstance(value, str) and DIGEST.fullmatch(value)
            for value in keys.values()
        ),
        "invalid key digest",
    )
    _need(len(set(keys.values())) == 3, "signing roles are not separated")
    tools = approval.get("toolchain")
    _need(
        isinstance(tools, dict) and set(tools) == TOOLCHAIN_FIELDS, "invalid toolchain"
    )
    _need(
        tools.get("cosign") == COSIGN_TOOLCHAIN
        and isinstance(tools.get("trivy"), dict),
        "invalid scanner/signer",
    )
    _need(
        re.fullmatch(
            r"moby/buildkit@sha256:[0-9a-f]{64}", str(tools.get("buildkit_image", ""))
        ),
        "BuildKit image is mutable",
    )
    _need(
        str(tools.get("buildkit_version", "")).startswith("v"),
        "invalid BuildKit version",
    )
    database = tools.get("trivy_db")
    _need(
        isinstance(database, dict)
        and set(database) == {"metadata_sha256", "package_sha256", "package_url"}
        and str(database.get("package_url", "")).startswith("https://")
        and SHA256.fullmatch(str(database.get("metadata_sha256", "")))
        and SHA256.fullmatch(str(database.get("package_sha256", ""))),
        "invalid Trivy database",
    )
    for name in TOOLCHAIN_FIELDS - {
        "buildkit_image",
        "buildkit_version",
        "cosign",
        "trivy",
        "trivy_db",
    }:
        _need(
            isinstance(tools.get(name), str) and tools[name], f"empty approved {name}"
        )


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ("git", *args),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateCError("Git identity check failed") from exc


def _archive_sha(root: Path, commit: str) -> str:
    try:
        process = subprocess.Popen(
            ("git", "archive", "--format=tar", commit),
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert process.stdout is not None
        digest = hashlib.sha256()
        for block in iter(lambda: process.stdout.read(1024 * 1024), b""):
            digest.update(block)
        _, stderr = process.communicate(timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateCError("Git archive check failed") from exc
    _need(
        process.returncode == 0,
        f"Git archive failed: {stderr.decode(errors='replace')}",
    )
    return digest.hexdigest()


def source_identity(root: Path, manifest: Mapping[str, Any]) -> dict[str, str]:
    edge = validate_manifest(manifest)
    root = root.resolve(strict=True)
    actual = {
        "archive_sha256": _archive_sha(root, edge["commit"]),
        "commit": _git(root, "rev-parse", "HEAD"),
        "parent": _git(root, "rev-parse", "HEAD^"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
    }
    _need(
        not _git(root, "status", "--porcelain=v1"),
        "Gate C source checkout is not clean",
    )
    _need(
        actual == {name: edge[name] for name in actual}, "Gate C source is not Gate B"
    )
    return actual


def _run(*command: str) -> str:
    try:
        return subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=60
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise GateCError(f"toolchain check failed: {command[0]}") from exc


def actual_toolchain(
    approval: Mapping[str, Any], buildkit: str, metadata_sha: str
) -> dict[str, Any]:
    _need(SHA256.fullmatch(metadata_sha), "invalid Trivy metadata SHA-256")
    docker = _run(
        "docker", "version", "--format", "{{.Client.Version}}\\t{{.Server.Version}}"
    ).split("\t")
    _need(len(docker) == 2 and all(docker), "invalid Docker identity")
    _run("bash", str(ROOT / "scripts" / "ci" / "verify-cosign-toolchain.sh"))
    actual_trivy = json.loads(
        _run("python3", str(ROOT / "scripts" / "ci" / "verify-trivy-executable.py"))
    )
    approved = approval["toolchain"]
    return {
        "buildkit_image": approved["buildkit_image"],
        "buildkit_version": buildkit,
        "buildx_version": _run("docker", "buildx", "version"),
        "cosign": COSIGN_TOOLCHAIN,
        "docker_client_version": docker[0],
        "docker_server_version": docker[1],
        "git_version": _run("git", "--version"),
        "python_version": _run("python3", "--version"),
        "tar_version": _run("tar", "--version").splitlines()[0],
        "trivy": actual_trivy,
        "trivy_db": {**approved["trivy_db"], "metadata_sha256": metadata_sha},
        "uname": _run("uname", "-srmo"),
    }


def key_identities(evidence: Path, approval: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for role in ("host", "receipt", "software"):
        relative = f"keys/{role}-cosign.pub"
        path = evidence / relative
        _need(
            path.is_file() and not path.is_symlink() and path.stat().st_size <= 65536,
            "invalid public key",
        )
        digest = "sha256:" + _file_sha(path)
        _need(digest == approval["signing_keys"][role], f"unapproved {role} key")
        result[role] = {"path": relative, "sha256": digest}
    return result


def _bound(evidence: Path, relative: str, label: str) -> tuple[Path, str]:
    path_part = Path(relative)
    _need(
        not path_part.is_absolute() and ".." not in path_part.parts,
        f"unsafe {label} path",
    )
    path = evidence / path_part
    _need(path.is_file() and not path.is_symlink(), f"missing {label}")
    return path, "sha256:" + _file_sha(path)


def artifact_inventory(
    evidence: Path, approval: Mapping[str, Any], keys: Mapping[str, Any]
) -> list[dict[str, Any]]:
    path = evidence / "artifacts.tsv"
    _need(path.is_file() and not path.is_symlink(), "artifact inventory is missing")
    prefix = f"{approval['registry']['origin']}/{approval['registry']['namespace']}/"
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        _need(len(fields) == 7, "artifact inventory row is invalid")
        role, name, reference, image_id, scan_rel, signature_rel, base_rel = fields
        _need(role in {"host", "software"}, "invalid artifact role")
        _need(
            (role, name) == ("host", "host-bundle")
            or (role == "software" and name in SERVICES),
            "invalid artifact name",
        )
        repository, separator, manifest_digest = reference.rpartition("@")
        _need(
            separator == "@"
            and repository == f"{prefix}{name}"
            and DIGEST.fullmatch(manifest_digest),
            "mutable artifact",
        )
        _need(DIGEST.fullmatch(image_id), "invalid image ID")
        scan_path, scan_sha = _bound(evidence, scan_rel, "scan evidence")
        scan = _read(scan_path, "scan evidence", canonical=False)
        _need(
            scan.get("ArtifactID") == image_id
            and scan.get("ArtifactName") == image_id
            and scan.get("ArtifactType") == "container_image",
            "scan identity is not bound to artifact",
        )
        results = scan.get("Results")
        _need(isinstance(results, list) and results, "scan results are missing")
        for result in results:
            _need(isinstance(result, dict), "invalid scan result")
            findings = result.get("Vulnerabilities") or []
            _need(
                isinstance(findings, list)
                and all(isinstance(item, dict) for item in findings),
                "invalid scan findings",
            )
            _need(
                not any(
                    item.get("Severity") in {"HIGH", "CRITICAL"} for item in findings
                ),
                "scan failed",
            )
        signature_path, signature_sha = _bound(
            evidence, signature_rel, "signature evidence"
        )
        signatures = _read_json_value(signature_path, "signature evidence")
        _need(
            isinstance(signatures, list) and signatures,
            "signature evidence is invalid",
        )
        for verification in signatures:
            _need(isinstance(verification, dict), "signature evidence is invalid")
            critical = verification.get("critical")
            identity = critical.get("identity") if isinstance(critical, dict) else None
            image = critical.get("image") if isinstance(critical, dict) else None
            _need(
                isinstance(critical, dict)
                and critical.get("type") == "cosign container image signature"
                and isinstance(identity, dict)
                and identity.get("docker-reference") == repository
                and isinstance(image, dict)
                and image.get("docker-manifest-digest") == manifest_digest,
                "signature identity is not bound to artifact",
            )
        base: dict[str, str] | None = None
        if role == "software" and name in PYTHON_SERVICES:
            _need(base_rel != "-", "Python base evidence is missing")
            base_path, base_sha = _bound(evidence, base_rel, "Python base evidence")
            base_value = _read(base_path, "Python base evidence", canonical=False)
            _need(
                base_value.get("service") == name
                and base_value.get("image_id") == image_id
                and base_value.get("base_digest") == PYTHON_BASE_DIGEST
                and base_value.get("platform") == "linux/amd64",
                "Python base evidence is invalid",
            )
            base = {"evidence": base_rel, "evidence_sha256": base_sha}
        else:
            _need(base_rel == "-", "unexpected base evidence")
        records.append(
            {
                "base_image": base,
                "image_id": image_id,
                "name": name,
                "reference": reference,
                "role": role,
                "scan": {
                    "evidence": scan_rel,
                    "evidence_sha256": scan_sha,
                    "high_critical_exit_code": 0,
                },
                "signature": {
                    "evidence": signature_rel,
                    "evidence_sha256": signature_sha,
                    "public_key_sha256": keys[role]["sha256"],
                    "verified": True,
                },
            }
        )
    expected = {
        ("host", "host-bundle"),
        *(("software", service) for service in SERVICES),
    }
    _need(
        len(records) == len(expected)
        and {(r["role"], r["name"]) for r in records} == expected,
        "incomplete artifacts",
    )
    return sorted(records, key=lambda item: (item["role"], item["name"]))


def prepare(environ: Mapping[str, str]) -> tuple[Path, str]:
    manifest_sha = environ.get("GATE_B_MANIFEST_SHA256", "")
    approval_sha = environ.get("GATE_C_APPROVAL_SHA256", "")
    run_id = environ.get("GATE_C_RUN_ID", "")
    _need(
        SHA256.fullmatch(manifest_sha) and SHA256.fullmatch(approval_sha),
        "invalid input hash",
    )
    try:
        manifest_raw = base64.b64decode(
            environ.get("GATE_B_MANIFEST_BASE64", ""), validate=True
        )
        approval_raw = base64.b64decode(
            environ.get("GATE_C_APPROVAL_BASE64", ""), validate=True
        )
    except (binascii.Error, ValueError) as exc:
        raise GateCError("input is not strict base64") from exc
    _need(
        _sha(manifest_raw) == manifest_sha and _sha(approval_raw) == approval_sha,
        "input hash mismatch",
    )
    manifest = parse_canonical(manifest_raw, "Gate B manifest")
    approval = parse_canonical(approval_raw, "Gate C approval")
    edge = validate_manifest(manifest)
    validate_approval(approval, manifest_sha, run_id)
    root_raw = environ.get("GATE_C_EVIDENCE_ROOT", "")
    _need(approval["evidence_root"] == root_raw, "unapproved evidence root")
    root = Path(root_raw)
    _need(root.is_dir() and not root.is_symlink(), "evidence root is unavailable")
    controller = Path(environ.get("GATE_C_CONTROLLER_ROOT", "")).resolve(strict=True)
    _need(
        _git(controller, "rev-parse", "HEAD") == approval["controller_commit"],
        "unapproved controller",
    )
    _need(not _git(controller, "status", "--porcelain=v1"), "dirty controller")
    runner = {
        name: environ.get(f"RUNNER_{name.upper()}", "")
        for name in ("arch", "name", "os")
    }
    _need(runner == approval["runner"], "unapproved runner")
    _need(
        environ.get("HARBOR_HOST") == approval["registry"]["origin"],
        "unapproved registry",
    )
    workflow_run = environ.get("GATE_C_WORKFLOW_RUN_ID", "")
    _need(workflow_run, "missing workflow run identity")
    evidence = root.resolve(strict=True) / manifest_sha / run_id
    _need(
        not evidence.exists() and not evidence.is_symlink(),
        "run namespace already exists",
    )
    (evidence / "inputs").mkdir(parents=True, mode=0o700)
    (evidence / "inputs" / "integration-manifest.json").write_bytes(manifest_raw)
    (evidence / "inputs" / "gate-c-approval.json").write_bytes(approval_raw)
    _write_new(
        evidence / "prepared.json",
        {
            "approval_sha256": approval_sha,
            "controller_commit": approval["controller_commit"],
            "gate_b_manifest_sha256": manifest_sha,
            "run_id": run_id,
            "source_commit": edge["commit"],
            "workflow_run_id": workflow_run,
        },
    )
    output = Path(environ.get("GITHUB_OUTPUT", ""))
    _need(output.is_file(), "GITHUB_OUTPUT is unavailable")
    with output.open("a", encoding="utf-8") as handle:
        handle.write(f"candidate_commit={edge['commit']}\nevidence_dir={evidence}\n")
    return evidence, edge["commit"]


def build_receipt(
    evidence: Path, source_root: Path, buildkit: str, metadata_sha: str
) -> dict[str, Any]:
    manifest_path = evidence / "inputs" / "integration-manifest.json"
    approval_path = evidence / "inputs" / "gate-c-approval.json"
    manifest = _read(manifest_path, "Gate B manifest")
    approval = _read(approval_path, "Gate C approval")
    prepared = _read(evidence / "prepared.json", "preparation")
    _need(
        _file_sha(manifest_path) == prepared.get("gate_b_manifest_sha256"),
        "manifest drift",
    )
    _need(_file_sha(approval_path) == prepared.get("approval_sha256"), "approval drift")
    validate_approval(approval, prepared["gate_b_manifest_sha256"], prepared["run_id"])
    source = source_identity(source_root, manifest)
    _need(source["commit"] == prepared.get("source_commit"), "source drift")
    runner = {
        name: os.environ.get(f"RUNNER_{name.upper()}", "")
        for name in ("arch", "name", "os")
    }
    _need(runner == approval["runner"], "runner drift")
    tools = actual_toolchain(approval, buildkit, metadata_sha)
    _need(tools == approval["toolchain"], "toolchain drift")
    keys = key_identities(evidence, approval)
    artifacts = artifact_inventory(evidence, approval, keys)
    cleanup = _read(evidence / "cleanup.json", "cleanup")
    required_cleanup = {
        "builder_absent": True,
        "containers_absent": True,
        "credentials_absent": True,
        "local_images_absent": True,
        "temporary_archives_absent": True,
        "work_namespace_absent": True,
    }
    _need(cleanup == required_cleanup, "cleanup postconditions failed")
    topology_path = evidence / "topology.json"
    topology = _read(topology_path, "topology")
    _need(topology.get("service_inventory") == list(SERVICES), "invalid H/S topology")
    return {
        "approval_sha256": prepared["approval_sha256"],
        "artifacts": artifacts,
        "cleanup": cleanup,
        "controller": {"commit": prepared["controller_commit"]},
        "forbidden_mutations": {
            "cloud_api": False,
            "deploy": False,
            "gitea_handoff": False,
            "gitea_release": False,
        },
        "gate": "C",
        "gate_b": {
            "manifest_sha256": prepared["gate_b_manifest_sha256"],
            "schema": MANIFEST_SCHEMA,
        },
        "lane": "edge",
        "run": {
            "id": prepared["run_id"],
            "registry_namespace": approval["registry"]["namespace"],
            "workflow_run_id": prepared["workflow_run_id"],
            "zero_inheritance": True,
        },
        "runner": runner,
        "schema": RECEIPT_SCHEMA,
        "signing": keys,
        "source": source,
        "status": "pass",
        "toolchain": tools,
        "topology": {"sha256": "sha256:" + _file_sha(topology_path), "value": topology},
    }


def write_failure(
    evidence: Path, step: str, exit_code: int, cleanup: Path | None
) -> None:
    path = evidence / "failure.json"
    if path.exists():
        return
    prepared = (
        _read(evidence / "prepared.json", "preparation")
        if (evidence / "prepared.json").is_file()
        else None
    )
    cleanup_value = _read(cleanup, "cleanup") if cleanup and cleanup.is_file() else None
    _write_new(
        path,
        {
            "cleanup": cleanup_value,
            "exit_code": exit_code,
            "first_failed_step": step,
            "prepared": prepared,
            "schema": FAILURE_SCHEMA,
            "status": "fail",
        },
    )


def preserve_prepare_failure(environ: Mapping[str, str]) -> None:
    manifest_sha = environ.get("GATE_B_MANIFEST_SHA256", "")
    run_id = environ.get("GATE_C_RUN_ID", "")
    root = Path(environ.get("GATE_C_EVIDENCE_ROOT", ""))
    if not (
        SHA256.fullmatch(manifest_sha)
        and RUN_ID.fullmatch(run_id)
        and root.is_dir()
        and not root.is_symlink()
    ):
        return
    evidence = root.resolve(strict=True) / manifest_sha / run_id
    if evidence.is_dir() and (evidence / "prepared.json").is_file():
        prepared = _read(evidence / "prepared.json", "preparation")
        if prepared.get("workflow_run_id") == environ.get("GATE_C_WORKFLOW_RUN_ID"):
            write_failure(evidence, "prepare", 1, None)
        return
    if evidence.exists() or evidence.is_symlink():
        return
    evidence.mkdir(parents=True, mode=0o700)
    write_failure(evidence, "prepare", 1, None)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    source = commands.add_parser("verify-source")
    source.add_argument("--source-root", required=True, type=Path)
    source.add_argument("--manifest", required=True, type=Path)
    keys = commands.add_parser("verify-keys")
    keys.add_argument("--evidence-dir", required=True, type=Path)
    keys.add_argument("--approval", required=True, type=Path)
    tools = commands.add_parser("verify-toolchain")
    tools.add_argument("--approval", required=True, type=Path)
    tools.add_argument("--buildkit-version", required=True)
    tools.add_argument("--trivy-db-metadata-sha256", required=True)
    receipt = commands.add_parser("receipt")
    receipt.add_argument("--evidence-dir", required=True, type=Path)
    receipt.add_argument("--source-root", required=True, type=Path)
    receipt.add_argument("--buildkit-version", required=True)
    receipt.add_argument("--trivy-db-metadata-sha256", required=True)
    receipt.add_argument("--output", required=True, type=Path)
    failure = commands.add_parser("failure")
    failure.add_argument("--evidence-dir", required=True, type=Path)
    failure.add_argument("--step", required=True)
    failure.add_argument("--exit-code", required=True, type=int)
    failure.add_argument("--cleanup", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare(os.environ)
        elif args.command == "verify-source":
            source_identity(args.source_root, _read(args.manifest, "Gate B manifest"))
        elif args.command == "verify-keys":
            key_identities(args.evidence_dir, _read(args.approval, "Gate C approval"))
        elif args.command == "verify-toolchain":
            approval = _read(args.approval, "Gate C approval")
            _need(
                actual_toolchain(
                    approval, args.buildkit_version, args.trivy_db_metadata_sha256
                )
                == approval["toolchain"],
                "toolchain does not match approval",
            )
        elif args.command == "receipt":
            _write_new(
                args.output,
                build_receipt(
                    args.evidence_dir,
                    args.source_root,
                    args.buildkit_version,
                    args.trivy_db_metadata_sha256,
                ),
            )
        else:
            write_failure(args.evidence_dir, args.step, args.exit_code, args.cleanup)
    except (GateCError, OSError) as exc:
        if args.command == "prepare":
            preserve_prepare_failure(os.environ)
        print(f"Gate C rejected: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
