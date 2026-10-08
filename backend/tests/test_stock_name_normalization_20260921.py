"""名称规范化与观察标的风险文案 — 2026-09-21 复审

覆盖：
1. 股票名称在 tag.name / 传入 name / 行情 quote_name 三处的全角空白与全角字母归一化，
   使万科A 与 行情"万  科Ａ"归一为同一字符串，避免 name_conflict 误报；
2. 真实冲突（不同汉字名称）必须继续触发 name_conflict；
3. 风险名 ST / *ST / 退 / 退市 / 证券类别 A 与 B 必须**保留为风险**，不得被归一化抹去；
4. 观察标的风控文案须**随 board_type 区分**：创业板/科创板/北交所使用对应板块文案，
   主板不应被误称为创业板/科创板/北交所；unknown 板块保守文案；
5. 缺失标签 / 未知代码 / 错代码 / 板块冲突 / 停牌 / 黑名单 仍走 fail-closed；
6. 真实冲突（即便是主板）必须仍被风控链路 block。

使用 run_offline.py 跑：会创建独立临时 DB、阻断 socket、禁调度推送。
"""
from datetime import date, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.stock_tagger import (
    stock_tagger,
    TAG_OBSERVE_ONLY,
    TAG_BLOCKED,
    TAG_TRADEABLE,
)
from app.db.session import Base
from app.models.stock import StockTag, StockSpot, StockBlacklist
from app.risk.engine import RiskContext, RiskEngine, RiskLevel
from app.risk.rules import BlacklistRule, ObserveOnlyRule


def _tag_row(code, name="", board_type=None, board_tag=None, **overrides):
    fields = dict(
        code=code,
        name=name,
        board_type=board_type or stock_tagger.get_board_type(code),
        board_tag=board_tag if board_tag is not None else stock_tagger.get_board_tag(
            stock_tagger.get_board_type(code)
        ),
        is_st=False,
        is_suspended=False,
        is_delisting=False,
        is_ipo_recent=False,
        is_limit_up=False,
        is_limit_down=False,
    )
    fields.update(overrides)
    return StockTag(**fields)


@pytest_asyncio.fixture
async def db_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'name_norm.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


# ---------------- 1. clean_name 归一化覆盖 ----------------


@pytest.mark.parametrize("raw,expected", [
    # 半角空白折叠
    ("万  科Ａ", "万科A"),       # 全角Ａ + 多余空格
    ("万 科 A", "万科A"),       # 半角空格 + 半角A
    ("　万　科　A　", "万科A"),   # 全角空格 + 末尾空格
    ("\u3000万科Ａ\u3000", "万科A"),
    # 全角数字
    ("平安银行１２３", "平安银行123"),
    # ST/退市类别保留原始风险字段；归一化只清空白与全角，不抹 ST/*ST/退
    ("ＳＴ测试", "ST测试"),
    ("＊ＳＴ测试", "*ST测试"),
    ("退测试", "退测试"),
    ("测试退", "测试退"),
    ("测试退市", "测试退市"),
    # 证券类别 A/B 区分保留
    ("三一A", "三一A"),
    ("三一Ｂ", "三一B"),
    # 已无效输入仍返回空串
    ("", ""), ("nan", ""), ("  ", ""), ("\u3000", ""),
])
def test_clean_name_normalizes_fullwidth_whitespace_and_letters(raw, expected):
    """clean_name 只折叠空白 + 全角字母/数字归一化，不抹去风险/类别信息。"""
    assert stock_tagger.clean_name(raw) == expected


def test_name_risks_preserved_after_normalization():
    """归一化不得抹去 ST / 退市 / 类别 A 等风险字符。"""
    # 行情以全角形式提供 ST 仍必须被识别为 ST 风险
    is_st, is_delisting = stock_tagger.name_risks("ＳＴ万  科Ａ")
    assert is_st is True and is_delisting is False
    is_st, is_delisting = stock_tagger.name_risks("＊ＳＴ测试")
    assert is_st is True and is_delisting is False
    # 退市：前缀/后缀/中间词
    assert stock_tagger.name_risks("退测试") == (False, True)
    assert stock_tagger.name_risks("测试退") == (False, True)
    assert stock_tagger.name_risks("测试退市") == (False, True)
    # 证券 A 与 B：name_risks 当前只关心 ST/退，归一化只确保不被解析错误。
    # 即归一化后等于行为不应误把"三一A"识别为"三一"等
    assert stock_tagger.name_risks("三一Ａ") == (False, False)
    assert stock_tagger.name_risks("三一Ｂ") == (False, False)


# ---------------- 2. 名称归一化使三字段名称一致，不再误报冲突 ----------------


def test_three_field_names_normalized_same_avoids_name_conflict():
    """tag.name=万科A，传入 name=万科A，行情 quote_name='万  科Ａ'
    归一化后必须相等，不得触发 name_conflict。
    """
    tag = _tag_row("000002", name="万科A")
    status = stock_tagger.resolve_status(
        "000002", tag, name="万科A", quote_name="万  科Ａ",
    )
    assert "name_conflict" not in status["identity_issues"]
    assert status["is_tradeable"] is True
    assert status["board_tag"] == TAG_TRADEABLE
    assert status["execution_authorized"] is False  # 投影从未授权


