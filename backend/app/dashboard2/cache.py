"""Dashboard 2.0 快照缓存"""

import json
import math
from datetime import datetime
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.governance import DashboardSnapshot
from .schemas import Dashboard2Snapshot


def _sanitize_json_values(value):
    """将 NaN/Inf 等非法 JSON 数值清洗为 0，避免接口序列化 500。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, dict):
        return {k: _sanitize_json_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_values(v) for v in value]
    return value


class DashboardSnapshotCache:
    SNAPSHOT_KEY = "overview-v2"

    async def save(self, session: AsyncSession, snapshot: Dashboard2Snapshot, status: str = "ok"):
        payload = json.dumps(
            _sanitize_json_values(snapshot.model_dump()),
            ensure_ascii=False,
            allow_nan=False,
        )
        # overview-v2 是“最新值缓存”而非审计流水。原实现每分钟插入一行，
        # 会让查询与 SQLite 持续膨胀；从现在起覆盖同键最新行。
        row = (
            await session.execute(
                select(DashboardSnapshot)
                .where(
                    DashboardSnapshot.snapshot_key == self.SNAPSHOT_KEY,
                    DashboardSnapshot.trade_date.is_(None),
                    DashboardSnapshot.status == status,
                )
                .order_by(desc(DashboardSnapshot.snapshot_time))
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            row = DashboardSnapshot(
                snapshot_key=self.SNAPSHOT_KEY,
                trade_date=None,
                status=status,
            )
            session.add(row)
        row.snapshot_time = datetime.now()
        row.payload_json = payload
        await session.flush()

    async def latest(self, session: AsyncSession) -> Optional[dict]:
        result = await session.execute(
            select(DashboardSnapshot)
            .where(
                DashboardSnapshot.snapshot_key == self.SNAPSHOT_KEY,
                DashboardSnapshot.trade_date.is_(None),
                DashboardSnapshot.status == "ok",
            )
            .order_by(desc(DashboardSnapshot.snapshot_time))
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        try:
            return _sanitize_json_values(json.loads(row.payload_json))
        except Exception:
            return None


dashboard_snapshot_cache = DashboardSnapshotCache()
