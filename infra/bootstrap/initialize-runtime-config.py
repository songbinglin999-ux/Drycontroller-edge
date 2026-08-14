#!/usr/bin/python3
"""Create per-device runtime secrets from public installer configuration."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path


BOOTSTRAP_DIR = Path(__file__).resolve().parent
if str(BOOTSTRAP_DIR) not in sys.path:
    sys.path.insert(0, str(BOOTSTRAP_DIR))

from installer_public_config import validate_public_config  # noqa: E402


PLACEHOLDER = re.compile(r"change_me_[A-Za-z0-9_]+")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _temporary_file(path: Path, data: bytes, mode: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        handle = os.fdopen(descriptor, "wb")
        descriptor = -1
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    temporary = _temporary_file(path, data, mode)
    try:
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_create(path: Path, data: bytes, mode: int) -> bool:
    temporary = _temporary_file(path, data, mode)
    try:
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            return False
        os.chmod(path, mode)
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _parse_env(contents: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in contents.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or ENV_NAME.fullmatch(name) is None or name in fields:
            raise ValueError("runtime environment structure invalid")
        fields[name] = value
    return fields


def _validate_placeholder_value(
    template_value: str,
    actual_value: str,
    generated: dict[str, str],
) -> None:
    placeholders = list(PLACEHOLDER.finditer(template_value))
    pattern: list[str] = []
    offset = 0
    for placeholder in placeholders:
        pattern.append(re.escape(template_value[offset : placeholder.start()]))
        pattern.append("([0-9a-f]{64})")
        offset = placeholder.end()
    pattern.append(re.escape(template_value[offset:]))
    match = re.fullmatch("".join(pattern), actual_value)
    if match is None:
        raise ValueError("runtime environment secret value invalid")
    for placeholder, value in zip(placeholders, match.groups(), strict=True):
        name = placeholder.group(0)
        if name in generated and generated[name] != value:
            raise ValueError("runtime environment repeated secret mismatch")
        generated[name] = value


def _validate_runtime(contents: str, template: str, machine: str, cloud_url: str) -> None:
    expected = _parse_env(template)
    if expected.get("LICENSE_MACHINE_ID") != "" or "CLOUD_OPS_BASE_URL" in expected:
        raise ValueError("runtime environment template fields invalid")
    actual = _parse_env(contents)
    if set(actual) != set(expected) | {"CLOUD_OPS_BASE_URL"}:
        raise ValueError("runtime environment fields invalid")
    generated: dict[str, str] = {}
    for name, template_value in expected.items():
        if name == "LICENSE_MACHINE_ID":
            if actual[name] != machine:
                raise ValueError("runtime environment machine-id mismatch")
        elif PLACEHOLDER.search(template_value):
            _validate_placeholder_value(template_value, actual[name], generated)
        elif actual[name] != template_value:
            raise ValueError("runtime environment fixed value mismatch")
    if actual["CLOUD_OPS_BASE_URL"] != cloud_url:
        raise ValueError("runtime environment Cloud URL mismatch")


def _read_private_runtime(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("runtime environment permissions invalid")
        return handle.read()


def _validate_physical_ethernet(interface: str, sys_class_net: Path) -> None:
    interface_path = sys_class_net / interface
    if (
        not interface_path.is_dir()
        or not (interface_path / "device").exists()
        or (interface_path / "type").read_text(encoding="ascii").strip() != "1"
    ):
        raise ValueError("OS Installer factory interface must be one physical Ethernet device")


def initialize(
    *,
    public_config: Path,
    env_template: Path,
    machine_id: Path,
    runtime_env: Path,
    cloud_env: Path,
    netplan: Path,
    sys_class_net: Path = Path("/sys/class/net"),
) -> dict[str, str]:
    payload = validate_public_config(json.loads(public_config.read_text(encoding="utf-8")))
    cloud_url = payload["cloud_ops_base_url"]
    interface = payload["factory_interface"]
    address = payload["factory_address"]
    _validate_physical_ethernet(interface, sys_class_net)
    template = "\n".join(
        line
        for line in env_template.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    machine = machine_id.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", machine):
        raise ValueError("host machine-id invalid")
    cloud_url = cloud_url.rstrip("/")
    if runtime_env.exists() or runtime_env.is_symlink():
        runtime = _read_private_runtime(runtime_env)
        _validate_runtime(runtime, template, machine, cloud_url)
    else:
        generated: dict[str, str] = {}

        def replace(match: re.Match[str]) -> str:
            name = match.group(0)
            generated.setdefault(name, secrets.token_hex(32))
            return generated[name]

        runtime = PLACEHOLDER.sub(replace, template)
        runtime = re.sub(r"(?m)^LICENSE_MACHINE_ID=.*$", f"LICENSE_MACHINE_ID={machine}", runtime)
        runtime += f"\nCLOUD_OPS_BASE_URL={cloud_url}\n"
        _validate_runtime(runtime, template, machine, cloud_url)
        if not _atomic_create(runtime_env, runtime.encode("utf-8"), 0o600):
            runtime = _read_private_runtime(runtime_env)
            _validate_runtime(runtime, template, machine, cloud_url)
    cloud = f"CLOUD_OPS_BASE_URL={cloud_url}\n"
    network = (
        "network:\n"
        "  version: 2\n"
        "  ethernets:\n"
        # Netplan names networkd files after this ID. It must sort before the
        # autoinstall all-en/all-eth DHCP definitions for the factory NIC.
        "    00-drycontroller-factory:\n"
        f"      match:\n        name: {interface}\n"
        "      dhcp4: false\n"
        "      optional: true\n"
        f"      addresses: [{address}]\n"
        "      link-local: []\n"
    )
    _atomic_write(cloud_env, cloud.encode("utf-8"), 0o600)
    _atomic_write(netplan, network.encode("utf-8"), 0o600)
    return {
        "cloud_ops_base_url": cloud_url,
        "factory_interface": interface,
        "factory_address": address,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-config", required=True, type=Path)
    parser.add_argument("--env-template", required=True, type=Path)
    parser.add_argument("--machine-id", type=Path, default=Path("/etc/machine-id"))
    parser.add_argument("--runtime-env", type=Path, default=Path("/etc/drycontroller/runtime.env"))
    parser.add_argument("--cloud-env", type=Path, default=Path("/etc/drycontroller/cloud-ops.env"))
    parser.add_argument(
        "--netplan",
        type=Path,
        default=Path("/etc/netplan/90-drycontroller-factory.yaml"),
    )
    args = parser.parse_args()
    try:
        result = initialize(
            public_config=args.public_config,
            env_template=args.env_template,
            machine_id=args.machine_id,
            runtime_env=args.runtime_env,
            cloud_env=args.cloud_env,
            netplan=args.netplan,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"runtime configuration failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