def test_real_chinese_name_conflict_still_flagged():
    """真实冲突：tag.name='万科A'，传入 name='万科B'（不同汉字）
    归一化后仍不等，必须继续触发 name_conflict。
    """
    tag = _tag_row("000002", name="万科A")
    status = stock_tagger.resolve_status(
        "000002", tag, name="万科B", quote_name="万科A",
    )
    assert "name_conflict" in status["identity_issues"]
    assert status["is_tradeable"] is False
    # 但即便仅传入 vs 标签冲突，也需触发
    status_only_supplied = stock_tagger.resolve_status(
        "000002", tag, name="万科B",
    )
    assert "name_conflict" in status_only_supplied["identity_issues"]
    assert status_only_supplied["is_tradeable"] is False
    # 仅行情名称冲突
    status_only_quote = stock_tagger.resolve_status(
        "000002", tag, quote_name="万  科B",
    )
    assert "name_conflict" in status_only_quote["identity_issues"]


def test_partial_whitespace_difference_in_chinese_names_still_conflict():
    """真实冲突语义保留：'万科A' 与 '万 科B'（仅空白差异、汉字 A vs B）
    必须识别为冲突，不能被全角化掩盖。
    """
    tag = _tag_row("000002", name="万科A")
    status = stock_tagger.resolve_status(
        "000002", tag, name="万 科B", quote_name="万科A",
    )
    assert "name_conflict" in status["identity_issues"]


# ---------------- 3. ObserveOnlyRule 文案与板块对齐 ----------------


@pytest.mark.parametrize("code,expected_fragment", [
    ("300001", "创业板"),    # 创业板
    ("301001", "创业板"),    # 创业板
    ("688001", "科创板"),    # 科创板
    ("688501", "科创板"),    # 科创板 CDR
    ("830001", "北交所"),    # 北交所 8 开头
    ("920001", "北交所"),    # 北交所 920 开头
])
def test_observe_only_message_uses_actual_board(code, expected_fragment):
    """ObserveOnlyRule 文案必须与实际 board_type 一致。"""
    decision = ObserveOnlyRule().check(
        RiskContext(code=code, action="buy", board_tag="observe_only")
    )
    assert decision.level == RiskLevel.BLOCK
    assert expected_fragment in decision.message, (
        f"{code} 的 board_type 应匹配文案“{expected_fragment}”，"
        f"实际：“{decision.message}”"
    )


def test_observe_only_message_does_not_falsely_call_main_board_observ():
    """主板不属观察板；ObserveOnlyRule 在主板上必须直接 pass。"""
    decision = ObserveOnlyRule().check(
        RiskContext(code="000001", action="buy", board_tag="tradeable")
    )
    assert decision.level == RiskLevel.PASS
    assert "创业板" not in decision.message
    assert "科创板" not in decision.message
    assert "北交所" not in decision.message


def test_observe_only_message_for_unknown_or_missing_board_is_conservative():
    """unknown / 缺身份 仍归类为观察（保守），文案应明确“未知/缺身份”语义，
    而非笼统称 创业板/科创板/北交所。
    """
    decision = ObserveOnlyRule().check(
        RiskContext(code="999999", action="buy", board_tag="observe_only")
    )
    assert decision.level == RiskLevel.BLOCK
    # 文案可以提到“观察标的”但不应只点名 创业板/科创板/北交所 三选一。
    # 允许出现以下保守短语之一："未知" 或 "待核验" 或 "板块待核验"
    msg = decision.message
    assert (
        "未知" in msg or "待核验" in msg or "板块待核验" in msg
    ), f"unknown 板块应使用保守文案，实际：“{msg}”"


# ---------------- 4. 风控链路在真实名称冲突时仍 block ----------------


def test_real_main_board_name_conflict_still_blocked_by_risk():
    """即便主板（tradeable），只要标签/传入/行情名称冲突触发 name_conflict，
    风控链路必须 block — identity_issues 不允许绕过 BlacklistRule。
    """
    engine = RiskEngine()
    engine.register(BlacklistRule())
    engine.register(ObserveOnlyRule())
    tag = _tag_row("000002", name="万科A")
    status = stock_tagger.resolve_status(
        "000002", tag, name="万科B", quote_name="万 科B",
    )
    assert "name_conflict" in status["identity_issues"]
    ctx = RiskContext(
        code="000002", action="buy",
        board_tag=status["board_tag"],
        is_st=status["is_st"], is_suspended=status["is_suspended"],
        is_delisting=status["is_delisting"],
    )
    result = engine.check(ctx)
    # 不论是 BLACKLIST 还是 OBSERVE_ONLY 通道，至少一个规则应 BLOCK。
    assert any(
        d["level"] == RiskLevel.BLOCK.value for d in result["decisions"]
    ), f"身份冲突未触发 block，结果={result}"


