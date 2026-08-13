from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Protocol

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


def load_backend(*, mode: str, key_path: Path) -> DeviceIdentityBackend:
    if mode == "file-test":
        return FileTestIdentityBackend(key_path)
    if mode == "tpm":
        raise RuntimeError("TPM identity backend is unavailable until RB-5B")
    raise RuntimeError("DRYCONTROLLER_IDENTITY_BACKEND must explicitly select a supported backend")


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
