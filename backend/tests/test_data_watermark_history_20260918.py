"""D5：data_watermark 的 upsert 会销毁历史状态，改为覆盖前留档。

缺陷
----
`data_watermark` 是 `(dataset, trade_date)` 唯一 + upsert，只保留最新一版。
事故复盘问的是"当时那一刻闸门看到的是什么"，而那一刻的状态已被后续运行覆盖，
只能间接靠 `data_quality_run.summary_json` 的冻结快照还原。

修法
----
新增 append-only 的 `data_watermark_revision`：upsert 覆盖前把旧行整行留档。
不回溯、不补齐、不用当前数据回填过去 —— 只保留"当时已落库的状态"。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import prediction_data_quality as quality
from app.db.session import Base
from app.models.governance import DataQualityRun, DataWatermark, DataWatermarkRevision


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wm.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _watermark(dataset="stock_kline", day=date(2026, 9, 17), **overrides):
    item = {
        "dataset": dataset,
        "trade_date": day.isoformat(),
        "observed_at": "2026-09-17T13:05:00",
        "max_available_at": "2026-09-16T15:00:00",
        "record_count": 3000,
        "expected_count": 3032,
        "completeness": 0.99,
        "status": "ok",
        "details": {"coverage_scope": "all_rows"},
    }
    item.update(overrides)
    return item


def _run(day=date(2026, 9, 17)):
    return DataQualityRun(
        trade_date=day,
        run_type="prediction_gate",
        status="completed",
        started_at=datetime(2026, 9, 17, 13, 5),
        completed_at=datetime(2026, 9, 17, 13, 5, 1),
        summary_json="{}",
    )


async def _persist(maker, items):
    async with maker() as db:
        await quality.PredictionDataQualityAuditor()._persist(
            db, run=_run(), findings=[], watermarks=items,
        )


@pytest.mark.asyncio
async def test_first_write_creates_no_revision(maker):
    """第一次写入没有"被覆盖的旧状态"，不应凭空造一条历史。"""
    await _persist(maker, [_watermark()])
    async with maker() as db:
        assert await db.scalar(select(DataWatermarkRevision.id)) is None
        assert await db.scalar(select(DataWatermark.completeness)) == pytest.approx(0.99)


@pytest.mark.asyncio
async def test_overwrite_keeps_the_state_the_gate_actually_saw(maker):
    await _persist(maker, [_watermark()])
    # 第二次运行：数据缺失，闸门转为 blocked
    await _persist(maker, [_watermark(
        observed_at="2026-09-17T14:00:00", record_count=0, completeness=0.0,
        status="blocked", details={"coverage_scope": "all_rows", "reason": "source_down"},
    )])
    async with maker() as db:
        rows = (await db.execute(select(DataWatermarkRevision))).scalars().all()
        assert len(rows) == 1
        kept = rows[0]
        # 13:05 那一刻的真实状态
        assert kept.status == "ok"
        assert kept.completeness == pytest.approx(0.99)
        assert kept.record_count == 3000
        assert kept.observed_at == datetime(2026, 9, 17, 13, 5)
        # replaced_at 是覆盖发生的墙钟时刻，不是被覆盖版本的观测时刻
        assert kept.replaced_at >= datetime(2026, 9, 17, 14, 0)
        assert abs(kept.replaced_at - datetime.now()) < timedelta(seconds=60)
        assert kept.replacement_kind == "superseded"
        # 最新状态仍然是 upsert 语义
        live = (await db.execute(select(DataWatermark))).scalars().one()
        assert live.status == "blocked" and live.completeness == 0.0


@pytest.mark.asyncio
async def test_history_is_per_dataset_and_trade_date(maker):
    await _persist(maker, [
        _watermark("stock_kline"), _watermark("fund_flow"), _watermark("limit_up_pool"),
    ])
    await _persist(maker, [
        _watermark("stock_kline", status="degraded", completeness=0.5),
        _watermark("fund_flow", status="degraded", completeness=0.5),
        _watermark("limit_up_pool", status="degraded", completeness=0.5),
        _watermark("auction_data", status="missing", completeness=0.0),
    ])
    async with maker() as db:
        rows = (await db.execute(select(DataWatermarkRevision))).scalars().all()
        assert {(r.dataset, r.status) for r in rows} == {
            ("stock_kline", "ok"), ("fund_flow", "ok"), ("limit_up_pool", "ok"),
        }, "第一次出现的 auction_data 不应产生历史行"
        # 不同交易日互不干扰
    await _persist(maker, [_watermark(day=date(2026, 9, 18))])
    async with maker() as db:
        assert len((await db.execute(select(DataWatermarkRevision))).scalars().all()) == 3


@pytest.mark.asyncio
async def test_revision_uses_owned_leaf_fields_not_live_orm_state(maker):
    """留档必须复制叶子字段，不能与 ORM 行共享可变对象。"""
    await _persist(maker, [_watermark(details={"coverage_scope": "all_rows", "n": 1})])
    await _persist(maker, [_watermark(
        observed_at="2026-09-17T14:00:00",
        details={"coverage_scope": "all_rows", "mutated": True},
    )])
    async with maker() as db:
        kept = (await db.execute(select(DataWatermarkRevision))).scalars().one()
        assert "mutated" not in kept.details_json
        assert '"n":1' in kept.details_json.replace(" ", "")


def _load_migration():
    """按仓库既有做法直接从迁移文件加载 upgrade()，验证真实 DDL 与保护触发器。"""
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" \
        / "034_data_watermark_revisions.py"
    spec = importlib.util.spec_from_file_location("wm_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration, MigrationContext, Operations


def test_migration_creates_table_seeds_current_state_and_is_append_only(tmp_path):
    import sqlalchemy as sa

    migration, MigrationContext, Operations = _load_migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig.sqlite'}")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(
                connection,
                tables=[DataWatermark.__table__, DataQualityRun.__table__],
            )
            connection.execute(DataWatermark.__table__.insert().values(
                dataset="auction_data", trade_date=date(2026, 9, 17),
                observed_at=datetime(2026, 9, 17, 15, 10, 2),
                record_count=0, expected_count=2992, completeness=0.0,
                status="missing", details_json='{"path_degraded":true}',
            ))
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            migration.upgrade()  # 幂等

            # 只播种"现在已知的最新状态"，且明确标为 initial_seed
            seeded = connection.execute(sa.text(
                "SELECT dataset, status, completeness, replacement_kind, "
                "strftime('%Y-%m-%d %H:%M:%S', replaced_at) "
                "FROM data_watermark_revision"
            )).fetchall()
            assert seeded == [
                ("auction_data", "missing", 0.0, "initial_seed", "2026-09-17 15:10:02")
            ]
            # append-only 触发器必须真的拦得住
            for statement in (
                "UPDATE data_watermark_revision SET status='ok'",
                "DELETE FROM data_watermark_revision",
            ):
                with pytest.raises(sa.exc.IntegrityError) as excinfo:
                    connection.execute(sa.text(statement))
                assert "append-only" in str(excinfo.value)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_writer_history_is_append_only_via_migration(maker):
    """迁移装好触发器后，写入路径产生的历史同样不可改不可删。"""
    import sqlalchemy as sa

    await _persist(maker, [_watermark()])
    await _persist(maker, [_watermark(observed_at="2026-09-17T14:00:00", status="degraded")])
    migration, MigrationContext, Operations = _load_migration()
    async with maker() as db:
        await db.execute(sa.text("DROP TABLE data_watermark_revision"))
        await db.execute(sa.text(
            "CREATE TABLE data_watermark_revision (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "dataset VARCHAR(40) NOT NULL, trade_date DATE NOT NULL, observed_at DATETIME NOT NULL, "
            "max_available_at DATETIME, record_count INTEGER NOT NULL DEFAULT 0, "
            "expected_count INTEGER, completeness FLOAT NOT NULL DEFAULT 0, "
            "status VARCHAR(20) NOT NULL DEFAULT 'missing', details_json TEXT NOT NULL DEFAULT '{}', "
            "replaced_at DATETIME NOT NULL, replacement_kind VARCHAR(20) NOT NULL DEFAULT 'superseded')"
        ))
        await db.commit()

        def install(connection):
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

        # run_sync 传的是同步 Session，迁移需要真正的 Connection
        await (await db.connection()).run_sync(install)
        await db.commit()
        for statement in (
            "UPDATE data_watermark_revision SET status='ok'",
            "DELETE FROM data_watermark_revision",
        ):
            with pytest.raises(Exception) as excinfo:
                await db.execute(sa.text(statement))
            await db.rollback()
            assert "append-only" in str(excinfo.value)


def test_helper_does_not_reference_live_watermark_row():
    """回归护栏：留档 helper 只读叶子字段，不把 ORM 行存进历史。"""
    import inspect

    source = inspect.getsource(quality._watermark_revision)
    assert "DataWatermarkRevision(" in source
    for field in ("dataset", "trade_date", "observed_at", "status", "details_json"):
        assert field in source


def test_migration_is_registered_as_an_append_only_revision():
    from scripts.deployment_evidence_contract import REVISIONS, expected_guards

    assert REVISIONS[-1] == "034_data_watermark_revisions"
    guards = expected_guards("034_data_watermark_revisions")
    assert guards["data_watermark_revision_no_update"]["sql"] == (
        "CREATE TRIGGER data_watermark_revision_no_update BEFORE UPDATE "
        "ON data_watermark_revision BEGIN SELECT RAISE(ABORT, "
        "'watermark history is append-only'); END"
    )
    assert "data_watermark_revision_no_delete" in guards


def test_revision_table_schema_is_pinned():
    """水位历史表的列/索引一旦漂移，历史可追溯性就没了，必须显式钉住。"""
    table = DataWatermarkRevision.__table__
    assert {c.name for c in table.columns} == {
        "id", "dataset", "trade_date", "observed_at", "max_available_at",
        "record_count", "expected_count", "completeness", "status",
        "details_json", "replaced_at", "replacement_kind",
    }
    assert {i.name for i in table.indexes} == {"ix_data_watermark_revision_lookup"}
    # 只允许 id 主键唯一：同一 (dataset, trade_date) 必须能有多版本。
    assert [tuple(c.name for c in u.columns) for u in table.constraints
            if u.__class__.__name__ == "UniqueConstraint"] == []


def test_orm_install_creates_the_append_only_triggers():
    """`init_db()` 走 create_all，不跑迁移；保护触发器必须挂在 ORM 上。

    否则开发库/测试库只有表没有保护（发布契约测试会报
    "required append-only triggers missing"）。
    """
    import sqlite3
    import tempfile
    from pathlib import Path

    import sqlalchemy as sa

    from app.db.session import Base

    path = Path(tempfile.mkdtemp()) / "orm.sqlite"
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(
                connection,
                tables=[DataQualityRun.__table__, DataWatermark.__table__,
                        DataWatermarkRevision.__table__],
            )
    finally:
        engine.dispose()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )}
    finally:
        conn.close()
    assert names == {
        "data_watermark_revision_no_update", "data_watermark_revision_no_delete",
    }, names
