"""
edge-ota-agent 工具函数单元测试
"""

import os
import sys
from pathlib import Path
import importlib

import pytest


PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[2]

for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

def _load_module(tmp_path: Path):
    os.environ["OTA_DATA_DIR"] = str(tmp_path / "data")
    os.environ["OTA_LOG_DIR"] = str(tmp_path / "logs")
    os.environ["OTA_WORK_DIR"] = str(tmp_path / "work")
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

