#!/usr/bin/env python3
"""Validate the fixed, local Ubuntu snapshot used by the OS Installer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


LOCK_SCHEMA = "drycontroller.offline-apt-input.v1"
PLAN_SCHEMA = "drycontroller.offline-apt-plan.v1"
BASE_REQUESTED_PACKAGES = frozenset(
    {
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
    }
)
COMPONENTS = frozenset({"main", "restricted", "universe", "multiverse"})
LOWER_SHA256 = re.compile(r"[0-9a-f]{64}")
PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9+.-]*")
PACKAGE_VERSION = re.compile(r"[A-Za-z0-9.+:~\-]+")
SNAPSHOT_PATH = re.compile(r"/ubuntu/[0-9]{8}T[0-9]{6}Z/")
SOURCE_SUITE = re.compile(r"noble(?:-updates|-security)?")


class ContractError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _regular_file(root: Path, relative: str, label: str) -> Path:
    if not isinstance(relative, str):
        raise ContractError(f"{label} path invalid")
    pure = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or pure.is_absolute()
        or pure.as_posix() != relative
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ContractError(f"{label} path invalid")
    path = root.joinpath(*pure.parts)
    try:
        cursor = root
        for part in pure.parts:
            cursor = cursor / part
            metadata = cursor.lstat()
            if cursor == path:
                if not stat.S_ISREG(metadata.st_mode):
                    raise ContractError(f"{label} must be a regular file")
            elif not stat.S_ISDIR(metadata.st_mode):
                raise ContractError(f"{label} parent path must be a directory")
        path.resolve().relative_to(root.resolve())
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ContractError(f"{label} path invalid") from exc
    return path


def _metadata_record(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"path", "size", "sha256"}:
        raise ContractError(f"{label} fields invalid")
    path = value["path"]
    size = value["size"]
    digest = value["sha256"]
    if (
        not isinstance(path, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1
        or not isinstance(digest, str)
        or LOWER_SHA256.fullmatch(digest) is None
    ):
        raise ContractError(f"{label} fields invalid")
    return value


def _verify_metadata_file(
    root: Path,
    value: object,
    *,
    label: str,
) -> tuple[dict[str, object], Path, bytes]:
    record = _metadata_record(value, label=label)
    path = _regular_file(root, str(record["path"]), label)
    raw = path.read_bytes()
    if len(raw) != record["size"]:
        raise ContractError(f"{label} size mismatch")
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise ContractError(f"{label} digest mismatch")
    return record, path, raw


def _fields(raw: bytes, *, label: str) -> dict[str, str]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ContractError(f"{label} is not UTF-8") from exc
    result: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        if line.startswith((" ", "\t")):
            if current is None:
                raise ContractError(f"{label} continuation is invalid")
            result[current] += "\n" + line[1:]
            continue
        key, separator, value = line.partition(":")
        if not separator or not key or key in result:
            raise ContractError(f"{label} fields invalid")
        current = key
        result[key] = value.lstrip()
    return result


def _paragraphs(raw: bytes, *, label: str) -> list[dict[str, str]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"{label} is not UTF-8") from exc
    paragraphs = [paragraph for paragraph in re.split(r"\n[ \t]*\n", text) if paragraph.strip()]
    if not paragraphs:
        raise ContractError(f"{label} is empty")
    return [
        _fields((paragraph.rstrip("\n") + "\n").encode(), label=label)
        for paragraph in paragraphs
    ]


def _release_sha256(raw: bytes) -> dict[str, tuple[str, int]]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ContractError("Release is not UTF-8") from exc
    entries: dict[str, tuple[str, int]] = {}
    in_sha256 = False
    for line in lines:
        if line == "SHA256:":
            in_sha256 = True
            continue
        if in_sha256 and not line.startswith((" ", "\t")):
            break
        if not in_sha256:
            continue
        parts = line.split()
        if len(parts) != 3 or LOWER_SHA256.fullmatch(parts[0]) is None:
            raise ContractError("Release SHA256 section invalid")
        try:
            size = int(parts[1])
        except ValueError as exc:
            raise ContractError("Release SHA256 section invalid") from exc
        if size < 0 or parts[2] in entries:
            raise ContractError("Release SHA256 section invalid")
        entries[parts[2]] = (parts[0], size)
    if not entries:
        raise ContractError("Release SHA256 section missing")
    return entries


def _validate_snapshot_uri(value: object) -> str:
    if not isinstance(value, str):
        raise ContractError("snapshot_uri must be one immutable Ubuntu snapshot URI")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "snapshot.ubuntu.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or SNAPSHOT_PATH.fullmatch(parsed.path) is None
    ):
        raise ContractError("snapshot_uri must be one immutable Ubuntu snapshot URI")
    return value


def _requested_packages(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ContractError("requested_packages lock is missing")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "version"}:
            raise ContractError("requested package fields invalid")
        name = item["name"]
        version = item["version"]
        if (
            not isinstance(name, str)
            or PACKAGE_NAME.fullmatch(name) is None
            or not isinstance(version, str)
            or PACKAGE_VERSION.fullmatch(version) is None
        ):
            raise ContractError("requested package fields invalid")
        result.append({"name": name, "version": version})
    names = [item["name"] for item in result]
    if len(set(names)) != len(names):
        raise ContractError("requested package lock contains duplicates")
    if set(names) != BASE_REQUESTED_PACKAGES:
        raise ContractError("requested package set does not match the OS Installer contract")
    return result


def _resolved_packages(value: object, snapshot_root: Path) -> list[dict[str, object]]:
    required_fields = {
        "name",
        "version",
        "architecture",
        "filename",
        "size",
        "sha256",
    }
    if not isinstance(value, list) or not value:
        raise ContractError("resolved_packages lock is missing")
    result: list[dict[str, object]] = []
    names: set[str] = set()
    filenames: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != required_fields:
            raise ContractError("resolved package fields invalid")
        name = item["name"]
        version = item["version"]
        architecture = item["architecture"]
        filename = item["filename"]
        size = item["size"]
        digest = item["sha256"]
        if (
            not isinstance(name, str)
            or PACKAGE_NAME.fullmatch(name) is None
            or name in names
            or not isinstance(version, str)
            or PACKAGE_VERSION.fullmatch(version) is None
            or not isinstance(architecture, str)
            or architecture not in {"amd64", "all"}
            or not isinstance(filename, str)
            or not filename.startswith("pool/")
            or not filename.endswith(".deb")
            or filename in filenames
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
            or not isinstance(digest, str)
            or LOWER_SHA256.fullmatch(digest) is None
        ):
            raise ContractError("resolved package fields invalid")
        path = _regular_file(snapshot_root, filename, "resolved package")
        if path.stat().st_size != size or _sha256(path) != digest:
            raise ContractError("resolved package artifact mismatch")
        names.add(name)
        filenames.add(filename)
        result.append(dict(item))
    return result


def _package_index_records(raw: bytes, *, label: str) -> list[dict[str, str]]:
    required = {"Package", "Version", "Architecture", "Filename", "Size", "SHA256"}
    records = _paragraphs(raw, label=label)
    if any(not required.issubset(record) for record in records):
        raise ContractError("Packages entry fields invalid")
    return records


def validate_contract(
    *,
    snapshot_root: Path,
    lock_path: Path,
    expected_lock_sha256: str,
) -> tuple[dict[str, object], bytes]:
    if LOWER_SHA256.fullmatch(expected_lock_sha256) is None:
        raise ContractError("lock SHA256 must be one lowercase SHA-256 digest")
    try:
        root_metadata = snapshot_root.lstat()
        lock_metadata = lock_path.lstat()
    except OSError as exc:
        raise ContractError("snapshot root or lock is missing") from exc
    if not stat.S_ISDIR(root_metadata.st_mode) or not stat.S_ISREG(lock_metadata.st_mode):
        raise ContractError("snapshot root and lock types are invalid")
    actual_lock_sha256 = _sha256(lock_path)
    if actual_lock_sha256 != expected_lock_sha256:
        raise ContractError("offline apt input lock digest mismatch")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("offline apt input lock is invalid JSON") from exc
    expected_fields = {
        "schema",
        "snapshot_uri",
        "suite",
        "architecture",
        "sources",
        "requested_packages",
        "resolved_packages",
    }
    if not isinstance(lock, dict) or set(lock) != expected_fields or lock.get("schema") != LOCK_SCHEMA:
        raise ContractError("offline apt input lock fields invalid")
    if lock["suite"] != "noble":
        raise ContractError("suite must be noble")
    if lock["architecture"] != "amd64":
        raise ContractError("architecture must be amd64")
    snapshot_uri = _validate_snapshot_uri(lock["snapshot_uri"])
    sources = lock["sources"]
    if not isinstance(sources, list) or not sources:
        raise ContractError("sources lock is missing")

    release_files: list[dict[str, object]] = []
    package_indexes: list[dict[str, object]] = []
    package_records: list[dict[str, str]] = []
    combined_packages = bytearray()
    source_suites: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "suite",
            "codename",
            "release",
            "packages",
        }:
            raise ContractError("source fields invalid")
        suite = source["suite"]
        codename = source["codename"]
        if (
            not isinstance(suite, str)
            or SOURCE_SUITE.fullmatch(suite) is None
            or suite in source_suites
            or codename != "noble"
        ):
            raise ContractError("source suite/codename invalid")
        source_suites.add(suite)
        release, _release_path, release_raw = _verify_metadata_file(
            snapshot_root,
            source["release"],
            label="Release",
        )
        if release["path"] != f"dists/{suite}/Release":
            raise ContractError("Release path does not match source suite")
        release_fields = _fields(release_raw, label="Release")
        if (
            release_fields.get("Suite") != suite
            or release_fields.get("Codename") != "noble"
            or "amd64" not in release_fields.get("Architectures", "").split()
        ):
            raise ContractError("Release suite/codename/architecture mismatch")
        release_hashes = _release_sha256(release_raw)
        release_files.append(dict(release))
        indexes = source["packages"]
        if not isinstance(indexes, list) or not indexes:
            raise ContractError("Packages indexes lock is missing")
        seen_components: set[str] = set()
        for index in indexes:
            if not isinstance(index, dict) or set(index) != {
                "component",
                "path",
                "size",
                "sha256",
            }:
                raise ContractError("Packages index fields invalid")
            component = index["component"]
            if (
                not isinstance(component, str)
                or component not in COMPONENTS
                or component in seen_components
            ):
                raise ContractError("Packages component invalid")
            seen_components.add(str(component))
            metadata = {key: index[key] for key in ("path", "size", "sha256")}
            packages, _packages_path, packages_raw = _verify_metadata_file(
                snapshot_root,
                metadata,
                label="Packages",
            )
            expected_path = f"dists/{suite}/{component}/binary-amd64/Packages"
            if packages["path"] != expected_path:
                raise ContractError("Packages path does not match source suite/component/architecture")
            release_relative = f"{component}/binary-amd64/Packages"
            if release_hashes.get(release_relative) != (packages["sha256"], packages["size"]):
                raise ContractError("Packages digest/size is not bound by Release")
            package_indexes.append({"suite": suite, "component": component, **packages})
            package_records.extend(
                _package_index_records(packages_raw, label=f"Packages {suite}/{component}")
            )
            if combined_packages:
                combined_packages.extend(b"\n")
            combined_packages.extend(packages_raw.rstrip(b"\n") + b"\n")
    if "noble" not in source_suites:
        raise ContractError("sources must include noble")

    requested = _requested_packages(lock["requested_packages"])
    resolved = _resolved_packages(lock["resolved_packages"], snapshot_root)
    resolved_by_name = {str(item["name"]): item for item in resolved}
    if any(
        name not in resolved_by_name or resolved_by_name[name]["version"] != request["version"]
        for request in requested
        for name in (request["name"],)
    ):
        raise ContractError("requested package version is absent from resolved_packages")
    for package in resolved:
        matches = [
            record
            for record in package_records
            if record.get("Package") == package["name"]
            and record.get("Version") == package["version"]
            and record.get("Architecture") == package["architecture"]
            and record.get("Filename") == package["filename"]
            and record.get("Size") == str(package["size"])
            and record.get("SHA256") == package["sha256"]
        ]
        if not matches:
            raise ContractError("resolved package is not exactly bound by Packages metadata")

    plan = {
        "schema": PLAN_SCHEMA,
        "lock_sha256": actual_lock_sha256,
        "snapshot_uri": snapshot_uri,
        "suite": "noble",
        "architecture": "amd64",
        "release_files": release_files,
        "package_indexes": package_indexes,
        "requested_packages": requested,
        "resolved_packages": resolved,
        "combined_packages_sha256": hashlib.sha256(combined_packages).hexdigest(),
    }
    return plan, bytes(combined_packages)


def verify_solver(plan_path: Path, solver_output_path: Path) -> None:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        output = solver_output_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("solver plan or output is unreadable") from exc
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise ContractError("solver plan is invalid")
    expected = {
        (str(item["name"]), str(item["version"]))
        for item in plan.get("resolved_packages", [])
        if isinstance(item, dict)
    }
    actual: set[tuple[str, str]] = set()
    expression = re.compile(r"^Inst\s+(\S+?)(?:\s+\[[^]]+\])?\s+\((\S+)")
    for line in output.splitlines():
        match = expression.match(line)
        if match is None:
            continue
        name = re.sub(r":(?:amd64|all)$", "", match.group(1))
        actual.add((name, match.group(2)))
    if not actual or actual != expected:
        raise ContractError("dependency resolution differs from resolved_packages lock")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--snapshot-root", required=True, type=Path)
    validate.add_argument("--lock", required=True, type=Path)
    validate.add_argument("--lock-sha256", required=True)
    validate.add_argument("--plan-out", required=True, type=Path)
    validate.add_argument("--combined-packages-out", required=True, type=Path)
    solver = subparsers.add_parser("verify-solver")
    solver.add_argument("--plan", required=True, type=Path)
    solver.add_argument("--solver-output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            plan, packages = validate_contract(
                snapshot_root=args.snapshot_root,
                lock_path=args.lock,
                expected_lock_sha256=args.lock_sha256,
            )
            _write_new(args.plan_out, _canonical_bytes(plan))
            _write_new(args.combined_packages_out, packages)
        else:
            verify_solver(args.plan, args.solver_output)
    except (ContractError, OSError) as exc:
        print(f"offline apt input invalid: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
