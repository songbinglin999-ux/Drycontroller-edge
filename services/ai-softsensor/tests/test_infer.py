"""
ai-softsensor 推理模型单元测试
"""

import os
import sys
import types
from pathlib import Path

import numpy as np  # type: ignore[import]


PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]

LOG_ROOT = Path("/tmp/drycontroller-logs")
LOG_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("LOG_ROOT", str(LOG_ROOT))

for entry in (str(SERVICE_ROOT), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

SRC_ROOT = SERVICE_ROOT / "src"
pkg = types.ModuleType("src")
pkg.__path__ = [str(SRC_ROOT)]
sys.modules["src"] = pkg

from src.infer import SoftSensorModel  # noqa: E402


def _default_config():
    return {
        "input_features": ["A", "B"],
        "output": {
            "name": "MOISTURE.PREDICTED",
            "unit": "%",
            "range": [10.0, 20.0],
            "target": 12.0,
        },
    }


def test_preprocess_respects_feature_order():
    model = SoftSensorModel(_default_config())
    features = model.preprocess({"B": 2.0, "A": 1.0})
    assert features.shape == (1, 2)
    assert features.dtype == np.float32
    np.testing.assert_allclose(features[0], np.array([1.0, 2.0], dtype=np.float32))


def test_inference_returns_payload_and_clamps_range():
    model = SoftSensorModel(_default_config())
    result = model.inference({"A": 1.0, "B": 2.0})
    assert result is not None
    assert "predicted_moisture" in result
    assert "confidence" in result
    assert "inference_time_ms" in result
    assert 10.0 <= result["predicted_moisture"] <= 20.0
    assert 0.5 <= result["confidence"] <= 0.99


def test_confidence_bounds():
    model = SoftSensorModel(_default_config())
    high_deviation = model._estimate_confidence(100.0)
    low_deviation = model._estimate_confidence(model.target_moisture)
    assert 0.5 <= high_deviation <= 0.99
    assert 0.5 <= low_deviation <= 0.99

