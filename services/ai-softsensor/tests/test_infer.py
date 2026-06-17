"""
ai-softsensor 推理模型单元测试
"""

import os
import sys
import types
from pathlib import Path
from typing import Any, cast

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


def test_simulate_formula_uses_canonical_48_tags():
    config = _default_config()
    config["output"]["range"] = [0.0, 100.0]
    model = SoftSensorModel(config)

    dry_conditions = {
        "FTT1": 110.0,
        "FTT2": 108.0,
        "FTT3": 106.0,
        "TT1": 145.0,
        "TT2": 140.0,
        "VFM1": 25.0,
        "VFM2": 26.0,
        "VFM3": 27.0,
        "VF1": 55.0,
        "VF2": 54.0,
        "VF3": 53.0,
        "I_M3": 14.0,
        "I_M5": 15.0,
        "I_M7": 16.0,
        "I_M9": 15.0,
        "MT1": 9.5,
    }
    wet_conditions = {
        "FTT1": 70.0,
        "FTT2": 72.0,
        "FTT3": 74.0,
        "TT1": 95.0,
        "TT2": 100.0,
        "VFM1": 48.0,
        "VFM2": 47.0,
        "VFM3": 46.0,
        "VF1": 22.0,
        "VF2": 23.0,
        "VF3": 24.0,
        "I_M3": 30.0,
        "I_M5": 31.0,
        "I_M7": 29.0,
        "I_M9": 32.0,
        "MT1": 18.0,
    }

    assert model._simulate_formula(dry_conditions) < model._simulate_formula(wet_conditions)


def test_simulate_formula_uses_target_when_mt1_is_none():
    model = SoftSensorModel(_default_config())

    assert model._simulate_formula(cast(Any, {"MT1": None})) == model.target_moisture


def test_confidence_bounds():
    model = SoftSensorModel(_default_config())
    high_deviation = model._estimate_confidence(100.0)
    low_deviation = model._estimate_confidence(model.target_moisture)
    assert 0.5 <= high_deviation <= 0.99
    assert 0.5 <= low_deviation <= 0.99
