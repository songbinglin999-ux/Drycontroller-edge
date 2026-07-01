"""
ai-ctrl-optimizer 基础健康状态测试
"""

import importlib.util
import os
import sys
from pathlib import Path


PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]

LOG_ROOT = Path("/tmp/drycontroller-logs")
LOG_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_ROOT", str(LOG_ROOT))

for entry in (str(SERVICE_ROOT), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module spec: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main_mod = _load_module("ai_ctrl_optimizer_main", SERVICE_ROOT / "src" / "main.py")
OptimizerService = main_mod.OptimizerService


def test_health_payload_status_changes():
    service = OptimizerService()
    payload = service._health_payload()
    assert payload["status"] == "starting"

    service.running = True
    payload = service._health_payload()
    assert payload["status"] == "running"
