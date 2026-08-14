#!/usr/bin/python3
"""Create per-device runtime secrets from public installer configuration."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path


BOOTSTRAP_DIR = Path(__file__).resolve().parent
if str(BOOTSTRAP_DIR) not in sys.path:
    sys.path.insert(0, str(BOOTSTRAP_DIR))

from installer_public_config import validate_public_config  # noqa: E402


PLACEHOLDER = re.compile(r"change_me_[A-Za-z0-9_]+")


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(
    *,
    public_config: Path,
    env_template: Path,
    machine_id: Path,
    runtime_env: Path,
    cloud_env: Path,
    netplan: Path,
) -> dict[str, str]:
    payload = validate_public_config(json.loads(public_config.read_text(encoding="utf-8")))
    cloud_url = payload["cloud_ops_base_url"]
    interface = payload["factory_interface"]
    address = payload["factory_address"]
    template = "\n".join(
        line
        for line in env_template.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    machine = machine_id.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", machine):
        raise ValueError("host machine-id invalid")
    generated: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        name = match.group(0)
        generated.setdefault(name, secrets.token_hex(32))
        return generated[name]

    runtime = PLACEHOLDER.sub(replace, template)
    runtime = re.sub(r"(?m)^LICENSE_MACHINE_ID=.*$", f"LICENSE_MACHINE_ID={machine}", runtime)
    runtime += f"\nCLOUD_OPS_BASE_URL={cloud_url.rstrip('/')}\n"
    if PLACEHOLDER.search(runtime):
        raise ValueError("runtime environment still contains secret placeholders")
    cloud = f"CLOUD_OPS_BASE_URL={cloud_url.rstrip('/')}\n"
    network = (
        "network:\n"
        "  version: 2\n"
        "  ethernets:\n"
        "    drycontroller-factory:\n"
        f"      match:\n        name: {interface}\n"
        "      dhcp4: false\n"
        "      optional: true\n"
        f"      addresses: [{address}]\n"
        "      link-local: []\n"
    )
    _atomic_write(runtime_env, runtime.encode("utf-8"), 0o600)
    _atomic_write(cloud_env, cloud.encode("utf-8"), 0o600)
    _atomic_write(netplan, network.encode("utf-8"), 0o600)
    return {
        "cloud_ops_base_url": cloud_url.rstrip("/"),
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
