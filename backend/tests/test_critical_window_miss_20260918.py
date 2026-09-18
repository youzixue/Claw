"""关键窗口任务被 missed 时必须**持久**留证，而不是只写一行 warning。

事故（2026-09-18）
-----------------
笔记本在**电池供电 + 合盖**下被 macOS 睡眠（09:23:01 睡 1069 秒），
`auction_evidence_0925`（09:25 双来源竞价证据的**唯一**采样窗）被判 missed
15 分 25 秒。当天 `intraday_fast` 共 missed 139 次，
`operational_health.job_alerts_today` 是 `current_process_only` 的内存列表、
只保留 100 条 —— **09:25 那条被冲掉了**。
后果：`auction_data` 当日为空，而"任务从未执行"与"执行了但写入 0 行"
在数据上**无法区分**，排查时会误判成"竞价证据修复失败"。

为什么不能靠 misfire_grace_time 补救
-----------------------------------
竞价证据契约要求 `observed_at` 落在 09:25:00–09:25:30 内；
迟到执行只会返回 `outside_evidence_window` 并写 0 行 —— 晚跑等于没跑。
所以正确的做法是：**让它可见**，而不是让它晚跑。
"""
from __future__ import annotations

from app.data.scheduler import CRITICAL_WINDOW_JOB_IDS, DataScheduler


def test_critical_windows_cover_the_irreversible_ones():
    for job_id in (
        "auction_evidence_0925",
        "auction_collect_0920",
        "auction_collect_0924",
        "auction_collect_0925",
        "promotion_prediction_0925",
        "promotion_prediction_1510",
        "promotion_prediction_2000",
        "close_snapshot_finalize",
    ):
        assert job_id in CRITICAL_WINDOW_JOB_IDS, job_id
        assert CRITICAL_WINDOW_JOB_IDS[job_id], "每个关键窗口都要写明「为什么不可补」"
    # 高频任务不属于关键窗口（它们的丢失可由下一轮补上）
    for noisy in ("intraday_fast", "tencent_spot", "auction_collect",
                  "dashboard2_snapshot", "sector_derive"):
        assert noisy not in CRITICAL_WINDOW_JOB_IDS, noisy


def test_missed_critical_window_is_kept_in_memory_snapshot():
    sched = object.__new__(DataScheduler)
    sched._critical_window_misses = []
    sched._record_critical_window_miss({
        "job_id": "auction_evidence_0925",
        "scheduled_at": "2026-09-18T09:25:25",
        "observed_at": "2026-09-18T09:40:50",
        "finish_lateness_sec": 925.587,
    })
    assert len(sched._critical_window_misses) == 1
    rec = sched._critical_window_misses[0]
    assert rec["job_id"] == "auction_evidence_0925"
    assert "09:25:00-09:25:30" in rec["window"]


def test_missed_noisy_job_is_not_recorded_as_critical_window():
    """非关键任务不得污染这份记录，否则关键窗口又会被冲掉。"""
    sched = object.__new__(DataScheduler)
    sched._critical_window_misses = []
    sched._record_critical_window_miss({"job_id": "intraday_fast"})
    assert sched._critical_window_misses == []


def test_in_memory_snapshot_is_bounded():
    sched = object.__new__(DataScheduler)
    sched._critical_window_misses = []
    for i in range(40):
        sched._record_critical_window_miss({
            "job_id": "auction_collect_0925", "scheduled_at": f"t{i}",
            "observed_at": "o", "finish_lateness_sec": i,
        })
    assert len(sched._critical_window_misses) == 20


def test_health_exposes_the_critical_window_misses():
    import inspect

    body = inspect.getsource(DataScheduler.get_pipeline_runtime_status)
    assert "critical_window_misses" in body
