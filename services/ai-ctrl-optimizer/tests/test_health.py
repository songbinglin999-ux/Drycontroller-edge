"""
ai-ctrl-optimizer 基础健康状态测试
"""

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

from src.main import OptimizerService  # noqa: E402


def test_health_payload_status_changes():
    service = OptimizerService()
    payload = service._health_payload()
    assert payload["status"] == "starting"

    service.running = True
    payload = service._health_payload()
    assert payload["status"] == "running"

