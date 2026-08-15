from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from common.tailscale_status import inspect_tailscale_status


TAILSCALE_BIN = "/usr/bin/tailscale"
TAILSCALE_SOCKET = "/var/run/tailscale/tailscaled.sock"
TAILNET_KEY = re.compile(r"^hskey-auth-[A-Za-z0-9_-]{16,192}$")
TAILSCALE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
CommandRunner = Callable[[list[str]], int]
StatusInspector = Callable[..., dict[str, object]]


class TailnetJoinError(RuntimeError):
    pass


def _run_command(command: list[str]) -> int:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env={"HOME": "/tmp", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TailnetJoinError("tailnet_join_failed") from exc
    return completed.returncode


def _validated_login_server(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise TailnetJoinError("tailnet_config_invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise TailnetJoinError("tailnet_config_invalid")
    return value.rstrip("/")


class TailnetClient:
    def __init__(
        self,
        *,
        expected_version: str,
        login_server: str,
        temp_root: Path = Path("/run/drycontroller-tailnet-join"),
        run_command: CommandRunner = _run_command,
        inspect_status: StatusInspector = inspect_tailscale_status,
    ):
        if TAILSCALE_VERSION.fullmatch(expected_version) is None:
            raise TailnetJoinError("tailnet_config_invalid")
        self.expected_version = expected_version
        self.login_server = _validated_login_server(login_server)
        self.temp_root = temp_root
        self.run_command = run_command
        self.inspect_status = inspect_status

    def status(self, *, node_name: str) -> dict[str, object]:
        if not node_name or node_name != node_name.lower():
            raise TailnetJoinError("tailnet_device_identity_invalid")
        return self.inspect_status(
            expected_version=self.expected_version,
            expected_login_server=self.login_server,
            expected_hostname=node_name,
            tailscale_bin=TAILSCALE_BIN,
            socket_path=TAILSCALE_SOCKET,
        )

    def join(self, *, node_name: str, auth_key: str) -> dict[str, object]:
        current = self.status(node_name=node_name)
        if current.get("ok") is True:
            return current
        if TAILNET_KEY.fullmatch(auth_key) is None:
            raise TailnetJoinError("tailnet_auth_key_rejected")

        self._prepare_temp_root()
        descriptor, raw_path = tempfile.mkstemp(prefix=".auth-key.", dir=self.temp_root)
        key_path = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                descriptor = -1
                stream.write(auth_key)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            command = [
                TAILSCALE_BIN,
                f"--socket={TAILSCALE_SOCKET}",
                "up",
                f"--login-server={self.login_server}",
                f"--hostname={node_name}",
                "--accept-dns=false",
                "--accept-routes=false",
                f"--auth-key=file:{key_path}",
            ]
            if self.run_command(command) != 0:
                raise TailnetJoinError("tailnet_auth_key_rejected")
            result = self.status(node_name=node_name)
            if result.get("ok") is not True:
                raise TailnetJoinError("tailnet_join_verification_failed")
            return result
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                key_path.unlink()
            except OSError as exc:
                raise TailnetJoinError("tailnet_auth_key_cleanup_failed") from exc

    def _prepare_temp_root(self) -> None:
        try:
            self.temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = self.temp_root.lstat()
        except OSError as exc:
            raise TailnetJoinError("tailnet_temp_path_invalid") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (os.geteuid() == 0 and metadata.st_uid != 0)
        ):
            raise TailnetJoinError("tailnet_temp_path_invalid")


def load_tailnet_client_from_runtime(path: Path) -> TailnetClient:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise TailnetJoinError("tailnet_config_invalid")
        if os.geteuid() == 0 and metadata.st_uid != 0:
            raise TailnetJoinError("tailnet_config_invalid")
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name in {"TS_LOGIN_SERVER", "TAILSCALE_EXPECTED_VERSION"}:
                if name in values:
                    raise TailnetJoinError("tailnet_config_invalid")
                values[name] = value
        return TailnetClient(
            expected_version=values["TAILSCALE_EXPECTED_VERSION"],
            login_server=values["TS_LOGIN_SERVER"],
        )
    except (KeyError, OSError, UnicodeDecodeError) as exc:
        raise TailnetJoinError("tailnet_config_invalid") from exc
