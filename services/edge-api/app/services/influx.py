"""
InfluxDB 查询服务

本模块封装了 InfluxDB 时序数据库的查询操作，提供以下功能：
- 实时数据查询：获取指定标签的最新数据点
- 历史数据查询：支持时间窗口聚合的历史趋势查询
- 训练数据查询：用于 AI 模型训练的样本数据导出
- 系统指标查询：服务健康状态和性能指标查询

使用 Flux 查询语言与 InfluxDB 2.x 交互。
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

from influxdb_client import InfluxDBClient

from app.core.config import settings

# 时间窗口格式校验正则：支持 s(秒)、m(分)、h(时)、d(天)、w(周)
DURATION_PATTERN = re.compile(r"^\d+(s|m|h|d|w)$")
# 支持的聚合函数集合
VALID_AGGREGATIONS = {"mean", "max", "min", "sum", "last", "first"}


class InfluxQueryService:
    """
    InfluxDB 查询服务类
    
    提供对 InfluxDB 时序数据库的异步查询封装，支持：
    - 实时数据点查询（latest_points）
    - 历史趋势查询（history_points）
    - 训练样本导出（training_samples）
    - 系统指标趋势（system_metric_trend）
    - 服务告警查询（active_service_alarms）
    """
    
    def __init__(self) -> None:
        """初始化 InfluxDB 客户端连接"""
        self.client = InfluxDBClient(
            url=settings.influxdb_url,
            token=settings.INFLUXDB_TOKEN,
            org=settings.INFLUXDB_ORG,
            timeout=60_000,  # 查询超时时间：60秒
        )
        self.query_api = self.client.query_api()
        self.bucket = settings.INFLUXDB_BUCKET
        # 默认测量表名，用于存储工业采集数据
        self.measurement = os.getenv("INFLUXDB_MEASUREMENT", "industrial_data")
        # 避免解释器退出阶段 Influx SDK __del__ 抛出噪音异常：显式关闭 client
        atexit.register(self.close)

    def close(self) -> None:
        """
        释放 InfluxDB 连接资源（幂等）。
        """
        client = getattr(self, "client", None)
        if client:
            try:
                client.close()
            finally:
                self.client = None
                self.query_api = None

    def __del__(self):
        # 兜底：异常不应在析构阶段冒泡
        try:
            self.close()
        except Exception:
            pass

    async def latest_points(self, tags: List[str], lookback: str = "5m") -> List[Dict[str, Any]]:
        """
        查询指定标签的最新数据点
        
        Args:
            tags: 要查询的标签名称列表
            lookback: 回溯时间窗口，默认 5 分钟（格式如 5m、1h、1d）
            
        Returns:
            包含每个标签最新值的字典列表，每个字典包含：
            - tag: 标签名
            - value: 数值
            - unit: 单位
            - time: ISO 格式时间戳
        """
        if not tags:
            return []
        lookback = self._validate_duration(lookback)
        flux = self._build_latest_flux(tags, lookback)
        return await asyncio.to_thread(self._query_and_parse_latest, flux)

    async def history_points(
        self,
        tag: str,
        period: str = "1h",
        window: str = "1m",
        aggregation: str = "mean",
    ) -> List[Dict[str, Any]]:
        """
        查询单个标签的历史趋势数据
        
        Args:
            tag: 标签名称
            period: 查询时间范围（如 1h、6h、1d）
            window: 聚合窗口大小（如 1m、5m、1h）
            aggregation: 聚合函数（mean/max/min/sum/last/first）
            
        Returns:
            时间序列数据点列表，每个点包含 time 和 value
        """
        period = self._validate_duration(period)
        window = self._validate_duration(window)
        aggregation = self._validate_aggregation(aggregation)
        flux = self._build_history_flux(tag, period, window, aggregation)
        return await asyncio.to_thread(self._query_and_parse_history, flux)

    async def query_flux(self, flux: str):
        """
        执行自定义 Flux 查询并返回原始表数据
        
        Args:
            flux: Flux 查询语句
            
        Returns:
            InfluxDB 查询结果表列表
        """
        return await asyncio.to_thread(self.query_api.query, flux)

    async def training_samples(
        self,
        measurement: str,
        start: str,
        stop: str = "now()",
        line: Optional[str] = None,
        require_lab: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        查询 AI 模型训练样本数据
        
        用于导出软测量模型训练所需的历史数据，支持按产线过滤
        以及是否要求包含实验室化验值。
        
        Args:
            measurement: 测量表名（如 softsensor_training_data）
            start: 起始时间（Flux 格式，如 -7d）
            stop: 结束时间（默认 now()）
            line: 可选的产线 ID 过滤
            require_lab: 是否要求样本包含实验室化验值
            
        Returns:
            训练样本字典列表
        """
        flux = self._build_training_flux(measurement, start, stop, line, require_lab)
        tables = await self.query_flux(flux)
        return self._tables_to_dicts(tables)

    async def system_metric_trend(
        self,
        measurement: str,
        field: str,
        period: str = "6h",
        window: str = "5m",
        line: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        查询系统指标趋势数据
        
        用于获取 CPU、内存、磁盘等系统指标的历史趋势。
        
        Args:
            measurement: 测量表名（如 system_metrics）
            field: 字段名（如 cpu_percent）
            period: 查询时间范围
            window: 聚合窗口
            line: 可选的产线过滤
            
        Returns:
            时间序列数据点列表
        """
        period = self._validate_duration(period)
        window = self._validate_duration(window)
        flux = self._build_system_metric_flux(measurement, field, period, window, line)
        tables = await self.query_flux(flux)
        return self._parse_timeseries_tables(tables)

    async def active_service_alarms(
        self,
        measurement: str,
        period: str = "10m",
        severity: Optional[str] = None,
        include_history: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        查询活跃的服务告警
        
        Args:
            measurement: 测量表名（如 system_health）
            period: 查询时间范围
            severity: 可选的严重级别过滤
            include_history: 是否包含历史告警
            
        Returns:
            告警记录列表
        """
        flux = self._build_service_alarm_flux(
            measurement=measurement,
            period=period,
            severity=severity,
            include_history=include_history,
        )
        tables = await self.query_flux(flux)
        return self._tables_to_dicts(tables)

    def _build_latest_flux(self, tags: List[str], lookback: str) -> str:
        """
        构建获取最新数据点的 Flux 查询
        
        查询逻辑：
        1. 在指定时间范围内筛选目标标签
        2. 透视表结构，将字段转为列
        3. 按时间倒序排序，取每个标签的唯一最新值
        """
        tag_list = json.dumps(tags)
        return f"""
tagList = {tag_list}
from(bucket: "{self.bucket}")
  |> range(start: -{lookback})
  |> filter(fn: (r) => r["_measurement"] == "{self.measurement}")
  |> filter(fn: (r) => contains(value: r["tag"], set: tagList))
  |> pivot(rowKey: ["_time", "tag"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "tag", "value", "unit"])
  |> sort(columns: ["_time"], desc: true)
  |> unique(column: "tag")
"""

    def _build_history_flux(self, tag: str, period: str, window: str, aggregation: str) -> str:
        """
        构建历史数据查询的 Flux 语句
        
        查询逻辑：
        1. 按时间范围和标签过滤数据
        2. 使用 aggregateWindow 进行时间窗口聚合
        3. 按时间正序排列返回
        """
        return f"""
from(bucket: "{self.bucket}")
  |> range(start: -{period})
  |> filter(fn: (r) => r["_measurement"] == "{self.measurement}")
  |> filter(fn: (r) => r["tag"] == "{tag}")
  |> filter(fn: (r) => r["_field"] == "value")
  |> aggregateWindow(every: {window}, fn: {aggregation}, createEmpty: false)
  |> keep(columns: ["_time", "_value", "tag"])
  |> sort(columns: ["_time"], desc: false)
"""

    def _query_and_parse_latest(self, flux: str) -> List[Dict[str, Any]]:
        """
        执行查询并解析最新数据点结果
        
        将 InfluxDB 返回的表格数据转换为标准化的字典列表格式
        """
        tables = self.query_api.query(flux)
        result: List[Dict[str, Any]] = []
        for table in tables:
            for record in table.records:
                values = record.values
                result.append(
                    {
                        "tag": values.get("tag"),
                        "value": values.get("value"),
                        "unit": values.get("unit", ""),
                        "time": values.get("_time").isoformat() if values.get("_time") else None,
                    }
                )
        return result

    def _query_and_parse_history(self, flux: str) -> List[Dict[str, Any]]:
        """执行历史查询并解析结果"""
        tables = self.query_api.query(flux)
        return self._parse_timeseries_tables(tables)

    def _parse_timeseries_tables(self, tables) -> List[Dict[str, Any]]:
        """
        解析时间序列表格数据
        
        将 InfluxDB 返回的多表结果展平为简单的时间-值列表
        """
        result: List[Dict[str, Any]] = []
        for table in tables:
            for record in table.records:
                values = record.values
                result.append(
                    {
                        "time": values.get("_time").isoformat()
                        if values.get("_time")
                        else None,
                        "value": values.get("_value"),
                    }
                )
        return result

    def _build_training_flux(
        self,
        measurement: str,
        start: str,
        stop: str,
        line: Optional[str],
        require_lab: bool,
    ) -> str:
        """
        构建训练数据查询的 Flux 语句
        
        查询逻辑：
        1. 按时间范围和测量表过滤
        2. 可选按产线 ID 过滤
        3. 透视数据结构
        4. 可选要求包含实验室化验值（moisture_lab 字段）
        """
        # 产线过滤条件
        line_filter = (
            f'  |> filter(fn: (r) => r["line_id"] == "{line}")' if line else ""
        )
        # 实验室数据过滤条件（用于确保样本有标签）
        lab_filter = (
            "  |> filter(fn: (r) => exists r.moisture_lab)" if require_lab else ""
        )
        return f"""
from(bucket: "{self.bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r["_measurement"] == "{measurement}")
{line_filter}
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
{lab_filter}
  |> sort(columns: ["_time"], desc: false)
"""

    def _build_system_metric_flux(
        self,
        measurement: str,
        field: str,
        period: str,
        window: str,
        line: Optional[str],
    ) -> str:
        """
        构建系统指标查询的 Flux 语句
        
        用于查询 edge-monitor 采集的 CPU、内存等系统指标趋势
        """
        line_filter = f'  |> filter(fn: (r) => r["line"] == "{line}")' if line else ""
        return f"""
from(bucket: "{self.bucket}")
  |> range(start: -{period})
  |> filter(fn: (r) => r["_measurement"] == "{measurement}")
  |> filter(fn: (r) => r["_field"] == "{field}")
{line_filter}
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_value", "line"])
  |> sort(columns: ["_time"], desc: false)
"""

    def _build_service_alarm_flux(
        self,
        measurement: str,
        period: str,
        severity: Optional[str],
        include_history: bool,
    ) -> str:
        """
        构建服务告警查询的 Flux 语句
        
        用于查询服务健康检查产生的告警记录
        """
        severity_filter = ""
        if severity:
            severity_filter = f'  |> filter(fn: (r) => r["status"] == "{severity}")'
        status_filter = '  |> filter(fn: (r) => r["_field"] == "status")'
        history_keep = (
            ""
            if not include_history
            else "  |> keep(columns: [\"_time\", \"_value\", \"name\", \"status\"])"
        )
        return f"""
from(bucket: "{self.bucket}")
  |> range(start: -{period})
  |> filter(fn: (r) => r["_measurement"] == "{measurement}")
{status_filter}
{severity_filter}
  |> pivot(rowKey: ["name"], columnKey: ["_field"], valueColumn: "_value")
{history_keep}
"""

    def _tables_to_dicts(self, tables) -> List[Dict[str, Any]]:
        """
        将 InfluxDB 查询结果表转换为字典列表
        
        过滤掉以下划线开头的内部字段（保留 _time），
        并将时间戳转换为 ISO 格式字符串
        """
        rows: List[Dict[str, Any]] = []
        for table in tables:
            for record in table.records:
                values = dict(record.values)
                # 过滤内部字段，但保留 _time
                row: Dict[str, Any] = {
                    key: value
                    for key, value in values.items()
                    if not key.startswith("_") or key == "_time"
                }
                # 转换时间格式
                if "_time" in values and values["_time"]:
                    row["time"] = values["_time"].isoformat()
                rows.append(row)
        return rows

    @staticmethod
    def _validate_duration(value: str) -> str:
        """
        校验时间窗口格式
        
        有效格式示例：5m（5分钟）、1h（1小时）、1d（1天）
        """
        if not DURATION_PATTERN.match(value):
            raise ValueError("Invalid duration format. Use e.g. 5m, 1h, 1d.")
        return value

    @staticmethod
    def _validate_aggregation(value: str) -> str:
        """
        校验聚合函数名称
        
        支持：mean（均值）、max（最大）、min（最小）、sum（求和）、
              last（最后一个）、first（第一个）
        """
        value_lower = value.lower()
        if value_lower not in VALID_AGGREGATIONS:
            raise ValueError(f"Unsupported aggregation '{value}'.")
        return value_lower


@lru_cache(maxsize=1)
def get_influx_service() -> InfluxQueryService:
    """获取 InfluxDB 查询服务单例"""
    return InfluxQueryService()

