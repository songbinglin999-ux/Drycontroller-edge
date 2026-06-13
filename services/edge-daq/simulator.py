"""
独立运行的 PLC 数据模拟器。

- 每秒生成一批典型点位（温度、电流、变频反馈、状态量等）
- 通过现有 DataPublisher 同时发布 MQTT 与写入 InfluxDB
- 大屏/后端无需真实 PLC 即可联调
"""

import asyncio
from importlib import import_module, util
import logging
import math
import os
from pathlib import Path
import random
import signal
import time
from typing import Any, Dict, List

_structlog_spec = util.find_spec("structlog")
if _structlog_spec:
    structlog = import_module("structlog")
else:  # pragma: no cover
    class _StructLogShim:
        @staticmethod
        def get_logger(name=None):
            logging.basicConfig(level=logging.INFO)
            return logging.getLogger(name or "edge-daq-simulator")

    structlog = _StructLogShim()

from daq.publisher import DataPublisher  # noqa: E402

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None

logger: Any = structlog.get_logger(__name__)


DEFAULT_TAGS: List[Dict[str, Any]] = (
    [{"name": f"LL{i}", "signal_type": "DI", "type": "bool", "unit": ""} for i in range(1, 7)]
    + [{"name": f"FTT{i}", "signal_type": "AI", "type": "float", "unit": "℃"} for i in range(1, 4)]
    + [{"name": f"TT{i}", "signal_type": "AI", "type": "float", "unit": "℃"} for i in range(1, 3)]
    + [{"name": "MT1", "signal_type": "AI", "type": "float", "unit": "%"}]
    + [{"name": f"I_M{i}", "signal_type": "AI", "type": "float", "unit": "A"} for i in range(1, 16)]
    + [{"name": f"M{i}", "signal_type": "DI", "type": "bool", "unit": ""} for i in range(1, 16)]
    + [{"name": f"VFM{i}", "signal_type": "AI", "type": "float", "unit": "%/Hz"} for i in range(1, 4)]
    + [{"name": f"VF{i}", "signal_type": "DO", "type": "bool", "unit": ""} for i in range(1, 4)]
)


def _iter_tag_items(node: Any):
    if isinstance(node, dict):
        if isinstance(node.get("name"), str):
            yield node
        for value in node.values():
            yield from _iter_tag_items(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_tag_items(item)


def _candidate_tag_config_paths() -> List[Path]:
    paths: List[Path] = []
    for raw in (os.getenv("SIMULATOR_TAGS_CONFIG"), os.getenv("TAGS_CONFIG"), "/app/configs/tags.yaml"):
        if raw:
            paths.append(Path(raw))
    paths.append(Path(__file__).resolve().parents[2] / "configs" / "tags.yaml")
    return paths


def _profile_for_tag(tag: Dict[str, Any], index: int) -> Dict[str, Any]:
    signal_type = str(tag.get("signal_type", "")).upper()
    tag_type = str(tag.get("type", "")).lower()
    unit = tag.get("unit", "")

    if tag_type == "bool" or signal_type in {"DI", "DO"}:
        return {
            "name": tag["name"],
            "unit": unit,
            "base": 1.0,
            "amplitude": 0.0,
            "noise": 0.0,
            "min": 0.0,
            "max": 1.0,
            "precision": 0,
        }

    if unit == "℃":
        base, amplitude, min_value, max_value = 70.0, 5.0, 0.0, 200.0
    elif unit == "%":
        base, amplitude, min_value, max_value = 50.0, 8.0, 0.0, 100.0
    elif unit == "A":
        base, amplitude, min_value, max_value = 8.0 + (index % 5) * 3.0, 2.0, 0.0, 100.0
    elif unit == "%/Hz":
        base, amplitude, min_value, max_value = 45.0, 10.0, 0.0, 100.0
    else:
        base, amplitude, min_value, max_value = 1.0, 0.2, 0.0, None

    return {
        "name": tag["name"],
        "unit": unit,
        "base": base,
        "amplitude": amplitude,
        "noise": amplitude * 0.2,
        "period": 120.0 + index * 7.0,
        "min": min_value,
        "max": max_value,
        "precision": 2,
    }


def _load_tag_profiles() -> List[Dict]:
    if yaml is None:
        return []

    for path in _candidate_tag_config_paths():
        if not path.exists():
            continue
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tags = [tag for tag in _iter_tag_items(config.get("tags", {})) if tag.get("name")]
        if tags:
            return [_profile_for_tag(tag, index) for index, tag in enumerate(tags)]
    return []


def _default_tag_profiles() -> List[Dict]:
    return [_profile_for_tag(tag, index) for index, tag in enumerate(DEFAULT_TAGS)]


class RealtimeSimulator:
    """驱动 MQTT + InfluxDB 的实时数据流"""

    def __init__(self):
        self.publisher = DataPublisher()
        self.interval = float(os.getenv("SIMULATOR_INTERVAL", "1.0"))
        self.tag_profiles = _load_tag_profiles() or _default_tag_profiles()
        self.start_time = time.time()
        self._running = False

    def stop(self):
        self._running = False

    async def run(self):
        await self.publisher.connect()
        self._running = True
        logger.info(
            "simulator_started",
            tags=len(self.tag_profiles),
            interval=self.interval,
        )
        try:
            while self._running:
                await self._publish_batch()
                await asyncio.sleep(self.interval)
        finally:
            await self.publisher.disconnect()
            logger.info("simulator_stopped")

    async def _publish_batch(self):
        elapsed = time.time() - self.start_time
        payloads = []
        for profile in self.tag_profiles:
            value = self._generate_value(profile, elapsed)
            payloads.append(
                self.publisher.publish_tag_data(
                    tag_name=profile["name"],
                    value=value,
                    unit=profile.get("unit", ""),
                )
            )
        await asyncio.gather(*payloads)
        logger.info(
            "simulator_batch_published",
            points=len(self.tag_profiles),
        )

    def _generate_value(self, profile: Dict, elapsed: float) -> float:
        base = profile["base"]
        amplitude = profile.get("amplitude", 1.0)
        noise = profile.get("noise", amplitude * 0.2)
        period = profile.get("period", 120.0)
        precision = profile.get("precision", 2)

        periodic = math.sin((2 * math.pi * elapsed) / period) * amplitude
        jitter = random.uniform(-noise, noise)
        value = base + periodic + jitter

        min_value = profile.get("min")
        max_value = profile.get("max")
        if min_value is not None:
            value = max(min_value, value)
        if max_value is not None:
            value = min(max_value, value)

        return round(value, precision)


async def main():
    simulator = RealtimeSimulator()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_stop():
        logger.info("simulator_shutdown_signal")
        simulator.stop()
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop)
        except NotImplementedError:
            # Windows 子进程上可能不可用
            pass

    runner = asyncio.create_task(simulator.run())
    await stop_event.wait()
    await runner


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
