"""验证脚本的样本构造回归测试（2026-09-17 行动项⑤）。

锁定两处踩过的坑：
  1. 样本必须按"买入周期"切分 —— 不能拿卖出流水条数当样本数
     （T 减仓使一笔持仓产生多条卖出记录）
  2. 运营性强制退出必须显式剔除 —— 识别用显式登记的模式，不用子串猜测
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import validate_strategy_edge as vse  # noqa: E402


def _db(tmp_path, rows):
    path = tmp_path / "t.db"
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE paper_trade_log (
        account_id INTEGER, code TEXT, trade_type TEXT, amount INTEGER,
        price REAL, trade_time TEXT, realized_pnl REAL, reason TEXT)""")
    con.executemany("INSERT INTO paper_trade_log VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def test_partial_sells_are_one_position_not_many(tmp_path):
    """600855 型：一次买入 + 两次部分卖出 = 1 个持仓，收益率按总盈亏/总成本。"""
    db = _db(tmp_path, [
        (2, "600855", "buy", 500, 13.15, "2026-09-16 09:48:15", None, ""),
        (2, "600855", "sell", 100, 13.12, "2026-09-17 09:45:41", -10.31, "触发持仓止损价"),
        (2, "600855", "sell", 400, 13.13, "2026-09-17 09:46:06", -22.25, "触发持仓止损价"),
    ])
    positions, _ = vse.load_positions(db)
    assert len(positions) == 1, "两次部分卖出必须合并为 1 个持仓"
    p = positions[0]
    assert p["cost"] == pytest.approx(500 * 13.15)
    # 正确口径：总盈亏 / 总成本；错误口径会得到两条被缩小的记录
    assert p["pct"] == pytest.approx((-10.31 - 22.25) / (500 * 13.15) * 100, rel=1e-6)


def test_two_buy_cycles_are_two_positions(tmp_path):
    """同一 (账户,股票) 的两轮买卖 = 2 个持仓。"""
    db = _db(tmp_path, [
        (2, "000001", "buy", 100, 10.0, "2026-09-01 09:30:00", None, ""),
        (2, "000001", "sell", 100, 11.0, "2026-09-02 09:30:00", 100.0, "触发短线止盈：10%"),
        (2, "000001", "buy", 100, 12.0, "2026-09-03 09:30:00", None, ""),
        (2, "000001", "sell", 100, 10.0, "2026-09-04 09:30:00", -200.0, "触发持仓止损价"),
    ])
    positions, _ = vse.load_positions(db)
    assert len(positions) == 2
    assert sorted(round(p["pct"], 2) for p in positions) == [-16.67, 10.0]


def test_operational_exits_are_excluded_explicitly(tmp_path):
    """运营性强制退出（版本隔离 / 系统清理）必须被剔除。

    特别注意 "旧版或未标版本仓位隔离退出"：'版本' 与 '隔离' 被 '仓位' 隔开，
    因此不能用子串 '版本隔离' 识别 —— 必须登记完整模式。
    """
    db = _db(tmp_path, [
        (8, "600272", "buy", 100, 10.0, "2026-09-01 09:30:00", None, ""),
        (8, "600272", "sell", 100, 8.4, "2026-09-03 09:30:00", -160.0,
         "旧版或未标版本仓位隔离退出：持仓版本=legacy_unversioned，当前版本=abcdef_shape_v3"),
        (2, "600001", "buy", 100, 10.0, "2026-09-01 09:30:00", None, ""),
        (2, "600001", "sell", 100, 10.5, "2026-09-02 09:30:00", 50.0, "触发短线止盈：5%"),
        (2, "600002", "buy", 100, 10.0, "2026-09-01 09:30:00", None, ""),
        (2, "600002", "sell", 100, 10.0, "2026-09-05 09:30:00", 0.0,
         "[系统清理] 持仓超过4个月从未刷新, 强制按最新价平仓释放资金"),
    ])
    positions, _ = vse.load_positions(db)
    assert len(positions) == 3
    flagged = [p for p in positions if p["operational"]]
    assert len(flagged) == 2, "版本隔离与系统清理两类都必须被识别"
    assert {p["code"] for p in flagged} == {"600272", "600002"}
    clean = [p for p in positions if not p["operational"]]
    assert [p["code"] for p in clean] == ["600001"]


def test_open_position_is_not_a_sample(tmp_path):
    """只有买入、没有卖出的持仓不计入已完成样本。"""
    db = _db(tmp_path, [
        (2, "000001", "buy", 100, 10.0, "2026-09-01 09:30:00", None, ""),
    ])
    positions, _ = vse.load_positions(db)
    assert positions == []


def test_known_rungs_are_not_flagged_as_unregistered(tmp_path):
    """合法策略 rung（含'昨日涨停次日转弱'）不得触发未登记告警。"""
    db = _db(tmp_path, [
        (2, "600001", "buy", 100, 10.0, "2026-09-01 09:30:00", None, ""),
        (2, "600001", "sell", 100, 10.2, "2026-09-02 09:30:00", 20.0,
         "昨日涨停次日转弱：跌幅-5.19%，延续性失败全退"),
    ])
    _, unregistered = vse.load_positions(db)
    assert unregistered == []


def test_unknown_reason_raises_a_warning(tmp_path):
    """未登记的退出原因必须冒泡为告警，避免运营事件被静默算进样本。"""
    db = _db(tmp_path, [
        (2, "600001", "buy", 100, 10.0, "2026-09-01 09:30:00", None, ""),
        (2, "600001", "sell", 100, 10.0, "2026-09-02 09:30:00", 0.0, "某个没登记过的新原因"),
    ])
    _, unregistered = vse.load_positions(db)
    assert unregistered and "某个没登记过的新原因" in unregistered[0]


def test_required_n_matches_documented_values():
    """n_min 公式与文档数值一致（σ=6.85%：δ=3% ≈ 53 笔、δ=2% ≈ 120 笔）。"""
    assert round(vse.required_n(6.85, 3.0)) == 53
    assert round(vse.required_n(6.85, 2.0)) == 120


def test_verdict_thresholds():
    """判定门槛：样本不足 / 晋级 / 淘汰 / 不显著。"""
    insufficient = vse.describe([1.0, -1.0, 2.0])
    assert vse.verdict(insufficient, 2.0)[0] == "样本不足"

    # 大样本正边际 -> 晋级
    strong = vse.describe([3.0] * 40 + [2.0] * 40 + [-2.0] * 40)
    assert vse.verdict(strong, 2.0)[0] == "晋级门通过"

    # 大样本负边际 -> 淘汰
    weak = vse.describe([-3.0] * 40 + [-2.0] * 40 + [1.0] * 40)
    assert vse.verdict(weak, 2.0)[0] == "淘汰门拒绝"
