"""
PLC 数据读取器

本模块负责从 PLC 控制器周期性读取数据，并将数据发布到消息队列和时序数据库。

主要功能：
- 按配置的扫描周期轮询 PLC 数据
- 支持 Modbus TCP 协议读取
- 自动检测报警条件并发布报警消息
- 数据范围校验

采集流程：
1. 按配置的 scan_interval 触发扫描
2. 遍历所有配置的标签点
3. 通过 Modbus 读取寄存器值
4. 校验范围和报警阈值
5. 发布到 MQTT 和 InfluxDB
"""

import asyncio
import os
import structlog
import time
from prometheus_client import Counter, Gauge, Histogram
from prometheus_client.registry import REGISTRY

logger = structlog.get_logger(__name__)
if "plc_heartbeat_missing_total" in REGISTRY._names_to_collectors:
    PLC_HEARTBEAT_MISSING = REGISTRY._names_to_collectors["plc_heartbeat_missing_total"]
else:
    PLC_HEARTBEAT_MISSING = Counter("plc_heartbeat_missing_total", "PLC heartbeat missing detected")


def _get_or_create_collector(name: str, factory, *args, **kwargs):
    """
    避免在单测/热重载场景重复注册 Prometheus 指标。
    """
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    return factory(name, *args, **kwargs)


PLC_SCAN_LOOP_ERROR = _get_or_create_collector(
    "plc_scan_loop_error_total",
    Counter,
    "PLC scan loop errors",
)
PLC_SCAN_OVERRUN = _get_or_create_collector(
    "plc_scan_overrun_total",
    Counter,
    "PLC scan cycle overrun (duration > scan_interval)",
)
PLC_SCAN_SKIPPED = _get_or_create_collector(
    "plc_scan_skipped_total",
    Counter,
    "PLC scan cycles skipped due to sustained overrun",
)
PLC_SCAN_LAG_SEC = _get_or_create_collector(
    "plc_scan_lag_seconds",
    Gauge,
    "PLC scan start lag vs scheduled time (monotonic seconds)",
)
PLC_SCAN_SLEEP_SEC = _get_or_create_collector(
    "plc_scan_sleep_seconds",
    Gauge,
    "PLC scan loop sleep duration (seconds)",
)
PLC_SCAN_DURATION_SEC = _get_or_create_collector(
    "plc_scan_duration_seconds",
    Histogram,
    "PLC scan duration (seconds)",
    buckets=(0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10),
)
PLC_SCAN_BACKPRESSURE_DELAY_SEC = _get_or_create_collector(
    "plc_scan_backpressure_delay_seconds",
    Gauge,
    "PLC scan backpressure delay applied (seconds)",
)
PLC_SCAN_BACKPRESSURE_TOTAL = _get_or_create_collector(
    "plc_scan_backpressure_total",
    Counter,
    "PLC scan cycles delayed due to downstream backpressure",
)


