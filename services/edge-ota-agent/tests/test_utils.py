"""
edge-ota-agent 工具函数单元测试
"""

import os
import sys
from pathlib import Path
import importlib



PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]   # edge-ota-agent/
ROOT = PATH.parents[3]           # Drycontroller-edge/ (项目根，含 common/)

for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

def _load_module(tmp_path: Path):
    os.environ["OTA_DATA_DIR"] = str(tmp_path / "data")
    os.environ["OTA_LOG_DIR"] = str(tmp_path / "logs")
    os.environ["OTA_WORK_DIR"] = str(tmp_path / "work")
    os.environ["LOG_ROOT"] = str(tmp_path / "logs")
    os.environ["REGISTRY_DOCKER_CONFIG_PATH"] = str(tmp_path / "registry" / "config.json")
    os.environ["REGISTRY_CREDENTIAL_STATUS_PATH"] = str(tmp_path / "registry" / "status.json")
    if "ota_agent.main" in sys.modules:
        return importlib.reload(sys.modules["ota_agent.main"])
    return importlib.import_module("ota_agent.main")


def test_split_api_keys_json_and_csv():
    ota_main = _load_module(Path("/tmp"))
    assert ota_main._split_api_keys('["a","b"]') == ["a", "b"]
    assert ota_main._split_api_keys("a, b ,c") == ["a", "b", "c"]
    assert ota_main._split_api_keys("") == []


def test_extract_images_from_compose_yaml():
    ota_main = _load_module(Path("/tmp"))
    content = """
services:
  api:
    image: example/edge-api:1.0
  daq:
    image: example/edge-daq:1.0
  dup:
    image: example/edge-api:1.0
  none:
    environment:
      - FOO=bar
"""
    images = ota_main._extract_images_from_compose_yaml(content)
    assert images == ["example/edge-api:1.0", "example/edge-daq:1.0"]


def test_tail_lines(tmp_path: Path):
    ota_main = _load_module(tmp_path)
    log_file = tmp_path / "test.log"
    log_file.write_text("a\nb\nc\nd\n", encoding="utf-8")
    tail = ota_main._tail_lines(log_file, 2)
    assert tail == ["c", "d"]


def test_write_and_read_json(tmp_path: Path):
    ota_main = _load_module(tmp_path)
    path = tmp_path / "data.json"
    ota_main._write_json(path, {"a": 1, "b": "x"})
    obj = ota_main._read_json(path)
    assert obj["a"] == 1
    assert obj["b"] == "x"


def test_registry_credential_check_requires_active_status(tmp_path: Path):
    ota_main = _load_module(tmp_path)
    payload = ota_main.OtaStartRequest(mode="online")
    missing = ota_main._registry_credential_check(payload)
    assert missing["ok"] is False
    assert missing["reason"] == "registry_credential_missing"

    status_path = tmp_path / "registry" / "status.json"
    status_path.parent.mkdir(parents=True)
    ota_main._write_json(
        status_path,
        {
            "status": "active",
            "credential_id": "cred-1",
            "registry_host": "git.example.com",
            "username": "robot$edge",
            "secret_fingerprint": "fp-1",
            "version": 2,
            "updated_at": ota_main._now().isoformat(),
        },
    )
    missing_config = ota_main._registry_credential_check(payload)
    assert missing_config["ok"] is False
    assert missing_config["reason"] == "registry_docker_config_missing"

    config_path = tmp_path / "registry" / "config.json"
    ota_main._write_json(
        config_path,
        {
            "auths": {
                "git.example.com": {
                    "auth": "cm9ib3QkZWRnZTpyb2JvdC1zZWNyZXQ=",
                }
            }
        },
    )
    ok = ota_main._registry_credential_check(
        ota_main.OtaStartRequest(
            mode="online",
            registry_credential_id="cred-1",
            registry_credential_version=2,
            registry_secret_fingerprint="fp-1",
        )
    )
    assert ok["ok"] is True

    stale = ota_main._registry_credential_check(
        ota_main.OtaStartRequest(mode="online", registry_credential_id="cred-1", registry_credential_version=3)
    )
    assert stale["ok"] is False
    assert stale["reason"] == "registry_credential_version_stale"


def test_registry_credential_check_follows_docker_config_env(tmp_path: Path, monkeypatch):
    ota_main = _load_module(tmp_path)
    monkeypatch.delenv("REGISTRY_DOCKER_CONFIG_PATH", raising=False)
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path / "custom-docker"))

    status_path = tmp_path / "registry" / "status.json"
    status_path.parent.mkdir(parents=True)
    ota_main._write_json(
        status_path,
        {
            "status": "active",
            "credential_id": "cred-1",
            "registry_host": "git.example.com",
            "username": "robot$edge",
            "secret_fingerprint": "fp-1",
            "version": 2,
            "updated_at": ota_main._now().isoformat(),
        },
    )
    docker_config_path = tmp_path / "custom-docker" / "config.json"
    docker_config_path.parent.mkdir(parents=True)
    ota_main._write_json(
        docker_config_path,
        {
            "auths": {
                "git.example.com": {
                    "auth": "cm9ib3QkZWRnZTpyb2JvdC1zZWNyZXQ=",
                }
            }
        },
    )

    result = ota_main._registry_credential_check(ota_main.OtaStartRequest(mode="online"))

    assert result["ok"] is True
