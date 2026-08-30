from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status

from app.core.config import settings
from common.license_verifier import verify_installed_license


def license_status(required_capability: str | None = None) -> dict:
    return verify_installed_license(
        license_path=Path(settings.LICENSE_FILE_PATH),
        trust_policy_path=Path(settings.LICENSE_TRUST_POLICY_PATH),
        device_identity_path=Path(settings.DEVICE_IDENTITY_PATH),
        tpm_observation_path=Path(settings.TPM_IDENTITY_OBSERVATION_PATH),
        required_capability=required_capability,
    )


def require_license_capability(capability: str) -> dict:
    result = license_status(capability)
    if result.get("valid") is not True:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "success": False,
                "error": "license_not_admitted",
                "reason": result.get("reason", "license_invalid"),
                "required_capability": capability,
            },
        )
    return result
