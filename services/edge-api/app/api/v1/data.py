"""
数据查询 API（v1）
"""

from fastapi import APIRouter, Depends

from app.services.realtime_data import (
    RealtimeDataService,
    get_realtime_data_service,
)
from app.core.security import require_readonly_http_source

router = APIRouter(dependencies=[Depends(require_readonly_http_source)])


@router.get("/tags")
async def get_tags(service: RealtimeDataService = Depends(get_realtime_data_service)):
    """
    获取所有可用标签
    """
    return service.get_tags_payload()
