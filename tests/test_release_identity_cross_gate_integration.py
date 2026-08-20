from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
HOST_WORKFLOW = ROOT / ".gitea/workflows/edge-host-release.yml"
SOFTWARE_BUILDER = ROOT / "scripts/ci/build-software-release.sh"
TRIVY_VERIFIER = ROOT / "scripts/ci/verify-trivy-executable.py"


def _workflow() -> dict[str, object]:
    return yaml.safe_load(HOST_WORKFLOW.read_text(encoding="utf-8"))


def _step_names(job: dict[str, object]) -> list[str]:
    return [step["name"] for step in job["steps"]]


def _load_trivy_verifier():
    assert TRIVY_VERIFIER.is_file()
    spec = importlib.util.spec_from_file_location(
        "verify_trivy_executable", TRIVY_VERIFIER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_host_release_jobs_preserve_strict_cross_gate_order():
    jobs = _workflow()["jobs"]
    release_steps = jobs["release"]["steps"]
    release_names = _step_names(jobs["release"])
    expected_release_names = [
        "Authorize exact release controller and candidate",
        "Establish pinned Trivy executable",
        "Checkout exact approved candidate",
        "Resolve/bind exact candidate identity",
        "Prime pinned Trivy database",
        "Build and complete paired HostRelease",
        "Publish completed public release handoff",
        "Remove pinned Trivy state",
    ]
    assert release_names == expected_release_names

    test_names = _step_names(jobs["test"])
    assert test_names.index("Authorize exact release controller and candidate") < test_names.index(
        "Checkout exact approved candidate"
    ) < test_names.index("Bind exact candidate source") < test_names.index(
        "Verify repository"
    )

    checkout = release_steps[2]
    assert checkout["with"] == {
        "ref": "${{ inputs.candidate_sha }}",
        "fetch-depth": 0,
        "clean": True,
        "persist-credentials": False,
    }
    assert release_steps[-1]["if"] == "${{ always() }}"


def test_controller_trivy_pin_matches_candidate_verifier_constants():
    verifier = _load_trivy_verifier()
    workflow = _workflow()
    establish = next(
        step
        for step in workflow["jobs"]["release"]["steps"]
        if step["name"] == "Establish pinned Trivy executable"
    )["run"]

    assert str(verifier.TRIVY_BINARY) == "/usr/local/bin/trivy"
    assert str(verifier.TRIVY_BINARY) in establish
    assert verifier.TRIVY_VERSION in establish
    assert verifier.TRIVY_BINARY_SHA256 in establish


def test_changed_python_images_prove_base_identity_before_push_and_sign():
    script = SOFTWARE_BUILDER.read_text(encoding="utf-8")
    loop = script.split('for service in "${SERVICES[@]}"; do', 1)[1].split(
        "\ndone\n", 1
    )[0]
    build = loop.index("release_step_build_or_mirror")
    scan = loop.index("release_step_scan")
    portable_id = loop.index("release_step_resolve_portable_image_id")
    verify_base = loop.index("python_base_image_contract.py verify-image")
    push = loop.index("release_step_push")
    sign = loop.index("release_step_sign_image")

    assert build < scan < portable_id < verify_base < push < sign
    assert script.index("python_base_image_contract.py verify-dockerfiles") < script.index(
        "release_step_resolve_predecessor"
    )
    assert script.index("python3 scripts/ci/verify-trivy-executable.py") < script.index(
        '"${TRIVY_BINARY}" image'
    )


def test_inherited_receipts_and_manifest_binding_precede_release_completion():
    script = SOFTWARE_BUILDER.read_text(encoding="utf-8")
    inherited = script.split("release_step_load_inherited_images() {", 1)[1].split(
        "\n}\n\nrelease_step_sign_manifest", 1
    )[0]
    assert inherited.index("python_base_image_contract.py verify-image") < inherited.index(
        'IMAGE_REFS["${service}"]'
    )

    receipts = script.rindex("python_base_image_contract.py verify-receipts")
    sign_manifest = script.rindex("release_step_sign_manifest")
    complete_cloud = script.rindex("release_step_complete_cloud")
    assert receipts < sign_manifest < complete_cloud


def test_exact_candidate_guards_exclude_old_and_protected_control_sources():
    workflow = HOST_WORKFLOW.read_text(encoding="utf-8")
    assert "4ba671d4b8ae6e9449b8c64a279194359e4f0598" not in workflow
    assert "ref: ${{ inputs.candidate_sha }}" in workflow
    assert 'test "${REQUESTED_CANDIDATE_SHA}" != "${GITHUB_SHA}"' in workflow
    assert "13af694b04641467182ffe7dfba1650229f0045d" in workflow
    assert "70ec3d49dab99d375820e679264b9a480d4bfe41" in workflow
    assert 'test "${protected_control_status}" -eq 1' in workflow
