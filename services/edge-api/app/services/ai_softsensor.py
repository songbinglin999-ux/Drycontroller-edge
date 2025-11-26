"""
AI 软测量领域服务

本模块封装了 AI 软测量相关的业务逻辑，为 API 层提供：
- 实时含水率预测值获取（从 MQTT 实时流）
- 历史含水率预测趋势查询（从 InfluxDB）
- 训练样本数据导出（用于模型迭代）
- AI 模型配置信息查询

服务职责：
- 聚合多个数据源（ws_stream、InfluxDB、配置文件）
- 提供统一的接口给 API 路由层
- 封装数据格式化和错误处理
"""

from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from app.core.config import settings
from app.services.realtime_data import (
    RealtimeDataService,
    get_realtime_data_service,
)
from app.services.influx import InfluxQueryService, get_influx_service
from app.services.ws_stream import ws_stream


class AISoftsensorService:
    """
    AI 软测量领域服务
    
    聚合实时数据、历史数据和配置信息，为 API 层提供统一接口。
    
    Attributes:
        PREDICTED_TAG: 预测含水率的标签名称
        _realtime: 实时数据服务
        _influx: InfluxDB 查询服务
        _config_path: AI 配置文件路径
        _training_measurement: 训练数据的 InfluxDB 测量表名
    """

    PREDICTED_TAG = "MOISTURE.PREDICTED"

    def __init__(
        self,
        realtime_service: Optional[RealtimeDataService] = None,
        influx_service: Optional[InfluxQueryService] = None,
        config_path: Optional[Path] = None,
    ) -> None:
        """
        初始化服务
        
        Args:
            realtime_service: 实时数据服务（可注入用于测试）
            influx_service: InfluxDB 服务（可注入用于测试）
            config_path: AI 配置文件路径
        """
        self._realtime = realtime_service or get_realtime_data_service()
        self._influx = influx_service or get_influx_service()
        self._config_path = Path(config_path or settings.AI_CONFIG)
        self._training_measurement = settings.TRAINING_MEASUREMENT

    def get_current_moisture(self, line: Optional[str] = None) -> Optional[dict]:
        """
        获取当前含水率预测值
        
        从 MQTT 实时流（通过 ws_stream）获取最新的 AI 推理结果。
        
        Args:
            line: 可选的产线 ID 过滤
            
        Returns:
            包含预测数据的响应字典，无数据时返回 None
        """
        data = ws_stream.get_latest("ai", line)
        if not data:
            return None
        return {
            "success": True,
            "data": data,
            "timestamp": int(time.time()),
        }

    async def get_moisture_history(
        self,
        period: str,
        window: str,
        aggregation: str,
    ) -> dict:
        """
        查询含水率预测历史趋势
        
        从 InfluxDB 查询指定时间范围内的含水率预测数据。
        
        Args:
            period: 查询时间范围（如 1d、6h）
            window: 聚合窗口大小（如 5m、1h）
            aggregation: 聚合函数（mean/max/min 等）
            
        Returns:
            包含历史数据的响应字典
        """
        return await self._realtime.get_history_payload(
            tag=self.PREDICTED_TAG,
            period=period,
            window=window,
            aggregation=aggregation,
        )

    def get_models_info(self) -> dict:
        """
        获取 AI 模型配置信息
        
        读取 ai-config.yaml 文件，返回软测量和优化器模型的配置。
        
        Returns:
            包含模型配置的响应字典
        """
        config = self._load_ai_config()
        softsensor_info = self._extract_model_block(config.get("softsensor", {}))
        optimizer_info = self._extract_model_block(config.get("optimizer", {}))
        return {
            "success": True,
            "data": {
                "softsensor": softsensor_info,
                "optimizer": optimizer_info,
            },
            "timestamp": int(time.time()),
        }

    async def get_lab_samples(
        self,
        start: str = "-7d",
        stop: str = "now()",
        line: Optional[str] = None,
        require_lab: bool = True,
        limit: int = 200,
    ) -> dict:
        """
        查询训练样本数据
        
        用于导出模型训练所需的历史数据，支持按产线和时间范围过滤。
        
        Args:
            start: 起始时间（Flux 格式）
            stop: 结束时间
            line: 可选的产线过滤
            require_lab: 是否要求包含实验室化验值
            limit: 返回记录数量限制
            
        Returns:
            包含样本数据的响应字典
        """
        records = await self._influx.training_samples(
            measurement=self._training_measurement,
            start=start,
            stop=stop,
            line=line,
            require_lab=require_lab,
        )
        if limit:
            records = records[:limit]
        return {
            "success": True,
            "data": records,
            "count": len(records),
            "timestamp": int(time.time()),
        }

    def _load_ai_config(self) -> Dict[str, Any]:
        """加载 AI 配置文件"""
        if not self._config_path.exists():
            return {}
        try:
            with self._config_path.open("r", encoding="utf-8") as fp:
                return yaml.safe_load(fp) or {}
        except Exception:
            return {}

    @staticmethod
    def _extract_model_block(block: Dict[str, Any]) -> Dict[str, Any]:
        """提取模型配置块的关键字段"""
        return {
            "model_name": block.get("model_name"),
            "model_version": block.get("model_version"),
            "model_path": block.get("model_path"),
            "input_features": block.get("input_features"),
            "output": block.get("output"),
            "publish": block.get("publish"),
        }


@lru_cache(maxsize=1)
def get_ai_softsensor_service() -> AISoftsensorService:
    """获取 AI 软测量服务单例"""
    return AISoftsensorService()

