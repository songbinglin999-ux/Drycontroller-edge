"""
scripts/validate-tags.py 单测

目标：
- validate_tags 返回的错误信息覆盖 io-map 位号正则
- main() 对错误/成功的退出码
"""

import sys
from pathlib import Path
import importlib.util

import pytest

PATH = Path(__file__).resolve()
ROOT = PATH.parents[2]  # Drycontroller-edge/

# scripts/validate-tags.py 文件名包含 '-'，不能用常规 import；用按路径加载。
_SCRIPT = ROOT / "scripts" / "validate-tags.py"
spec = importlib.util.spec_from_file_location("validate_tags_script", _SCRIPT)
assert spec and spec.loader
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)


def test_validate_tags_ok():
    cfg = {
        "tags": {
            "detection": {
                "AI": [
                    {"name": "FTT1", "type": "float"},
                    {"name": "I_M15", "type": "float"},
                ]
            }
        },
    }
    assert vt.validate_tags(cfg) == []


def test_validate_tags_reports_format_errors():
    cfg = {
        "tags": {
            "detection": {
                "AI": [
                    {"name": "I-M1"},
                    {"name": "dryer1"},
                    {"name": "FTT.1"},
                ]
            }
        },
    }
    errors = vt.validate_tags(cfg)
    assert len(errors) == 3
    assert all("不符合格式" in e for e in errors)


def test_main_exit_codes(tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    good = tmp_path / "tags.yaml"
    good.write_text(
        "tags:\n"
        "  detection:\n"
        "    AI:\n"
        "      - name: FTT1\n"
        "        type: float\n",
        encoding="utf-8",
    )

    # 成功场景：脚本会打印并正常返回（不 sys.exit(0)）
    monkeypatch.setattr(sys, "argv", ["validate-tags.py", "--file", str(good)])
    vt.main()
    out = capsys.readouterr()
    assert "符合约定" in (out.out or out.err)

    bad = tmp_path / "bad.yaml"
    bad.write_text("tags: { detection: { AI: [ { name: I-M1 } ] } }\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["validate-tags.py", "--file", str(bad)])
    with pytest.raises(SystemExit) as ex2:
        vt.main()
    assert ex2.value.code == 1
    out = capsys.readouterr()
    assert "校验失败" in (out.err or out.out)

