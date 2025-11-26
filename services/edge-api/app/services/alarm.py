"""
报警查询领域服务（只读）

本模块提供系统报警信息的查询功能，包括：
- 活跃报警查询：获取当前未恢复的报警
- 历史报警查询：获取指定时间范围内的所有报警

数据来源：
- 从 InfluxDB 的 system_health 测量表读取
- 报警数据由 edge-monitor 服务写入

注意：本服务为只读服务，不负责报警的生成和确认
"""

from __future__ import annotations

import time
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

