"""数据回填管理 — P0核心"""

from datetime import date, datetime
from typing import Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.governance import BackfillTask


class DataBackfillManager:
    """数据回填管理器"""

    async def create_task(self, session: AsyncSession,
                          data_type: str, start_date: date, end_date: date) -> BackfillTask:
        """创建回填任务"""
        task = BackfillTask(
            data_type=data_type,
            start_date=start_date,
            end_date=end_date,
            status="pending",
            created_at=datetime.now(),
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        logger.info(f"回填任务创建: {data_type} {start_date}→{end_date}, id={task.id}")
        return task

    async def start_task(self, session: AsyncSession, task_id: int):
        """开始执行回填任务"""
        task = await session.get(BackfillTask, task_id)
        if task and task.status == "pending":
            task.status = "running"
            await session.commit()

    async def update_progress(self, session: AsyncSession, task_id: int,
                              done: int, errors: int = 0):
        """更新进度"""
        task = await session.get(BackfillTask, task_id)
        if task:
            task.done_count = done
            task.error_count = errors
            if task.total_count and task.total_count > 0:
                task.progress = done / task.total_count
            await session.commit()

    async def complete_task(self, session: AsyncSession, task_id: int, success: bool = True):
        """完成任务"""
        task = await session.get(BackfillTask, task_id)
        if task:
            task.status = "done" if success else "failed"
            task.progress = 1.0 if success else task.progress
            task.finished_at = datetime.now()
            await session.commit()
            logger.info(f"回填任务完成: id={task_id}, 状态={'成功' if success else '失败'}")

    async def get_pending_tasks(self, session: AsyncSession) -> list[BackfillTask]:
        """获取待执行的任务"""
        result = await session.execute(
            select(BackfillTask).where(BackfillTask.status == "pending")
        )
        return list(result.scalars().all())

    async def get_running_tasks(self, session: AsyncSession) -> list[BackfillTask]:
        """获取正在运行的任务"""
        result = await session.execute(
            select(BackfillTask).where(BackfillTask.status == "running")
        )
        return list(result.scalars().all())


# 全局单例
backfill_manager = DataBackfillManager()
