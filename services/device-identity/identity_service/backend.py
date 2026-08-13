from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .contract import canonical_challenge


class DeviceIdentityBackend(Protocol):
    def public_key(self) -> str: ...

    def sign_identity_challenge(self, challenge: dict[str, str]) -> str: ...

    def health(self) -> dict[str, str]: ...


class FileTestIdentityBackend:
    """Explicit RB-5A test backend. It is never selected as a fallback."""

    def __init__(self, key_path: Path):
        self._key_path = key_path
        self._private_key = self._load_or_create_key()

    def public_key(self) -> str:
        return base64.b64encode(self._public_der()).decode("ascii")

    def fingerprint(self) -> str:
        return hashlib.sha256(self._public_der()).hexdigest()

    def sign_identity_challenge(self, challenge: dict[str, str]) -> str:
        canonical = canonical_challenge(challenge)
        signature = self._private_key.sign(canonical, ec.ECDSA(hashes.SHA256()))
        return base64.b64encode(signature).decode("ascii")

    def health(self) -> dict[str, str]:
        return {"status": "ok", "identity_fingerprint": self.fingerprint()}

    def _public_der(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def _load_or_create_key(self) -> ec.EllipticCurvePrivateKey:
        try:
            raw = self._key_path.read_bytes()
        except FileNotFoundError:
            self._key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._key_path.parent, 0o700)
            key = ec.generate_private_key(ec.SECP256R1())
            raw = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            _atomic_write(self._key_path, raw, mode=0o600)
            return key
        try:
            key = serialization.load_pem_private_key(raw, password=None)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("test identity key is invalid") from exc
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise RuntimeError("test identity key must be ECDSA P-256")
        if self._key_path.stat().st_mode & 0o077:
            raise RuntimeError("test identity key permissions must be 0600")
        return key


CommandRunner = Callable[[list[str]], None]


class TpmIdentityBackend:
    """TPM-only production identity derived from one fixed owner primary template."""

    _TCTI = "device:/dev/tpmrm0"
    _ATTRIBUTES = "fixedtpm|fixedparent|sensitivedataorigin|userwithauth|sign|noda"

    def __init__(self, *, run_command: CommandRunner | None = None):
        self._run_command = run_command or _run_tpm_command

    def public_key(self) -> str:
        public_der, _signature = self._use_primary()
        return base64.b64encode(public_der).decode("ascii")

    def fingerprint(self) -> str:
        public_der, _signature = self._use_primary()
        return hashlib.sha256(public_der).hexdigest()

    def sign_identity_challenge(self, challenge: dict[str, str]) -> str:
        canonical = canonical_challenge(challenge)
        public_der, signature = self._use_primary(canonical)
        if signature is None:  # pragma: no cover - guarded by _use_primary
            raise RuntimeError("TPM identity signing failed")
        if hashlib.sha256(public_der).hexdigest() != challenge["identity_fingerprint"]:
            raise RuntimeError("TPM identity fingerprint changed")
        public_key = _load_tpm_public_key(public_der)
        try:
            public_key.verify(signature, canonical, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature as exc:
            raise RuntimeError("TPM identity signature verification failed") from exc
        return base64.b64encode(signature).decode("ascii")

    def health(self) -> dict[str, str]:
        try:
            return {"status": "ok", "identity_fingerprint": self.fingerprint()}
        except RuntimeError:
            return {"status": "degraded", "reason": "tpm_identity_unavailable"}

    def _use_primary(self, message: bytes | None = None) -> tuple[bytes, bytes | None]:
        with tempfile.TemporaryDirectory(prefix="drycontroller-tpm-identity-") as temporary:
            work_dir = Path(temporary)
            os.chmod(work_dir, 0o700)
            context_path = work_dir / "primary.ctx"
            public_path = work_dir / "public.der"
            signature_path = work_dir / "signature.der"
            message_path = work_dir / "challenge.bin"
            create_command = [
                "/usr/bin/tpm2_createprimary",
                "--quiet",
                "--tcti",
                self._TCTI,
                "--hierarchy",
                "o",
                "--hash-algorithm",
                "sha256",
                "--key-algorithm",
                "ecc256:ecdsa-sha256",
                "--attributes",
                self._ATTRIBUTES,
                "--key-context",
                str(context_path),
                "--format",
                "der",
                "--output",
                str(public_path),
            ]
            try:
                self._run_required(create_command, "TPM identity unavailable")
                os.chmod(context_path, 0o600)
                public_der = public_path.read_bytes()
                _load_tpm_public_key(public_der)
                signature = None
                if message is not None:
                    _write_private_file(message_path, message)
                    self._run_required(
                        [
                            "/usr/bin/tpm2_sign",
                            "--quiet",
                            "--tcti",
                            self._TCTI,
                            "--key-context",
                            str(context_path),
                            "--hash-algorithm",
                            "sha256",
                            "--format",
                            "plain",
                            "--signature",
                            str(signature_path),
                            str(message_path),
                        ],
                        "TPM identity signing failed",
                    )
                    signature = signature_path.read_bytes()
                    if not signature or len(signature) > 256:
                        raise RuntimeError("TPM identity signature is invalid")
            except OSError as exc:
                raise RuntimeError("TPM identity unavailable") from exc
            return public_der, signature

    def _run_required(self, command: list[str], message: str) -> None:
        try:
            self._run_command(command)
        except RuntimeError as exc:
            raise RuntimeError(message) from exc


def load_backend(
    *,
    mode: str,
    key_path: Path,
    run_command: CommandRunner | None = None,
) -> DeviceIdentityBackend:
    if mode == "file-test":
        return FileTestIdentityBackend(key_path)
    if mode == "tpm":
        return TpmIdentityBackend(run_command=run_command)
    raise RuntimeError("DRYCONTROLLER_IDENTITY_BACKEND must explicitly select a supported backend")


def _load_tpm_public_key(public_der: bytes) -> ec.EllipticCurvePublicKey:
    try:
        public_key = serialization.load_der_public_key(public_der)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("TPM identity public key is invalid") from exc
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(public_key.curve, ec.SECP256R1):
        raise RuntimeError("TPM identity key must be ECDSA P-256")
    return public_key


def _run_tpm_command(command: list[str]) -> None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("TPM tool unavailable") from exc
    if completed.returncode != 0:
        raise RuntimeError("TPM tool failed")


def _write_private_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
