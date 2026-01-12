"""
边缘数据采集服务主入口

本模块是 edge-daq 服务的启动入口，负责：
- 连接 PLC 控制器（或使用模拟器）
- 周期性采集工业现场数据
- 将数据发布到 MQTT 和 InfluxDB

服务架构：
┌─────────────┐    ┌──────────────┐    ┌─────────────┐
│   PLC/模拟器  │ -> │  PLCReader   │ -> │ DataPublisher│
│  (数据源)    │    │  (采集调度)   │    │ (MQTT+Influx)│
└─────────────┘    └──────────────┘    └─────────────┘

环境变量配置：
- PLC_HOST: PLC 地址（默认 192.168.1.100）
- PLC_PORT: Modbus 端口（默认 502）
- USE_SIMULATOR: 是否使用模拟器（默认 true）
- HEALTH_PORT: 健康检查端口（默认 8080）
"""

import asyncio
import signal
import structlog
import yaml
import os
import time
from pathlib import Path

from aiohttp import web
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

try:
    # 部署镜像/monorepo 正常情况下应能直接 import
    from common.logging import setup_logging
except ModuleNotFoundError:
    # 单测/本地直接在 services/edge-daq 下运行时，可能缺少将 Drycontroller/ 加入 PYTHONPATH 的步骤
    # 这里做一次相对路径自修复：在父目录链上查找 common/logging/__init__.py，并把其父目录加入 sys.path
    import sys

    here = Path(__file__).resolve()
    injected = False
    for p in here.parents:
        if (p / "common" / "logging" / "__init__.py").exists():
            sys.path.insert(0, str(p))
            injected = True
            break
    if not injected:
        # 保持原异常语义，便于定位环境问题
        raise

    from common.logging import setup_logging

event_recorder = setup_logging("edge-daq")
logger = structlog.get_logger(__name__)

from daq.modbus_client import ModbusClient
from daq.plc_reader import PLCReader
from daq.publisher import DataPublisher
from daq.simulator import PLCSimulator
from daq.control import ControlSubscriber


