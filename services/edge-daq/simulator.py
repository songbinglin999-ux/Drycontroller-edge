"""
独立运行的 PLC 数据模拟器。

- 每秒生成一批典型点位（温度、湿度、链速、风机频率等）
- 通过现有 DataPublisher 同时发布 MQTT 与写入 InfluxDB
- 大屏/后端无需真实 PLC 即可联调
"""

import asyncio
from importlib import import_module, util
import logging
import math
import os
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

logger: Any = structlog.get_logger(__name__)


DEFAULT_TAG_PROFILES: List[Dict] = [
    {
        "name": "DRYER1.IN.TEMP",
        "unit": "°C",
        "base": 85.0,
        "amplitude": 4.0,
        "noise": 0.8,
        "period": 180.0,
        "min": 60.0,
        "max": 120.0,
        "precision": 2,
    },
    {
        "name": "DRYER1.OUT.TEMP",
        "unit": "°C",
        "base": 78.0,
        "amplitude": 3.0,
        "noise": 0.7,
        "period": 200.0,
        "min": 55.0,
        "max": 110.0,
        "precision": 2,
    },
    {
        "name": "DRYER1.CHAIN.SPEED",
        "unit": "m/min",
        "base": 4.2,
        "amplitude": 0.25,
        "noise": 0.05,
        "period": 240.0,
        "min": 3.5,
        "max": 5.5,
        "precision": 3,
    },
    {
        "name": "DRYER1.FAN.SPEED",
        "unit": "Hz",
        "base": 35.0,
        "amplitude": 4.0,
        "noise": 0.8,
        "period": 150.0,
        "min": 20.0,
        "max": 45.0,
        "precision": 2,
    },
    {
        "name": "DRYER1.TORQUE",
        "unit": "Nm",
        "base": 240.0,
        "amplitude": 25.0,
        "noise": 5.0,
        "period": 260.0,
        "min": 120.0,
        "max": 420.0,
        "precision": 1,
    },
    {
        "name": "DRYER2.IN.TEMP",
        "unit": "°C",
        "base": 65.0,
        "amplitude": 4.5,
        "noise": 0.9,
        "period": 210.0,
        "min": 45.0,
        "max": 110.0,
        "precision": 2,
    },
    {
        "name": "DRYER2.OUT.TEMP",
        "unit": "°C",
        "base": 55.0,
        "amplitude": 3.5,
        "noise": 0.7,
        "period": 190.0,
        "min": 35.0,
        "max": 90.0,
        "precision": 2,
    },
    {
        "name": "DRYER2.CHAIN.SPEED",
        "unit": "m/min",
        "base": 4.6,
        "amplitude": 0.3,
        "noise": 0.05,
        "period": 200.0,
        "min": 3.8,
        "max": 5.8,
        "precision": 3,
    },
    {
        "name": "DRYER2.FAN.SPEED",
        "unit": "Hz",
        "base": 31.0,
        "amplitude": 3.0,
        "noise": 0.7,
        "period": 140.0,
        "min": 18.0,
        "max": 42.0,
        "precision": 2,
    },
    {
        "name": "DRYER2.TORQUE",
        "unit": "Nm",
        "base": 210.0,
        "amplitude": 20.0,
        "noise": 4.0,
        "period": 230.0,
        "min": 100.0,
        "max": 380.0,
        "precision": 1,
    },
    {
        "name": "FERM.CHAMBER.TEMP",
        "unit": "°C",
        "base": 42.0,
        "amplitude": 2.0,
        "noise": 0.4,
        "period": 300.0,
        "min": 30.0,
        "max": 60.0,
        "precision": 2,
    },
    {
        "name": "FERM.HUMIDITY",
        "unit": "%",
        "base": 65.0,
        "amplitude": 6.0,
        "noise": 1.5,
        "period": 320.0,
        "min": 40.0,
        "max": 95.0,
        "precision": 2,
    },
    {
        "name": "EXHAUST.TEMP",
        "unit": "°C",
        "base": 185.0,
        "amplitude": 8.0,
        "noise": 1.2,
        "period": 260.0,
        "min": 120.0,
        "max": 260.0,
        "precision": 2,
    },
    {
        "name": "EXHAUST.RECOVERY.VALVE",
        "unit": "%",
        "base": 45.0,
        "amplitude": 20.0,
        "noise": 4.0,
        "period": 400.0,
        "min": 0.0,
        "max": 100.0,
        "precision": 1,
    },
]


class RealtimeSimulator:
    """驱动 MQTT + InfluxDB 的实时数据流"""

    def __init__(self):
        self.publisher = DataPublisher()
        self.interval = float(os.getenv("SIMULATOR_INTERVAL", "1.0"))
        self.tag_profiles = DEFAULT_TAG_PROFILES
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

