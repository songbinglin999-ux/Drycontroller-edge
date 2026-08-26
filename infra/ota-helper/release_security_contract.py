"""Executable parity gate for the duplicated Host/Software security boundary."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path


CONTRACT_FUNCTIONS = (
    "_atomic_write",
    "_bound_docker_daemon",
    "_descriptor_path",
    "_docker_daemon_identity",
    "_docker_environment",
    "_open_trusted_executable",
    "_opened_command",
    "_opened_docker_socket",
    "_private_docker_environment",
    "_read_identity_file",
    "_read_json",
    "_read_registry_auth_document",
    "_read_registry_auth_source",
    "_requires_registry_auth",
    "_strict_json_object",
    "_text",
    "_validate_ancestor_chain",
    "_validate_local_docker_config",
    "_validate_trusted_executable",
    "canonical_topology_digest",
    "utc_now",
)
CONTRACT_CONSTANTS = (
    "COSIGN_TOOLCHAIN",
    "HOST_ACTIVE_SCHEMA",
    "HOST_HEALTH_CONTRACT",
    "REGISTRY_AUTH_SCHEMA",
    "SOFTWARE_RELEASE_SCHEMA",
    "_SECURITY_CONTRACT_DIR",
    "_COMMIT",
    "_COSIGN",
    "_DAEMON_IDENTITY_PATH",
    "_DIGEST",
    "_DOCKER",
    "_DOCKER_COMPOSE",
    "_DOCKER_CONFIG_PARENT",
    "_DOCKER_SOCKET",
    "_FIXED_ENV",
    "_LOCAL_DOCKER_CONFIG",
    "_MAX_JSON_BYTES",
    "_OPERATION_ID",
    "_POLICY_FIELDS",
    "_REGISTRY",
    "_REGISTRY_AUTH_SOURCE",
    "_REPOSITORY_PATH",
    "_SEMVER",
    "_SERVICE_NAME",
    "_TOOL_IDENTITY_PATH",
    "_TRUST_ROOT",
)
EXPECTED_ENGINE_DIGESTS = {
    "host": "6fe45fdd7e3fd8b222b563f5e289297074d0c61f50b17987e929c918db36d386",
    "software": "1a08ef81eefb6b637beeb3cf29b2c946c72d8eb09b10dadc00fc3069716e2043",
}


def _assigned_constants(module: ast.Module) -> dict[str, ast.expr]:
    result: dict[str, ast.expr] = {}
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            result[node.target.id] = node.value
    return result


def engine_contract_digest(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source, filename=str(path))
    functions = {
        node.name: node
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    constants = _assigned_constants(module)
    try:
        contract = {
            "functions": {
                name: ast.get_source_segment(source, functions[name])
                for name in CONTRACT_FUNCTIONS
            },
            "constants": {
                name: ast.get_source_segment(source, constants[name])
                for name in CONTRACT_CONSTANTS
            },
        }
    except KeyError as exc:
        raise RuntimeError(f"release security contract member missing: {exc.args[0]}") from exc
    canonical = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def assert_engine_security_contract(role: str, path: Path) -> None:
    expected = EXPECTED_ENGINE_DIGESTS.get(role)
    observed = engine_contract_digest(path)
    if expected is None or observed != expected:
        raise RuntimeError(
            f"{role} release security contract drift: expected={expected} observed={observed}"
        )


SHARED_SECURITY_CONTRACT_VERSION = "sha256:" + hashlib.sha256(
    json.dumps(
        {
            "functions": CONTRACT_FUNCTIONS,
            "constants": CONTRACT_CONSTANTS,
            "engines": EXPECTED_ENGINE_DIGESTS,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
