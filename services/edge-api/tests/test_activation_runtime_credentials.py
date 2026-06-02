from pathlib import Path
import sys

PATH = Path(__file__).resolve()
for candidate in (PATH.parents[1], PATH.parents[1] / "app"):
    entry = str(candidate)
    if entry not in sys.path:
        sys.path.append(entry)

from app.services import activation


def test_activation_response_node_token_is_saved_for_runtime_auth(tmp_path: Path, monkeypatch):
    env_path = tmp_path / "tailnet.env"
    env_path.write_text(
        "CLOUD_OPS_BASE_URL=https://cloud.example\n"
        "CLOUD_OPS_ENROLL_TOKEN=old-token\n"
        "TS_HOSTNAME=edge-1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(activation, "_TAILNET_ENV_PATH", env_path)

    activation._save_runtime_credentials({"node_enroll_token": "new-token"})

    assert env_path.read_text(encoding="utf-8").splitlines() == [
        "CLOUD_OPS_BASE_URL=https://cloud.example",
        "CLOUD_OPS_ENROLL_TOKEN=new-token",
        "TS_HOSTNAME=edge-1",
    ]
    assert env_path.stat().st_mode & 0o777 == 0o600
