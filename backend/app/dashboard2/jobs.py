"""Dashboard 2.0 后台快照任务"""

from loguru import logger

from app.db.session import async_session
from .service import dashboard2_service
from .cache import dashboard_snapshot_cache


async def refresh_dashboard2_snapshot():
    async with async_session() as session:
        try:
            snapshot = await dashboard2_service.build_snapshot(session)
            await dashboard_snapshot_cache.save(session, snapshot, status="ok")
            await session.commit()
            logger.debug("overview-v2 快照刷新完成")
        except Exception as e:
            logger.error(f"overview-v2 快照刷新失败: {e}")
            await session.rollback()
