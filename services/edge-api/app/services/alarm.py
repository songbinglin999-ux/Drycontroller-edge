"""
报警查询领域服务（只读，已弃用）

DEPRECATED: /api/v1/alarms/active 和 /history 已迁移到 alarm_events SQL 表查询，
本模块不再被 alarms.py 引用。保留仅为 test_services.py 单测，等后续确认无其他
调用方后可整体移除。新代码不要再使用 AlarmService / get_alarm_service()。

历史数据来源：InfluxDB system_health measurement（由 edge-monitor 写入）。
"""

from __future__ import annotations

import time
import warnings
from functools import lru_cache
from typing import List, Optional

from app.core.config import settings
from app.services.influx import InfluxQueryService, get_influx_service


class AlarmService:
    """
    报警查询服务
    
    提供活跃报警和历史报警的查询接口。
    
    Attributes:
        _influx: InfluxDB 查询服务
        _measurement: 报警数据的测量表名
    """
    
    def __init__(self, influx_service: Optional[InfluxQueryService] = None) -> None:
        """
        初始化报警服务

        Args:
            influx_service: InfluxDB 服务（可注入用于测试）
        """
        warnings.warn(
            "AlarmService is deprecated; alarm read paths now query alarm_events table directly.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._influx = influx_service or get_influx_service()
        self._measurement = settings.SYSTEM_HEALTH_MEASUREMENT

    async def get_active_alarms(
        self,
        period: str = "10m",
        severity: Optional[str] = None,
    ) -> dict:
        """
        查询活跃报警
        
        获取指定时间范围内的最新报警状态。
        
        Args:
            period: 查询时间范围（默认 10 分钟）
            severity: 可选的严重级别过滤（如 warning、critical）
            
        Returns:
            包含报警列表的响应字典
        """
        alarms = await self._influx.active_service_alarms(
            measurement=self._measurement,
            period=period,
            severity=severity,
        )
        return {
            "success": True,
            "data": alarms,
            "count": len(alarms),
            "timestamp": int(time.time()),
        }

    async def get_alarm_history(
        self,
        period: str = "24h",
        severity: Optional[str] = None,
    ) -> dict:
        """
        查询历史报警
        
        获取指定时间范围内的所有报警记录（包括已恢复的）。
        
        Args:
            period: 查询时间范围（默认 24 小时）
            severity: 可选的严重级别过滤
            
        Returns:
            包含报警历史的响应字典
        """
        alarms = await self._influx.active_service_alarms(
            measurement=self._measurement,
            period=period,
            severity=severity,
            include_history=True,
        )
        return {
            "success": True,
            "data": alarms,
            "count": len(alarms),
            "timestamp": int(time.time()),
        }


@lru_cache(maxsize=1)
def get_alarm_service() -> AlarmService:
    """获取报警服务单例"""
    return AlarmService()

