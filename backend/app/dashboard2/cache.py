"""Dashboard 2.0 快照缓存"""

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.governance import DashboardSnapshot
from .schemas import Dashboard2Snapshot


class DashboardSnapshotCache:
    SNAPSHOT_KEY = "overview-v2"

    async def save(self, session: AsyncSession, snapshot: Dashboard2Snapshot, status: str = "ok"):
        payload = json.dumps(snapshot.model_dump(), ensure_ascii=False)
        row = DashboardSnapshot(
            snapshot_key=self.SNAPSHOT_KEY,
            trade_date=None,
            snapshot_time=datetime.now(),
            payload_json=payload,
            status=status,
        )
        session.add(row)
        await session.flush()

    async def latest(self, session: AsyncSession) -> Optional[dict]:
        result = await session.execute(
            select(DashboardSnapshot)
            .where(DashboardSnapshot.snapshot_key == self.SNAPSHOT_KEY)
            .order_by(desc(DashboardSnapshot.snapshot_time))
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        return json.loads(row.payload_json)


dashboard_snapshot_cache = DashboardSnapshotCache()
