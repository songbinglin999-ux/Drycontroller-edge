"""
系统监控服务
"""

import asyncio
import psutil
import structlog

logger = structlog.get_logger(__name__)

async def main():
    logger.info("edge_monitor_started")
    
    while True:
        # 收集系统指标
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        logger.info(
            "system_metrics",
            cpu_percent=cpu_percent,
            memory_percent=memory.percent,
            disk_percent=disk.percent,
        )
        
        await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())