class PLCReader:
    """
    PLC 数据读取器
    
    负责周期性采集 PLC 数据并发布到消息队列和数据库。
    
    Attributes:
        modbus_client: Modbus 客户端（或模拟器）
        publisher: 数据发布器
        config: 标签配置
        scan_interval: 扫描周期（秒）
    """
    
    def __init__(self, modbus_client, publisher, config, heartbeat_interval: float = 5.0, heartbeat_timeout_cycles: int = 3, heartbeat_state=None):
        """
        初始化读取器
        
        Args:
            modbus_client: Modbus 客户端实例
            publisher: 数据发布器实例
            config: 标签配置字典
            heartbeat_interval: 心跳写入周期（秒）
            heartbeat_timeout_cycles: 连续几次未收到心跳算超时
            heartbeat_state: 存放心跳状态的对象（可复用 DAQService）
        """
        self.modbus_client = modbus_client
        self.publisher = publisher
        self.config = config
        self.running = False
        # 从环境或配置读取扫描间隔，默认 1 秒
        env_scan = float(os.getenv("PLC_SCAN_INTERVAL", "0") or "0")
        self.scan_interval = env_scan if env_scan > 0 else config.get("acquisition", {}).get("scan_interval", 1.0)
        self.batch_size = int(config.get("acquisition", {}).get("batch_size", 0) or 0)
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout_cycles = heartbeat_timeout_cycles
        self.heartbeat_state = heartbeat_state
        self.last_plc_heartbeat = None
        self.heartbeat_write_value = 0
        self.heartbeat_cfg = (config or {}).get("heartbeat", {})  # {read_address, write_address, function_code}
        self._scan_task = None
    
    async def start(self):
        """
        启动数据采集
        
        创建后台任务执行周期性扫描
        """
        self.running = True
        logger.info("plc_reader_started", scan_interval=self.scan_interval)
        
        # 启动采集循环任务
        # 存储 task 便于优雅关闭
        self._scan_task = asyncio.create_task(self._scan_loop(), name="plc_scan_loop")
    
    async def stop(self):
        """停止数据采集"""
        self.running = False
        logger.info("plc_reader_stopped")
        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
            self._scan_task = None
    
    async def _scan_loop(self):
        """
        数据采集主循环
        
        按固定周期执行扫描，自动补偿采集耗时
        """
        # 工业级调度：使用 monotonic，避免系统时间跳变导致采样抖动
        scan_interval = float(self.scan_interval or 1.0)
        if scan_interval <= 0:
            scan_interval = 1.0

        next_run = time.monotonic()
        next_heartbeat = time.monotonic() + float(self.heartbeat_interval or 5.0)

        while self.running:
            # 背压：下游长期不可用时适度放慢采集，避免缓冲爆炸
            bp_delay = 0.0
            try:
                if self.publisher and hasattr(self.publisher, "get_backpressure_delay"):
                    bp_delay = float(self.publisher.get_backpressure_delay() or 0.0)
            except (TypeError, ValueError):
                bp_delay = 0.0
            if bp_delay > 0:
                PLC_SCAN_BACKPRESSURE_TOTAL.inc()
                PLC_SCAN_BACKPRESSURE_DELAY_SEC.set(bp_delay)
                await asyncio.sleep(bp_delay)
            else:
                PLC_SCAN_BACKPRESSURE_DELAY_SEC.set(0)

            scheduled = next_run
            start_mono = time.monotonic()
            PLC_SCAN_LAG_SEC.set(max(0.0, start_mono - scheduled))

            try:
                with PLC_SCAN_DURATION_SEC.time():
                    # 读取所有配置的标签
                    await self._read_all_tags()

                    # 心跳读写（用 monotonic 调度）
                    now_mono = time.monotonic()
                    if now_mono >= next_heartbeat:
                        await self._heartbeat_cycle()
                        # 避免长时间阻塞后“补写多次”引发风暴：只推进到下一次
                        next_heartbeat = now_mono + float(self.heartbeat_interval or 5.0)

            except asyncio.CancelledError:
                logger.info("scan_loop_cancelled")
                self.running = False
                break
            except (OSError, RuntimeError, TypeError, ValueError) as e:
                PLC_SCAN_LOOP_ERROR.inc()
                logger.error("scan_error", error=str(e), exc_info=True)

            # 固定周期推进；若持续超时则跳过已过期周期并计数
            end_mono = time.monotonic()
            duration = end_mono - start_mono
            if duration > scan_interval:
                PLC_SCAN_OVERRUN.inc()

            next_run = scheduled + scan_interval
            now_mono = time.monotonic()
            if next_run < now_mono:
                # 计算“跳过了多少周期”
                missed = int((now_mono - next_run) // scan_interval) + 1
                PLC_SCAN_SKIPPED.inc(missed)
                next_run = now_mono

            sleep_time = max(0.0, next_run - time.monotonic())
            PLC_SCAN_SLEEP_SEC.set(sleep_time)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
    
    async def _read_all_tags(self):
        """
        读取所有配置的标签数据
        
        遍历 tags.yaml 中定义的所有标签，按分组/类别组织
        """
        tags_config = self.config.get("tags", {})
        flat_tags = []
        
        # 遍历标签配置：section（分区）-> category（类别）-> tags（标签列表）
        for section, categories in tags_config.items():
            if not isinstance(categories, dict):
                continue
            
            for category, tags in categories.items():
                if not isinstance(tags, list):
                    continue
                
                for tag_config in tags:
                    flat_tags.append(tag_config)

        # 若 batch_size>1 且有标签，则批量读取，否则逐标签读取
        if self.batch_size and self.batch_size > 1 and flat_tags:
            await self._read_tags_batched(flat_tags, self.batch_size)
        else:
            for tag_config in flat_tags:
                await self._read_tag(tag_config)

    async def _read_tags_batched(self, tags: list, batch_size: int):
        """
        按功能码/字节序/寄存器长度分组，合并连续地址批量读取，降低报文数量。
        """
        # 预处理标签，过滤缺失必填的
        prepared = []
        for t in tags:
            name = t.get("name")
            addr = t.get("address")
            if not name or addr is None:
                continue
            tag_type = str(t.get("type", "float")).lower()
            function_code = int(t.get("function_code", 3))
            byte_order = t.get("byte_order", "big")
            scale = float(t.get("scale", 1.0) or 1.0)
            unit = t.get("unit", "")
            reg_len = 2 if tag_type in ("float", "int32", "uint32") else 1
            prepared.append({
                "raw": t,
                "name": name,
                "address": int(addr),
                "type": tag_type,
                "function_code": function_code,
                "byte_order": byte_order,
                "scale": scale,
                "unit": unit,
                "reg_len": reg_len,
            })

        # 分组：同功能码/字节序/寄存器长度
        from collections import defaultdict
        groups = defaultdict(list)
        for p in prepared:
            key = (p["function_code"], p["byte_order"], p["reg_len"])
            groups[key].append(p)

        for (fc, bo, reg_len), items in groups.items():
            items = sorted(items, key=lambda x: x["address"])

            # 划分连续区间
            batch = []
            current_start = None
            current_regs = 0
            for itm in items:
                if current_start is None:
                    current_start = itm["address"]
                    current_regs = reg_len
                    batch = [itm]
                    continue
                expected_next = current_start + current_regs
                if itm["address"] == expected_next and (current_regs + reg_len) <= batch_size:
                    batch.append(itm)
                    current_regs += reg_len
                else:
                    await self._read_batch_range(current_start, batch, fc, bo)
                    current_start = itm["address"]
                    current_regs = reg_len
                    batch = [itm]
            if batch:
                await self._read_batch_range(current_start, batch, fc, bo)

    async def _read_batch_range(self, start_addr: int, batch: list, function_code: int, byte_order: str):
        """
        读取一个连续区间并拆分发布。
        """
        total_regs = sum(item["reg_len"] for item in batch)
        registers = await self.modbus_client.read_registers(start_addr, total_regs, function_code=function_code)
        if not registers or len(registers) < total_regs:
            logger.error("batch_read_failed", start=start_addr, regs=total_regs)
            return

        offset = 0
        import struct
        endian = ">" if byte_order == "big" else "<"

        for item in batch:
            regs = registers[offset : offset + item["reg_len"]]
            offset += item["reg_len"]

            value = None
            try:
                if item["type"] == "float":
                    bytes_data = struct.pack(f"{endian}HH", regs[0], regs[1])
                    value = struct.unpack(f"{endian}f", bytes_data)[0]
                elif item["type"] in ("int32", "uint32"):
                    fmt = f"{endian}{'i' if item['type']=='int32' else 'I'}"
                    bytes_data = struct.pack(f"{endian}HH", regs[0], regs[1])
                    value = struct.unpack(fmt, bytes_data)[0]
                elif item["type"] == "bool":
                    value = bool(regs[0])
                else:
                    value = regs[0]

                if value is not None and isinstance(value, (int, float)) and item["scale"] != 1.0:
                    value = value * item["scale"]

                await self._after_read_value(item["raw"], value, item["unit"])

            except (IndexError, KeyError, RuntimeError, TypeError, ValueError, struct.error) as e:
                logger.error("batch_read_parse_error", tag=item["name"], error=str(e))

    async def _after_read_value(self, tag_config: dict, value, unit: str):
        """
        对读取到的值进行校验、报警和发布（供单标签与批量共用）。
        """
        tag_name = tag_config.get("name")
        if value is None:
            return

        # 范围校验
        value_range = tag_config.get("range")
        if value_range:
            min_val, max_val = value_range
            if value < min_val or value > max_val:
                logger.warning(
                    "value_out_of_range",
                    tag=tag_name,
                    value=value,
                    range=value_range,
                )
        
        # 报警检测
        alarm_high = tag_config.get("alarm_high")
        alarm_low = tag_config.get("alarm_low")
        if alarm_high and value > alarm_high:
            logger.warning(
                "alarm_high_triggered",
                tag=tag_name,
                value=value,
                threshold=alarm_high,
            )
            await self._publish_alarm(tag_name, "high", value, alarm_high)
        if alarm_low and value < alarm_low:
            logger.warning(
                "alarm_low_triggered",
                tag=tag_name,
                value=value,
                threshold=alarm_low,
            )
            await self._publish_alarm(tag_name, "low", value, alarm_low)
        
        # 发布
        await self.publisher.publish_tag_data(
            tag_name=tag_name,
            value=value,
            unit=unit,
            timestamp=time.time(),
        )

    async def _publish_alarm(self, tag_name: str, alarm_type: str, value: float, threshold: float):
        """
        发布报警消息
        """
        await self.publisher.publish_alarm(
            tag_name=tag_name,
            alarm_type=alarm_type,
            value=value,
            threshold=threshold,
        )

    async def _heartbeat_cycle(self):
        """
        进行一次心跳读写：
        - 可选读：从 heartbeat.read_address 读取 PLC 心跳值，检测变化
        - 可选写：向 heartbeat.write_address 写入递增计数
        """
        hb_cfg = self.heartbeat_cfg or {}
        read_addr = hb_cfg.get("read_address")
        write_addr = hb_cfg.get("write_address")
        fc = int(hb_cfg.get("function_code", 3))

        # 读取 PLC 心跳
        if read_addr is not None:
            regs = await self.modbus_client.read_registers(read_addr, 1, function_code=fc)
            if regs:
                val = regs[0]
                if self.last_plc_heartbeat is None:
                    self.last_plc_heartbeat = val
                    if self.heartbeat_state:
                        self.heartbeat_state.heartbeat_miss_count = 0
                else:
                    if val == self.last_plc_heartbeat:
                        if self.heartbeat_state:
                            self.heartbeat_state.heartbeat_miss_count += 1
                            if self.heartbeat_state.heartbeat_miss_count >= self.heartbeat_timeout_cycles:
                                logger.error("plc_heartbeat_missing", read_address=read_addr)
                                PLC_HEARTBEAT_MISSING.inc()
                                # 心跳超时触发降级与报警
                                await self._publish_alarm("PLC_HEARTBEAT", "missing", val, 0)
                                if hasattr(self.heartbeat_state, "on_plc_heartbeat_timeout"):
                                    await self.heartbeat_state.on_plc_heartbeat_timeout()
                    else:
                        self.last_plc_heartbeat = val
                        if self.heartbeat_state:
                            self.heartbeat_state.heartbeat_miss_count = 0
            else:
                if self.heartbeat_state:
                    self.heartbeat_state.heartbeat_fail_count += 1
                logger.error("plc_heartbeat_read_failed", read_address=read_addr)

        # 写入我们自己的心跳
        if write_addr is not None:
            self.heartbeat_write_value = (self.heartbeat_write_value + 1) % 65535
            ok = await self.modbus_client.write_register(write_addr, self.heartbeat_write_value)
            if not ok and self.heartbeat_state:
                self.heartbeat_state.heartbeat_fail_count += 1
    
    async def _read_tag(self, tag_config: dict):
        """
        读取单个标签的值
        
        处理流程：
        1. 从配置获取标签元信息
        2. 通过 Modbus 读取寄存器值
        3. 校验数值范围
        4. 检查报警条件
        5. 发布数据
        
        Args:
            tag_config: 标签配置字典，包含：
                - name: 标签名称
                - address: Modbus 寄存器地址
                - type: 数据类型（float/int）
                - unit: 工程单位
                - range: 有效范围 [min, max]
                - alarm_high/alarm_low: 报警阈值
        """
        tag_name = tag_config.get("name")
        address = tag_config.get("address")
        tag_type = str(tag_config.get("type", "float")).lower()
        unit = tag_config.get("unit", "")
        function_code = int(tag_config.get("function_code", 3))
        byte_order = tag_config.get("byte_order", "big")
        scale = float(tag_config.get("scale", 1.0) or 1.0)
        
        # 配置校验
        if not tag_name or address is None:
            return
        
        try:
            # 根据数据类型选择读取方式
            if tag_type == "float":
                value = await self.modbus_client.read_float(address, byte_order=byte_order, function_code=function_code)
            elif tag_type in ("int32", "uint32"):
                value = await self.modbus_client.read_int32(
                    address,
                    signed=(tag_type == "int32"),
                    byte_order=byte_order,
                    function_code=function_code,
                )
            else:
                registers = await self.modbus_client.read_registers(address, 1, function_code=function_code)
                value = registers[0] if registers else None
                if value is not None and tag_type == "int":
                    value = int(value)
                if value is not None and tag_type == "bool":
                    value = bool(value)
            
            # 缩放处理
            if value is not None and isinstance(value, (int, float)) and scale != 1.0:
                value = value * scale
            await self._after_read_value(tag_config, value, unit)
        
        except (IndexError, OSError, RuntimeError, TypeError, ValueError) as e:
            logger.error(
                "tag_read_error",
                tag=tag_name,
                address=address,
                error=str(e),
            )
