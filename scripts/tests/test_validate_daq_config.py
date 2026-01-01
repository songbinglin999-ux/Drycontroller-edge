"""
scripts/validate-daq-config.py 单测

目标：
- validate() 对典型错误（duplicate/missing/bad range/type）能报错
- main() 对错误/成功返回正确退出码
"""

import sys
from pathlib import Path
import importlib.util

import pytest

PATH = Path(__file__).resolve()
ROOT = PATH.parents[2]  # Drycontroller-edge/

# scripts/validate-daq-config.py 文件名包含 '-'，不能用常规 import；用按路径加载。
_SCRIPT = ROOT / "scripts" / "validate-daq-config.py"
spec = importlib.util.spec_from_file_location("validate_daq_config_script", _SCRIPT)
assert spec and spec.loader
vdc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vdc)


def test_validate_reports_errors(tmp_path: Path):
    path = tmp_path / "tags.yaml"
    path.write_text(
        "acquisition:\n"
        "  scan_interval: -1\n"
        "tags:\n"
        "  s:\n"
        "    c:\n"
        "      - name: A\n"
        "        address: 1\n"
        "        type: int\n"
        "      - name: B\n"
        "        address: 1\n"
        "        type: int\n"
        "      - name: C\n"
        "        address: 2\n"
        "        type: badtype\n"
        "        range: [1]\n",
        encoding="utf-8",
    )
    errors = vdc.validate(path)
    assert errors, "应当返回错误列表"
    assert any("scan_interval" in e for e in errors)
    assert any("duplicate address" in e for e in errors)
    assert any("range must be" in e for e in errors)
    assert any("invalid type" in e for e in errors)


def test_main_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    good = tmp_path / "good.yaml"
    good.write_text(
        "acquisition:\n"
        "  scan_interval: 0.5\n"
        "tags:\n"
        "  s:\n"
        "    c:\n"
        "      - name: A\n"
        "        address: 1\n"
        "        type: int\n",
        encoding="utf-8",
    )
    # 成功场景：脚本会打印并正常返回（不 sys.exit(0)）
    monkeypatch.setattr(sys, "argv", ["validate-daq-config.py", "--file", str(good)])
    vdc.main()
    out = capsys.readouterr()
    assert "校验通过" in (out.out or out.err)

    bad = tmp_path / "bad.yaml"
    bad.write_text("tags: { s: { c: [ { name: A, address: 1, type: bad } ] } }\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["validate-daq-config.py", "--file", str(bad)])
    with pytest.raises(SystemExit) as ex2:
        vdc.main()
    assert ex2.value.code == 1
    out = capsys.readouterr()
    assert "配置校验失败" in (out.err or out.out)


