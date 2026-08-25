from __future__ import annotations

import importlib.util
import io
import hashlib
import json
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "scripts" / "ci" / "python_base_image_contract.py"


def _contract():
    assert CONTRACT_PATH.is_file(), "formal Python base-image verifier is missing"
    spec = importlib.util.spec_from_file_location("python_base_image_contract", CONTRACT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_inspect(contract, *, architecture: str = "amd64") -> dict[str, object]:
    return {
        "Architecture": architecture,
        "Config": {
            "Labels": {
                contract.BASE_DIGEST_LABEL: contract.PYTHON_BASE_DIGEST,
                contract.BASE_NAME_LABEL: contract.PYTHON_BASE_NAME,
            }
        },
        "Id": "sha256:" + "a" * 64,
        "Os": "linux",
    }


def test_contract_scans_exactly_the_six_local_python_service_dockerfiles():
    contract = _contract()

    records = contract.verify_dockerfiles(ROOT)

    assert tuple(records) == contract.PYTHON_SERVICES
    assert all(record["base_reference"] == contract.PYTHON_BASE_REFERENCE for record in records.values())
    assert all(record["platform"] == contract.PYTHON_BASE_PLATFORM for record in records.values())


@pytest.mark.parametrize(
    ("from_line", "message"),
    (
        ("FROM python:3.11-slim", "digest locked"),
        (
            "FROM python:3.11-slim@sha256:" + "0" * 64,
            "approved base reference",
        ),
    ),
)
def test_dockerfile_contract_rejects_tag_only_and_digest_drift(from_line: str, message: str):
    contract = _contract()
    dockerfile = "\n".join(
        (
            from_line,
            f'LABEL {contract.BASE_NAME_LABEL}="{contract.PYTHON_BASE_NAME}"',
            f'LABEL {contract.BASE_DIGEST_LABEL}="{contract.PYTHON_BASE_DIGEST}"',
            "",
        )
    )

    with pytest.raises(contract.ContractError, match=message):
        contract.verify_dockerfile_text(dockerfile, "edge-api")


def test_dockerfile_contract_rejects_lowercase_mutable_final_stage():
    contract = _contract()
    dockerfile = "\n".join(
        (
            f"FROM {contract.PYTHON_BASE_REFERENCE}",
            "  from alpine:3.22 as final",
            f'LABEL {contract.BASE_NAME_LABEL}="{contract.PYTHON_BASE_NAME}"',
            f'LABEL {contract.BASE_DIGEST_LABEL}="{contract.PYTHON_BASE_DIGEST}"',
            "",
        )
    )

    with pytest.raises(contract.ContractError, match="exactly one FROM"):
        contract.verify_dockerfile_text(dockerfile, "edge-api")


def test_image_contract_rejects_wrong_platform_and_base_digest_drift():
    contract = _contract()

    with pytest.raises(contract.ContractError, match="linux/amd64"):
        contract.validate_image_inspect("edge-api", _valid_inspect(contract, architecture="arm64"))

    drifted = _valid_inspect(contract)
    drifted["Config"]["Labels"][contract.BASE_DIGEST_LABEL] = "sha256:" + "0" * 64
    with pytest.raises(contract.ContractError, match="base digest"):
        contract.validate_image_inspect("edge-api", drifted)


def test_image_contract_uses_portable_config_digest_not_containerd_inspect_id(tmp_path: Path):
    contract = _contract()
    config = {
        "architecture": "amd64",
        "config": {
            "Labels": {
                contract.BASE_DIGEST_LABEL: contract.PYTHON_BASE_DIGEST,
                contract.BASE_NAME_LABEL: contract.PYTHON_BASE_NAME,
            }
        },
        "os": "linux",
    }
    config_bytes = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    archive_path = tmp_path / "image.tar"
    with tarfile.open(archive_path, mode="w") as archive:
        for name, payload in (
            (
                "manifest.json",
                json.dumps([{"Config": "config.json", "Layers": [], "RepoTags": ["edge/edge-api:test"]}]).encode(),
            ),
            ("config.json", config_bytes),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    containerd_inspect_id = "sha256:" + "b" * 64
    record = contract.validate_image_archive(
        "edge-api",
        archive_path,
        expected_image_id=config_digest,
    )

    assert record["image_id"] == config_digest
    assert record["image_id"] != containerd_inspect_id


def test_receipt_binds_all_six_image_config_digests_to_the_signed_manifest(tmp_path: Path):
    contract = _contract()
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    image_ids: dict[str, str] = {}
    for index, service in enumerate(contract.PYTHON_SERVICES, start=1):
        image_id = f"sha256:{index:064x}"
        image_ids[service] = image_id
        record = contract.validate_image_inspect(
            service,
            {**_valid_inspect(contract), "Id": image_id},
            expected_image_id=image_id,
        )
        (receipt_dir / f"{service}.json").write_text(json.dumps(record), encoding="utf-8")

    manifest = {
        "image_ids": image_ids,
        "schema": "drycontroller.software-release.v1",
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    receipt = contract.verify_receipts(receipt_dir, manifest_path)

    assert receipt["schema"] == contract.RECEIPT_SCHEMA
    assert receipt["base"] == {
        "digest": contract.PYTHON_BASE_DIGEST,
        "name": contract.PYTHON_BASE_NAME,
        "platform": contract.PYTHON_BASE_PLATFORM,
    }
    assert receipt["images"] == image_ids
    assert receipt["software_manifest_digest"].startswith("sha256:")


@pytest.mark.parametrize("mutation", ("missing", "extra"))
def test_receipt_set_rejects_missing_or_extra_service_receipts(
    tmp_path: Path, mutation: str
):
    contract = _contract()
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    image_ids: dict[str, str] = {}
    for index, service in enumerate(contract.PYTHON_SERVICES, start=1):
        image_id = f"sha256:{index:064x}"
        image_ids[service] = image_id
        record = contract.validate_image_inspect(
            service,
            {**_valid_inspect(contract), "Id": image_id},
            expected_image_id=image_id,
        )
        (receipt_dir / f"{service}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
    if mutation == "missing":
        (receipt_dir / f"{contract.PYTHON_SERVICES[0]}.json").unlink()
    else:
        (receipt_dir / "unexpected.json").write_text("{}", encoding="utf-8")

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "image_ids": image_ids,
                "schema": "drycontroller.software-release.v1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(contract.ContractError, match="exactly six services"):
        contract.verify_receipts(receipt_dir, manifest_path)


def test_formal_builder_verifies_built_and_inherited_python_images_before_signing():
    script = (ROOT / "scripts/ci/build-software-release.sh").read_text(encoding="utf-8")

    assert "python_base_image_contract.py verify-dockerfiles" in script
    assert script.count("python_base_image_contract.py verify-image") == 3
    assert "python_base_image_contract.py verify-receipts" in script
    changed_loop = script.split('for service in "${SERVICES[@]}"; do', 1)[1].split(
        "\ndone\n", 1
    )[0]
    verify_image = changed_loop.index("python_base_image_contract.py verify-image")
    assert verify_image < changed_loop.index("release_step_push")
    assert verify_image < changed_loop.index("release_step_sign_image")
    assert script.rindex("python_base_image_contract.py verify-receipts") < script.rindex(
        "release_step_sign_manifest"
    )
    assert script.rindex("python_base_image_contract.py verify-receipts") < script.rindex(
        "release_step_complete_cloud"
    )