# ---------------- 5. fail-closed：缺失/未知/板块冲突/停牌/黑名单 ----------------


@pytest.mark.parametrize("kwargs", [
    {"tag": None},                                # missing_stock_tag
    {"code": "999999"},                           # invalid_or_unknown_code
    {"code": "600abc"},                           # 错代码
    {"overrides": {"board_type": "star"}},        # board_type_conflict
])
def test_fail_closed_for_identity_issues(kwargs):
    code = kwargs.pop("code", "000001")
    overrides = kwargs.pop("overrides", {})
    tag = _tag_row(code, **overrides) if overrides else None
    status = stock_tagger.resolve_status(code, tag)
    assert status["is_tradeable"] is False
    assert status["execution_authorized"] is False


@pytest.mark.asyncio
async def test_suspended_and_blacklist_fail_closed(db_factory):
    """停牌/黑名单必须经风控 block，且 status.is_tradeable=False。"""
    async with db_factory() as db:
        db.add(_tag_row("000001", name="平安银行", is_suspended=True))
        db.add(StockBlacklist(
            code="000002", reason="manual", source="manual",
            start_date=date.today(), auto_expire=False,
        ))
        await db.commit()

        status_suspended = await stock_tagger.load_status(db, "000001")
        assert status_suspended["is_tradeable"] is False
        assert status_suspended["is_suspended"] is True

        status_blacklist = await stock_tagger.load_status(db, "000002")
        assert status_blacklist["is_tradeable"] is False
        assert status_blacklist["active_blacklist"] is True


# ---------------- 6. 端到端：旧候选名称冲突 + 全角行情名称识别 ST ----------------


@pytest.mark.asyncio
async def test_old_candidate_normalized_matches_new_quote_name(db_factory):
    """候选的 name='万  科Ａ'，行情 '万科A'：归一化后相等，
    不应被误判为冲突；同时另一只 tag.name='ST平安'，行情 'ＳＴ平安' 应识别 ST。
    """
    async with db_factory() as db:
        db.add_all([
            _tag_row("000002", name="万科A"),
            _tag_row("000005", name="ST平安"),
            StockSpot(code="000002", name="万科A", price=10),
            StockSpot(code="000005", name="ＳＴ平安", price=10),
        ])
        await db.commit()

        info = await stock_tagger.get_signal_filter(db)
        # 000002 不在 blocked/observe；000005 因 ST 必须 blocked
        assert "000002" not in info["blocked_codes"]
        assert "000005" in info["blocked_codes"]

        # 旧候选以全角形式传入 000002：归一化后应通过（不是冲突）
        kept = await stock_tagger.filter_signals(
            db,
            [{"code": "000002", "name": "万  科Ａ", "buy_allowed": True}],
        )
        assert len(kept) == 1
        assert kept[0]["is_tradeable"] is True
        assert kept[0]["buy_allowed"] is True

        # 全角 ST 行情 → 000005 必被剔除
        dropped = await stock_tagger.filter_signals(
            db, [{"code": "000005", "name": "ST平安"}]
        )
        assert dropped == []


@pytest.mark.parametrize("other", ["万科Ｂ", "万科a", "万科А", "万科Ⓐ", "万科ᴬ", "万科A\u200b", "另一公司A"])
def test_nonwidth_compatibility_and_true_name_conflicts_are_not_folded(other):
    status = stock_tagger.resolve_status("000002", _tag_row("000002", "万科A"), quote_name=other)
    assert "name_conflict" in status["identity_issues"]
    assert not status["is_tradeable"]


@pytest.mark.parametrize("slot", ["tag", "supplied", "quote"])
@pytest.mark.parametrize("raw", ["ＳＴ测试", "＊ＳＴ测试", "Ｓ＊ＳＴ测试"])
def test_fullwidth_st_risk_in_every_name_source(slot, raw):
    tag = _tag_row("000002", raw if slot == "tag" else "测试")
    status = stock_tagger.resolve_status("000002", tag,
        name=raw if slot == "supplied" else "", quote_name=raw if slot == "quote" else "")
    assert status["is_st"] and status["board_tag"] == "blocked"


@pytest.mark.parametrize("raw", [None, True, [], {}, 12, "ＮＡＮ", "ＮＯＮＥ", "ＮＵＬＬ", "－－"])
def test_invalid_and_fullwidth_placeholders_do_not_supply_identity(raw):
    assert stock_tagger.clean_name(raw) == ""


def test_main_board_observe_status_is_not_overridden_by_code():
    decision = ObserveOnlyRule().check(RiskContext(code="000002", board_tag="observe_only"))
    assert decision.level == RiskLevel.BLOCK
    assert "身份" in decision.message
    assert all(x not in decision.message for x in ("创业板", "科创板", "北交所"))
    assert ObserveOnlyRule().check(
        RiskContext(code="000002", action="sell", board_tag="observe_only")
    ).level == RiskLevel.PASS