class DAQService:
    """
    数据采集服务主类
    
    负责协调 PLC 连接、数据采集和发布的全流程。
    支持真实 PLC 和模拟器两种数据源模式。
    
    Attributes:
        running: 服务运行状态标志
        modbus_client: Modbus TCP 客户端（真实 PLC 模式）
        plc_reader: PLC 数据读取器
        publisher: 数据发布器（MQTT + InfluxDB）
        simulator: PLC 模拟器（模拟模式）
        config: 标签配置信息
    """
    
    def __init__(self):
        """初始化采集服务"""
        self.running = False
        self.modbus_client = None
        self.plc_reader = None
        self.publisher = None
        self.simulator = None
        self.config = None
        self.control_subscriber = None
        # 现场定位关键：记录当前 PLC 配置（用于 /ready 返回）
        self.plc_host: str | None = None
        self.plc_port: int | None = None
        self.use_simulator: bool | None = None
        self.plc_slave_id: int | None = None
        self.plc_connect_timeout: int | None = None
        # env/runtime_file（云端下发）等
        self.plc_config_source: str | None = None
        # 健康检查 HTTP 服务器
        self.health_runner = None
        self.health_site = None
        self.health_port = int(os.getenv("HEALTH_PORT", "8080"))
        self.validation_errors = []
        # 心跳状态
        self.heartbeat_miss_count = 0
        self.heartbeat_fail_count = 0
        self.control_suspended = False
        
    async def load_config(self):
        """
        加载标签配置文件
        
        配置文件定义了需要采集的 PLC 标签点，包括：
        - 标签名称和地址映射
        - 数据类型和单位
        - 报警阈值
        
        Returns:
            加载的配置字典
        """
        config_path = Path("/app/configs/tags.yaml")
        
        if not config_path.exists():
            logger.warning("config_file_not_found", path=str(config_path))
            return {}
        
        with open(config_path, "r") as f:
            self.config = yaml.safe_load(f)
        
        logger.info("config_loaded", tags_count=len(self.config.get("tags", {})))
        return self.config
    
    async def start(self):
        """
        启动数据采集服务
        
        启动流程：
        1. 加载标签配置
        2. 启动健康检查服务
        3. 初始化数据发布器
        4. 连接 PLC 或启动模拟器
        5. 启动周期性采集循环
        """
        logger.info("starting_daq_service")
        event_recorder.record_event(
            "service_starting",
            tags={"component": "edge-daq"},
        )
        
        # 加载标签配置
        await self.load_config()
        self.validation_errors = self._validate_config(self.config)
        if self.validation_errors:
            logger.error("config_validation_failed", errors=self.validation_errors)
            raise RuntimeError(f"config validation failed: {self.validation_errors}")

        # 启动健康检查 HTTP 服务
        await self._start_health_server()
        
        # 读取 PLC 连接配置（工业推荐：支持云端下发 runtime 配置覆盖 env）
        plc_host = os.getenv("PLC_HOST", "192.168.1.100")
        plc_port = int(os.getenv("PLC_PORT", "502"))
        plc_slave_id = int(os.getenv("PLC_SLAVE_ID", "1"))
        plc_connect_timeout = int(os.getenv("PLC_CONNECT_TIMEOUT", "5"))
        use_simulator = os.getenv("USE_SIMULATOR", "false").lower() == "true"
        heartbeat_interval = float(os.getenv("PLC_HEARTBEAT_INTERVAL", "5") or "5")
        heartbeat_timeout_cycles = int(os.getenv("PLC_HEARTBEAT_TIMEOUT_CYCLES", "3") or "3")

        # 云端下发的运行时配置：/app/configs-runtime/plc.yaml
        # - edge-api 写入该文件并触发重启 edge-daq 使其生效
        # - 只修改应用层通讯参数，不修改网卡配置（避免共用网口断联风险）
        runtime_cfg_path = Path("/app/configs-runtime/plc.yaml")
        if runtime_cfg_path.exists():
            try:
                with runtime_cfg_path.open("r", encoding="utf-8") as f:
                    runtime_cfg = yaml.safe_load(f) or {}
                if isinstance(runtime_cfg, dict):
                    plc_host = str(runtime_cfg.get("host") or plc_host)
                    plc_port = int(runtime_cfg.get("port") or plc_port)
                    plc_slave_id = int(runtime_cfg.get("slave_id") or plc_slave_id)
                    plc_connect_timeout = int(runtime_cfg.get("connect_timeout") or plc_connect_timeout)
                    self.plc_config_source = "runtime_file"
            except Exception as exc:
                logger.warning("runtime_plc_config_load_failed", path=str(runtime_cfg_path), error=str(exc))
                self.plc_config_source = "env"
        else:
            self.plc_config_source = "env"

        # 保存配置，便于 /ready 输出给现场定位
        self.plc_host = plc_host
        self.plc_port = plc_port
        self.plc_slave_id = plc_slave_id
        self.plc_connect_timeout = plc_connect_timeout
        self.use_simulator = use_simulator

        # 初始化数据发布器（连接 MQTT 和 InfluxDB）
        self.publisher = DataPublisher()
        await self.publisher.connect()
        
        # 根据配置选择数据源：模拟器或真实 PLC
        if use_simulator:
            logger.info("using_plc_simulator")
            self.simulator = PLCSimulator(self.config)
            self.plc_reader = PLCReader(self.simulator, self.publisher, self.config)
        else:
            logger.info("connecting_to_plc", host=plc_host, port=plc_port)
            self.modbus_client = ModbusClient(
                host=plc_host,
                port=plc_port,
                slave_id=plc_slave_id,
                connect_timeout=plc_connect_timeout,
            )
            await self.modbus_client.connect()
            self.plc_reader = PLCReader(
                self.modbus_client,
                self.publisher,
                self.config,
                heartbeat_interval=heartbeat_interval,
                heartbeat_timeout_cycles=heartbeat_timeout_cycles,
                heartbeat_state=self,
            )
            # 启动控制订阅
            self.control_subscriber = ControlSubscriber(
                modbus_client=self.modbus_client,
                config=self.config,
                line_id=self.publisher.line_id,
            )
            await self.control_subscriber.start()
        
        # 启动数据采集循环
        self.running = True
        await self.plc_reader.start()
        
        logger.info("daq_service_started")
        event_recorder.record_event(
            "service_started",
            tags={"component": "edge-daq"},
        )
    
    async def stop(self):
        """
        停止数据采集服务
        
        按顺序停止：采集器 -> 发布器 -> PLC 连接 -> 健康检查
        """
        logger.info("stopping_daq_service")
        event_recorder.record_event(
            "service_stopping",
            tags={"component": "edge-daq"},
        )
        
        self.running = False
        
        # 停止采集循环
        if self.plc_reader:
            await self.plc_reader.stop()
        
        # 断开发布器连接
        if self.publisher:
            # 停机尽力重放缓冲（超时可控）
            try:
                drain_timeout = float(os.getenv("PUBLISHER_DRAIN_TIMEOUT_SEC", "10") or "10")
            except Exception:
                drain_timeout = 10.0
            if hasattr(self.publisher, "drain"):
                await self.publisher.drain(timeout_sec=drain_timeout)
            await self.publisher.flush_buffers()
            await self.publisher.disconnect()
        
        # 断开 PLC 连接
        if self.modbus_client:
            await self.modbus_client.disconnect()

        # 停止控制订阅
        if self.control_subscriber:
            await self.control_subscriber.stop()
            self.control_subscriber = None

        # 停止健康检查服务
        await self._stop_health_server()
        
        logger.info("daq_service_stopped")
        event_recorder.record_event(
            "service_stopped",
            tags={"component": "edge-daq"},
        )

    async def _start_health_server(self):
        """
        启动健康检查 HTTP 服务器
        
        提供：
        - /health：liveness（进程活着 + 关键状态摘要）
        - /ready：readiness（依赖通路满足生产条件才返回 200）
        - /metrics：Prometheus 指标
        """
        if self.health_runner:
            return

        app = web.Application()

        async def health_handler(request):
            """处理健康检查请求"""
            return web.json_response(self._health_payload())

        async def ready_handler(request):
            """处理就绪检查请求（不就绪返回 503）"""
            payload = self._ready_payload()
            status = 200 if payload.get("ready") else 503
            return web.json_response(payload, status=status)

        async def metrics_handler(request):
            """Prometheus 指标"""
            data = generate_latest()
            # aiohttp 不允许在 content_type 参数中包含 charset；prometheus_client 常量带 charset
            return web.Response(body=data, headers={"Content-Type": CONTENT_TYPE_LATEST})

        app.router.add_get("/health", health_handler)
        app.router.add_get("/ready", ready_handler)
        app.router.add_get("/metrics", metrics_handler)
        self.health_runner = web.AppRunner(app)
        await self.health_runner.setup()
        self.health_site = web.TCPSite(self.health_runner, "0.0.0.0", self.health_port)
        await self.health_site.start()
        logger.info("health_server_started", port=self.health_port)

    async def _stop_health_server(self):
        """停止健康检查服务器"""
        if self.health_runner:
            await self.health_runner.cleanup()
            self.health_runner = None
            self.health_site = None
            logger.info("health_server_stopped")

    def _health_payload(self):
        """
        构建健康检查响应数据
        
        Returns:
            包含服务状态和详细信息的字典
        """
        # publisher_connected 需尽量反映“真实通路连通性”，但也兼容测试/桩对象只提供 connected 的情况
        pub_connected = False
        if self.publisher:
            if hasattr(self.publisher, "mqtt_connected") or hasattr(self.publisher, "influx_connected"):
                pub_connected = bool(
                    getattr(self.publisher, "mqtt_connected", False)
                    or getattr(self.publisher, "influx_connected", False)
                )
            else:
                pub_connected = bool(getattr(self.publisher, "connected", False))

        return {
            "success": True,
            "service": "edge-daq",
            "status": "running" if self.running else "starting",
            "timestamp": int(time.time()),
            "details": {
                "publisher_connected": pub_connected,
                "use_simulator": self.simulator is not None,
                "modbus": self.modbus_client.get_status() if self.modbus_client else None,
                "scan_interval": getattr(self.plc_reader, "scan_interval", None),
                "config_errors": len(self.validation_errors),
                "heartbeat_miss_count": getattr(self, "heartbeat_miss_count", None),
                "heartbeat_fail_count": getattr(self, "heartbeat_fail_count", None),
                "publisher": self.publisher.get_status() if (self.publisher and hasattr(self.publisher, "get_status")) else None,
                "control_suspended": self.control_suspended,
            },
        }

    def _ready_payload(self):
        """
        就绪探针：用于编排系统（K8s / docker-compose healthcheck）判断服务是否可以对外提供“有效数据”。
        """
        # 服务主循环已启动
        running = bool(self.running)

        # 数据源：模拟器即认为可用；真实 PLC 则要求 modbus 连接可用
        modbus_connected = bool(self.modbus_client and self.modbus_client.get_status().get("connected")) if self.simulator is None else True
        modbus_ok = bool(modbus_connected)

        # 发布通路：至少一个（MQTT/Influx）在线，避免“看似健康但实际没出数”
        pub_ok = False
        pub_details = None
        if self.publisher:
            pub_details = self.publisher.get_status() if hasattr(self.publisher, "get_status") else None
            pub_ok = bool(getattr(self.publisher, "mqtt_connected", False) or getattr(self.publisher, "influx_connected", False))

        ready = running and modbus_ok and pub_ok and not self.control_suspended and not self.validation_errors

        reason = None
        hint = None
        warnings: list[str] = []
        if not running:
            reason = "SERVICE_STARTING"
        elif self.validation_errors:
            reason = "CONFIG_ERROR"
            hint = f"配置校验失败：{self.validation_errors}"
        elif self.control_suspended:
            reason = "CONTROL_SUSPENDED"
            hint = "PLC 心跳异常触发控制挂起（为安全起见停止下发控制）。"
        elif not pub_ok:
            reason = "PUBLISHER_OFFLINE"
            hint = "发布通路（MQTT/Influx）未连通：请检查 mosquitto/influxdb 以及账号密码。"
        elif not modbus_ok:
            reason = "PLC_OFFLINE"
            warnings.append("PLC_OFFLINE")
            # 现场最常见：容器内误配成 127.0.0.1（指向容器自己），必然连不上 PLC
            if (self.plc_host or "").strip() in ("127.0.0.1", "localhost"):
                hint = "PLC/Modbus 不通：当前 PLC_HOST=127.0.0.1（容器内指向自身）。请改为 PLC 实际 IP（如 192.168.x.x），或使用 USE_SIMULATOR=true 做联调。"
            else:
                hint = "PLC/Modbus 不通：请检查 PLC 是否上电、网络是否可达、Modbus 端口/从站地址是否正确。"

        return {
            "ready": ready,
            "service": "edge-daq",
            "timestamp": int(time.time()),
            "reason": reason,
            "hint": hint,
            "warnings": warnings,
            "details": {
                "running": running,
                "modbus_ok": modbus_ok,
                "modbus_connected": modbus_connected,
                "publisher_ok": pub_ok,
                "control_suspended": self.control_suspended,
                "config_errors": len(self.validation_errors),
                "publisher": pub_details,
                "plc": {
                    "host": self.plc_host,
                    "port": self.plc_port,
                    "use_simulator": bool(self.simulator is not None),
                    "slave_id": self.plc_slave_id,
                    "connect_timeout": self.plc_connect_timeout,
                    "config_source": self.plc_config_source,
                    "runtime_config_path": "/app/configs-runtime/plc.yaml",
                },
            },
        }

    def _validate_config(self, config: dict) -> list:
        """
        基础配置校验：必填字段、重复地址、range 结构。
        返回错误列表，不抛异常，便于健康检查上报。
        """
        errors = []
        tags_cfg = (config or {}).get("tags", {})
        acquisition = (config or {}).get("acquisition", {})
        if acquisition:
            scan_interval = acquisition.get("scan_interval")
            if scan_interval is not None and float(scan_interval) <= 0:
                errors.append("invalid_scan_interval")
            batch_size = acquisition.get("batch_size")
            if batch_size is not None and int(batch_size) < 0:
                errors.append("invalid_batch_size")
        heartbeat_cfg = (config or {}).get("heartbeat", {})
        if heartbeat_cfg:
            fc = int(heartbeat_cfg.get("function_code", 3) or 3)
            if fc not in (3, 4, 16):
                errors.append("invalid_heartbeat_function_code")
            for field in ("read_address", "write_address"):
                if heartbeat_cfg.get(field) is not None:
                    try:
                        int(heartbeat_cfg.get(field))
                    except Exception:
                        errors.append(f"invalid_heartbeat_{field}")
        seen = {}
        for section, categories in tags_cfg.items():
            if not isinstance(categories, dict):
                continue
            for category, tags in categories.items():
                if not isinstance(tags, list):
                    continue
                for tag in tags:
                    name = tag.get("name")
                    addr = tag.get("address")
                    t = tag.get("type")
                    if not name or addr is None or not t:
                        errors.append(f"missing_field:{section}.{category}")
                        continue
                    function_code = int(tag.get("function_code", 3) or 3)
                    if function_code not in (3, 4, 16):
                        errors.append(f"invalid_function_code:{name}:{function_code}")
                    byte_order = tag.get("byte_order", "big")
                    if byte_order not in ("big", "little"):
                        errors.append(f"invalid_byte_order:{name}:{byte_order}")
                    key = (addr, function_code)
                    if key in seen:
                        errors.append(f"duplicate_address:{addr}")
                    else:
                        seen[key] = name
                    rng = tag.get("range")
                    if rng and (not isinstance(rng, (list, tuple)) or len(rng) != 2):
                        errors.append(f"invalid_range:{name}")
                    alarm_high = tag.get("alarm_high")
                    alarm_low = tag.get("alarm_low")
                    if alarm_high is not None and alarm_low is not None and alarm_low > alarm_high:
                        errors.append(f"invalid_alarm_range:{name}")
                    if t not in ("float", "int", "int32", "uint32", "bool"):
                        errors.append(f"invalid_type:{name}:{t}")
        return errors
    
    async def on_plc_heartbeat_timeout(self):
        """
        PLC 心跳超时的降级处理：
        - 停止控制订阅，避免误下发
        - 记录事件，供监控告警
        """
        if self.control_suspended:
            return
        logger.error("plc_heartbeat_timeout_triggered")
        event_recorder.record_event(
            "plc_heartbeat_timeout",
            severity="error",
            tags={"component": "edge-daq"},
        )
        self.control_suspended = True
        if self.control_subscriber:
            try:
                await self.control_subscriber.stop()
            finally:
                self.control_subscriber = None


async def main():
    """
    服务主函数
    
    负责：
    1. 创建服务实例
    2. 注册系统信号处理（优雅关闭）
    3. 启动服务并保持运行
    4. 处理异常和清理资源
    """
    service = DAQService()
    
    # 获取事件循环并注册信号处理
    loop = asyncio.get_event_loop()
    
    def signal_handler():
        """处理 SIGTERM/SIGINT 信号，触发优雅关闭"""
        logger.info("received_shutdown_signal")
        asyncio.create_task(service.stop())
    
    # 注册 SIGTERM（Docker stop）和 SIGINT（Ctrl+C）信号
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        # 启动采集服务
        await service.start()
        
        # 主循环：保持服务运行直到收到停止信号
        while service.running:
            await asyncio.sleep(1)
    
    except Exception as e:
        logger.error("service_error", error=str(e), exc_info=True)
        event_recorder.record_event(
            "service_error",
            severity="error",
            fields={"error": str(e)},
            tags={"component": "edge-daq"},
        )
    
    finally:
        # 确保资源被清理
        await service.stop()


if __name__ == "__main__":
    # 运行异步主函数
    asyncio.run(main())

