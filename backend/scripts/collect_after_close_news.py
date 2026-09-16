"""盘后利好扫描 — 独立脚本（主板 + ST）。

盘后抓取两类信息，范围限定「主板 + ST」个股：
1. 券商研报：全市场最新个股研报 + 按行业聚合的“产业链热度”
2. 个股重大利好公告：重大资产重组 / 债权人变更·重整 / 中标大单 /
   业绩大增 / 困境反转·业绩拐点 / 摘帽预期 / 控制权变更 / 回购增持

抓取策略（关键：区分「增量型」与「状态型」，避免漏掉早期提交的事件）：
- 增量型（中标/业绩大增/回购/控制权变更等，时效强）：
  全局关键词 + 近 `--days` 天（默认 7）。
- 状态型/困境反转（摘帽/摘星/重整/债权人变更/扭亏/重大重组，跨数日~数周）：
  状态型事件分两条互补链路召回，识别“首次提交时间 → 最新进展”，
  覆盖“已提交申请但暂无最新公告”的长周期事件（重大资产重组/控制权变更
  在主板同样存在，不能只盯着 ST）：
  1. 主板（非 ST）：对状态型关键词做长窗口全文召回（近 `--state-lookback`
     天，默认 90），不依赖 akshare 1.18.54 有已知 bug 的个股公告接口。
  2. ST 个股：逐只定向补查（searchkey=代码，翻页查全近 `--st-lookback` 天，
     默认 45），抓更精确的个股级公告进展。

复用已有能力（不重复造轮子）：
- app.news.sources.cninfo.CninfoSource  巨潮公告(重大事件关键词召回 + 个股名称)
- app.news.catalyst.classify_news_event 事件分级
- app.core.stock_tagger                 板块识别
- app.db.session.async_session          读取 StockTag(名称/ST 标记)

说明：本脚本只产出“盘后利好扫描报告”，不写入交易/风控链路。
重整/摘帽等困境反转标的按用户要求纳入扫描结果，但输出会标注「ST / 高波动」风险。

运行（需在含 akshare 1.18.54 的后端环境，例如 docker exec 或已激活的后端 venv）：
    cd backend
    python scripts/collect_after_close_news.py [--days 7] [--state-lookback 90] [--st-lookback 45] [--top 200] [--inspect]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

from app.core.stock_tagger import stock_tagger  # noqa: E402
from app.news.catalyst import classify_news_event  # noqa: E402
from app.news.sources.cninfo import CninfoSource  # noqa: E402

# 主板（含深市原中小板，当前均归属“可交易”口径）
MAIN_BOARDS = {"main_sh", "main_sz", "sme"}

# 全局增量窗口（ST 状态型补查窗口独立可配，见 --st-lookback，默认 45 天）
GLOBAL_LOOKBACK_DEFAULT = 7

# 事件类型 -> 中文标签（复用 catalyst 的 event_type key，另补表格化命名）
_EVENT_LABELS = {
    "major_restructuring": "重大资产重组",
    "distress_restructuring": "重整/债权人变更",
    "delisting_removal": "摘帽预期",
    "risk_warning_review": "摘帽审核",
    "control_change": "控制权变更",
    "m&a_review_approved": "并购重组过会",
    "m&a_review_scheduled": "并购重组上会",
    "major_order": "中标大单",
    "earnings_surge": "业绩大增",
    "earnings_reversal": "业绩预增/扭亏",
    "earnings_growth": "业绩增长",
    "buyback": "回购/增持",
    "asset_transaction": "资产收购/置换",
    "regulatory_approval": "监管批复",
    "major_project": "重大项目",
    "performance_compensation": "业绩承诺补偿",
}

_EVENT_ORDER = [
    "major_restructuring",
    "distress_restructuring",
    "delisting_removal",
    "control_change",
    "m&a_review_approved",
    "m&a_review_scheduled",
    "major_order",
    "earnings_surge",
    "earnings_reversal",
    "earnings_growth",
    "buyback",
    "asset_transaction",
    "regulatory_approval",
    "major_project",
    "performance_compensation",
]

# 「状态型/困境反转」事件：需要长窗口定向补查，输出“首次提交 → 最新进展”
_STATE_EVENT_TYPES = {
    "distress_restructuring",
    "delisting_removal",
    "major_restructuring",
    "control_change",
    "earnings_reversal",
}

# 主板状态型事件的长窗口全文召回关键词（跨数周~数月推进，7 天窗口会漏）。
# 与增量型 _MATERIAL_SEARCH_KEYWORDS 有重叠，但这里用更长窗口 + 更大翻页做查全。
_STATE_SEARCH_KEYWORDS = (
    "重大资产重组",
    "购买资产",
    "控制权",
    "实际控制人",
    "重整",
    "撤销风险警示",
)

# 评级视为“正向/上调”的关键词
_POSITIVE_RATING_KEYWORDS = (
    "买入", "增持", "强烈推荐", "推荐", "优于大市", "跑赢", "上调", "首次", "强烈看好",
)


def _fmt_time(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    s = str(v).strip()
    return "" if s in ("NaT", "nan", "None") else s[:16]


def _pick_col(df: pd.DataFrame, *candidates: str) -> str:
    """从 DataFrame 里按候选名挑列（先精确、后包含），找不到返回空串。"""
    cols = [str(c).strip() for c in df.columns]
    for cand in candidates:
        for c in cols:
            if c == cand:
                return c
    for cand in candidates:
        for c in cols:
            if cand and cand in c:
                return c
    return ""


def _val(row, col: str) -> str:
    if not col or col not in row.index:
        return ""
    v = row.get(col)
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def is_st_name(name: str) -> bool:
    return "ST" in str(name or "").upper()


def in_scope(code: str, is_st: bool) -> bool:
    """主板(600/601/603/605/000/001/003/002) 或 ST。"""
    return stock_tagger.get_board_type(code) in MAIN_BOARDS or bool(is_st)


def classify_positive(title: str, is_st: bool) -> tuple[str, str]:
    """返回 (事件中文标签, event_type key)；非利好返回 ("", "")。

    与 catalyst 互补：重整/债权人/摘帽在 catalyst 中被记为 risk（其服务于正常交易池），
    这里按用户要求把它们转正为“困境反转/摘帽预期”扫描信号。
    """
    text = str(title or "").strip()
    if not text:
        return "", ""

    # 困境反转：重整 / 债权人变更（ST 与非 ST 均纳入，作为扫描信号）
    if any(k in text for k in (
        "被债权人申请重整", "被债权人申请预重整", "申请预重整", "启动预重整",
        "预重整投资", "重整投资协议", "重整计划", "债权人变更",
    )):
        return _EVENT_LABELS["distress_restructuring"], "distress_restructuring"

    # 摘帽预期
    if any(k in text for k in (
        "撤销风险警示", "撤销其他风险警示", "撤销退市风险警示", "摘帽", "摘星",
    )):
        return _EVENT_LABELS["delisting_removal"], "delisting_removal"

    # 业绩拐点/扭亏（catalyst 里扭亏为盈归入 hard，这里显式覆盖保证口径）
    if any(k in text for k in ("扭亏为盈", "同比扭亏", "业绩拐点", "亏损收窄", "业绩反转")):
        return _EVENT_LABELS["earnings_reversal"], "earnings_reversal"

    grade, etype, _adj = classify_news_event(text)
    if grade in ("hard", "medium") and etype in _EVENT_LABELS:
        return _EVENT_LABELS[etype], etype
    return "", ""


async def load_stock_tag_map() -> dict[str, dict]:
    """读取 StockTag 的名称与 ST 标记，作为名称/ST 的补充证据。"""
    try:
        from sqlalchemy import select

        from app.db.session import async_session
        from app.models.stock import StockTag

        async with async_session() as s:
            result = await s.execute(select(StockTag))
            return {
                t.code: {"name": t.name or "", "is_st": bool(t.is_st)}
                for t in result.scalars().all()
            }
    except Exception as e:  # noqa: BLE001
        logger.warning(f"StockTag 读取失败(将仅用名称推断 ST): {e}")
        return {}


def _announcements_from_frame(df: pd.DataFrame) -> list[dict]:
    """把巨潮标准公告 DataFrame(代码/简称/公告标题/公告时间/公告链接) 转成 item 列表。"""
    if df is None or getattr(df, "empty", True):
        return []
    if "公告时间" in df.columns:
        df = df.sort_values("公告时间", ascending=False)
    items: list[dict] = []
    for _, row in df.iterrows():
        code = CninfoSource._normalize_code(row.get("代码", ""))
        if not (code.isdigit() and len(code) == 6):
            continue
        title = str(row.get("公告标题", "") or "").strip()
        if not title:
            continue
        items.append({
            "code": code,
            "name": str(row.get("简称", "") or "").strip(),
            "title": title,
            "publish_time": row.get("公告时间"),
            "url": str(row.get("公告链接", "") or ""),
        })
    return items


async def fetch_announcements(days: int) -> list[dict]:
    """复用巨潮源抓取近 N 天公告（全局关键词 + 少量最新页），保留 代码/简称 供 ST 识别。"""
    source = CninfoSource()
    try:
        df = await source._fetch_disclosures(lookback_days=max(1, days))
    except Exception as e:  # noqa: BLE001
        logger.error(f"巨潮公告抓取失败: {e}")
        return []
    return _announcements_from_frame(df)


async def fetch_st_turnaround(
    st_codes: list[str],
    lookback_days: int = 45,
    concurrency: int = 8,
) -> list[dict]:
    """对 ST 个股逐只定向补查近 `lookback_days` 天公告，聚合“状态型”事件。

    直接调用巨潮官方查询接口（与 CninfoSource._fetch_global_page_set 同源：
    POST data + searchkey=代码 + 按 totalAnnouncement 翻页查全），不依赖
    akshare 1.18.54 有已知 bug 的 stock_zh_a_disclosure_report_cninfo。
    返回：每只 ST 每个状态型事件一条，含 首次提交时间/最新进展时间/最新标题。
    """
    if not st_codes:
        logger.warning("无 ST 代码，跳过 ST 定向补查")
        return []

    from datetime import timedelta

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    start = now - timedelta(days=max(1, lookback_days))
    sem = asyncio.Semaphore(max(1, concurrency))
    total = len(st_codes)
    done = 0

    async def one(code: str):
        nonlocal done
        async with sem:
            try:
                df = await asyncio.to_thread(
                    CninfoSource._fetch_global_page_set,
                    start_date=CninfoSource._date_text(start),
                    end_date=CninfoSource._date_text(now),
                    keyword=code,
                    max_pages=10,
                )
                return code, _announcements_from_frame(df)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"ST 公告定向补查失败 [{code}]: {e}")
                return code, []
            finally:
                done += 1
                if done % 50 == 0 or done == total:
                    logger.info(f"ST 定向补查进度 {done}/{total}")

    results = await asyncio.gather(*(one(c) for c in st_codes))
    return _aggregate_state_events(results)


def _aggregate_state_events(results: list[tuple[str, list[dict]]]) -> list[dict]:
    """把 (code, items) 列表聚合成状态型事件，输出“首次提交 → 最新进展”。

    items 须为时间倒序(最新在前)：同类事件取 首次提交(最旧)=first_time，
    最新进展(最新)=last_time/last_title。
    """
    agg: dict[tuple[str, str], dict] = {}
    for code, items in results:
        # 统一按发布时间倒序(最新在前)，保证“越往后遍历越旧”的首次提交时间假设成立
        # （ST 逐只补查天然倒序，主板多关键词混排后不保证，故此处显式排序）
        items = sorted(items, key=lambda x: _fmt_time(x.get("publish_time")), reverse=True)
        for item in items:
            label, etype = classify_positive(item["title"], True)
            if etype not in _STATE_EVENT_TYPES:
                continue
            key = (code, etype)
            if key not in agg:
                agg[key] = {
                    "code": code,
                    "name": item.get("name", ""),
                    "etype": etype,
                    "label": label,
                    "first_time": item["publish_time"],
                    "last_time": item["publish_time"],
                    "last_title": item["title"],
                }
            else:
                # 越往后遍历越旧，覆盖 first_time 为最旧=首次提交
                agg[key]["first_time"] = item["publish_time"]
    return list(agg.values())


async def fetch_main_state_events(
    lookback_days: int = 90,
    concurrency: int = 4,
    max_pages: int = 15,
) -> list[dict]:
    """主板（非 ST）状态型事件：长窗口关键词全文召回 + 聚合。

    覆盖重大资产重组/控制权变更/重整/摘帽等在主板同样存在的跨周期事件，
    弥补增量型 7 天窗口对“首次公告较早、进展仍在推进”事件的遗漏。
    直接调巨潮官方接口（与 ST 定向补查同源），按状态型关键词做长窗口召回。
    """
    from datetime import timedelta

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    start = now - timedelta(days=max(1, lookback_days))
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(keyword: str) -> list[dict]:
        async with sem:
            try:
                df = await asyncio.to_thread(
                    CninfoSource._fetch_global_page_set,
                    start_date=CninfoSource._date_text(start),
                    end_date=CninfoSource._date_text(now),
                    keyword=keyword,
                    max_pages=max_pages,
                )
                return _announcements_from_frame(df)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"主板状态型关键词召回失败 [{keyword}]: {e}")
                return []

    frames = await asyncio.gather(*(one(k) for k in _STATE_SEARCH_KEYWORDS))

    # 按代码分组后进行事件聚合（与 ST 逐只补查共用同一套聚合逻辑）
    by_code: dict[str, list[dict]] = {}
    for items in frames:
        for item in items:
            by_code.setdefault(item["code"], []).append(item)
    pairs = [(code, items) for code, items in by_code.items()]
    return _aggregate_state_events(pairs)


def _parse_stock_reports(df: pd.DataFrame) -> list[dict]:
    if df is None or getattr(df, "empty", True):
        return []
    code_c = _pick_col(df, "代码", "股票代码", "证券代码", "SECURITY_CODE")
    name_c = _pick_col(df, "名称", "股票名称", "证券简称", "简称", "SECURITY_NAME_ABBR")
    rating_c = _pick_col(df, "评级", "投资评级", "最新评级", "研究评级", "emRatingName")
    org_c = _pick_col(df, "机构", "研究机构", "机构名称", "券商", "orgSName")
    date_c = _pick_col(df, "公告日期", "发布日期", "研报日期", "日期", "publishDate", "预测日期")
    target_c = _pick_col(df, "目标价", "目标价格")
    industry_c = _pick_col(df, "行业", "所属行业", "indvInduName")

    out: list[dict] = []
    for _, row in df.iterrows():
        code = CninfoSource._normalize_code(_val(row, code_c))
        name = _val(row, name_c)
        rating = _val(row, rating_c)
        out.append({
            "code": code,
            "name": name,
            "rating": rating,
            "org": _val(row, org_c),
            "date": _fmt_time(_val(row, date_c)),
            "target": _val(row, target_c),
            "industry": _val(row, industry_c),
        })
    return out


async def fetch_research_reports() -> list[dict]:
    """全市场最新个股研报（评级/机构/目标价/行业）。"""
    import akshare as ak  # noqa: E402

    fn = getattr(ak, "stock_zyjs_report_em", None)
    if fn is None:
        logger.warning("akshare 无 stock_zyjs_report_em，跳过个股研报")
        return []
    try:
        df = await asyncio.to_thread(fn)
        reports = _parse_stock_reports(df)
        logger.info(f"个股研报抓取: {len(reports)} 条")
        return reports
    except Exception as e:  # noqa: BLE001
        logger.error(f"个股研报抓取失败(可用 --inspect 查看列名): {e}")
        return []


async def inspect_sources(days: int) -> None:
    """打印各数据源原始列名，便于首次运行核对字段。"""
    source = CninfoSource()
    try:
        df = await source._fetch_disclosures(lookback_days=max(1, days))
        print("== 巨潮公告列名 ==")
        print(list(df.columns) if df is not None else "None")
        if df is not None and not getattr(df, "empty", True):
            print(df.head(3).to_string())
    except Exception as e:  # noqa: BLE001
        print(f"巨潮公告 inspect 失败: {e}")

    import akshare as ak  # noqa: E402

    fn = getattr(ak, "stock_zyjs_report_em", None)
    if fn is None:
        print("akshare 无 stock_zyjs_report_em")
    else:
        try:
            rdf = await asyncio.to_thread(fn)
            print("\n== 个股研报列名 ==")
            print(list(rdf.columns) if rdf is not None else "None")
            if rdf is not None and not getattr(rdf, "empty", True):
                print(rdf.head(3).to_string())
        except Exception as e:  # noqa: BLE001
            print(f"个股研报 inspect 失败: {e}")


def _report_markdown(
    scan_date: str,
    industry_heat: list[dict],
    reports: list[dict],
    state_events: list[dict],
    event_groups: dict[str, list[dict]],
    total_news: int,
    tag_map: dict[str, dict],
) -> str:
    lines: list[str] = []
    lines.append(f"# 盘后利好扫描报告 — {scan_date}")
    lines.append("")
    lines.append("> 范围：主板 + ST 个股；仅作信息扫描，不构成投资建议。")
    lines.append("> 重整/摘帽等困境反转标的波动大，标注「ST / 高波动」风险。")
    lines.append("")

    # 一、产业链热度
    lines.append("## 一、券商研报 · 产业链热度 Top")
    lines.append("")
    if industry_heat:
        lines.append("| 排名 | 行业/产业链 | 研报数 | 买入/增持 | 代表个股 |")
        lines.append("| --- | --- | ---: | ---: | --- |")
        for i, item in enumerate(industry_heat, 1):
            lines.append(
                f"| {i} | {item['industry'] or '未标注'} | {item['count']} | {item['positive']} | "
                f"{item['samples']} |"
            )
    else:
        lines.append("（未抓取到产业链研报数据）")
    lines.append("")

    # 二、个股研报明细
    lines.append("## 二、券商研报 · 个股研报明细（主板+ST）")
    lines.append("")
    if reports:
        lines.append("| 代码 | 名称 | 评级 | 机构 | 日期 | 目标价 | 行业 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for r in reports:
            tag = tag_map.get(r["code"]) or {}
            is_st = bool(tag.get("is_st")) or is_st_name(r["name"])
            name = r["name"] or tag.get("name", "")
            lines.append(
                f"| {r['code']} | {name}{' [ST]' if is_st else ''} | {r['rating']} | "
                f"{r['org']} | {r['date']} | {r['target']} | {r['industry']} |"
            )
    else:
        lines.append("（未抓取到个股研报数据）")
    lines.append("")

    # 三、状态型/困境反转事件（主板 + ST · 进行中）
    lines.append("## 三、状态型/困境反转事件（主板 + ST · 进行中）")
    lines.append("")
    lines.append("> 主板走长窗口关键词召回，ST 逐只定向补查；识别“首次提交 → 最新进展”，覆盖暂无新公告的长周期事件。")
    lines.append("")
    if state_events:
        lines.append("| 代码 | 名称 | 事件 | 首次提交 | 最新进展 | 最新公告 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for it in state_events:
            tag = tag_map.get(it["code"]) or {}
            is_st = bool(tag.get("is_st")) or is_st_name(it.get("name", ""))
            name = it.get("name", "") or tag.get("name", "")
            lines.append(
                f"| {it['code']} | {name}{' [ST]' if is_st else ''} | {it['label']} | "
                f"{_fmt_time(it['first_time'])} | {_fmt_time(it['last_time'])} | "
                f"{it['last_title'][:50]} |"
            )
    else:
        lines.append("（未抓到状态型/困境反转事件）")
    lines.append("")

    # 四、个股重大利好 · 增量型
    lines.append("## 四、个股重大利好 · 增量型（全局关键词，近 7 天）")
    lines.append("")
    if total_news == 0:
        lines.append("（未命中主板+ST范围内的利好公告）")
    for etype in _EVENT_ORDER:
        items = event_groups.get(etype, [])
        if not items:
            continue
        lines.append(f"### {_EVENT_LABELS.get(etype, etype)}（{len(items)}）")
        lines.append("")
        lines.append("| 代码 | 名称 | 公告标题 | 时间 |")
        lines.append("| --- | --- | --- | --- |")
        for it in items:
            tag = tag_map.get(it["code"]) or {}
            is_st = bool(tag.get("is_st")) or is_st_name(it["name"])
            name = it["name"] or tag.get("name", "")
            lines.append(
                f"| {it['code']} | {name}{' [ST]' if is_st else ''} | "
                f"{it['title'][:60]} | {_fmt_time(it['publish_time'])} |"
            )
        lines.append("")
    return "\n".join(lines)


async def run(days: int, state_lookback: int, st_lookback: int, top: int, inspect: bool) -> str:
    if inspect:
        await inspect_sources(days)
        return ""

    tag_map = await load_stock_tag_map()
    logger.info(f"StockTag 已加载 {len(tag_map)} 条")

    st_codes = sorted(c for c, t in tag_map.items() if t.get("is_st"))
    logger.info(f"ST 个股清单: {len(st_codes)} 只")

    # 1. 增量：全局公告抓取 + 分类 + 范围过滤
    anns = await fetch_announcements(days)
    event_groups: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    total = 0
    for it in anns:
        tag = tag_map.get(it["code"]) or {}
        is_st = bool(tag.get("is_st")) or is_st_name(it["name"])
        if not in_scope(it["code"], is_st):
            continue
        label, etype = classify_positive(it["title"], is_st)
        if not etype:
            continue
        key = (it["code"], it["title"])
        if key in seen:
            continue
        seen.add(key)
        event_groups.setdefault(etype, []).append(it)
        total += 1
    for items in event_groups.values():
        items.sort(key=lambda x: _fmt_time(x.get("publish_time")), reverse=True)
    logger.info(f"增量利好公告命中: {total} 条（主板+ST）")

    # 2. 状态：ST 定向补查（直接调巨潮官方接口翻页查全，识别进行中状态）
    st_turnaround = await fetch_st_turnaround(st_codes, lookback_days=st_lookback)
    logger.info(f"ST 状态型事件: {len(st_turnaround)} 条")

    # 2b. 状态：主板（非 ST）长窗口关键词召回，补齐主板重大资产重组/控制权变更/重整等
    main_state = await fetch_main_state_events(lookback_days=state_lookback)
    main_state = [
        it for it in main_state
        if stock_tagger.get_board_type(it["code"]) in MAIN_BOARDS
        and not bool((tag_map.get(it["code"]) or {}).get("is_st"))
        and not is_st_name(it.get("name", ""))
    ]
    logger.info(f"主板状态型事件: {len(main_state)} 条")

    # 合并主板 + ST 状态型事件（按 (code, etype) 去重，ST 优先走定向补查明细）
    state_events: list[dict] = []
    state_key_seen: set[tuple[str, str]] = set()
    for it in list(main_state) + list(st_turnaround):
        key = (it["code"], it["etype"])
        if key in state_key_seen:
            continue
        state_key_seen.add(key)
        state_events.append(it)
    state_events.sort(key=lambda x: _fmt_time(x.get("last_time")), reverse=True)
    logger.info(f"状态型/困境反转事件合计: {len(state_events)} 条")

    # 3. 研报抓取
    reports = await fetch_research_reports()

    # 4. 研报范围过滤（主板+ST）
    scoped_reports = []
    for r in reports:
        if not (r["code"].isdigit() and len(r["code"]) == 6):
            continue
        tag = tag_map.get(r["code"]) or {}
        is_st = bool(tag.get("is_st")) or is_st_name(r["name"])
        if in_scope(r["code"], is_st):
            scoped_reports.append(r)

    # 5. 产业链热度（按行业聚合，只统计范围内个股研报）
    industry_stat: dict[str, dict] = {}
    for r in scoped_reports:
        ind = r["industry"] or "未标注"
        stat = industry_stat.setdefault(ind, {"count": 0, "positive": 0, "samples": []})
        stat["count"] += 1
        if any(k in r["rating"] for k in _POSITIVE_RATING_KEYWORDS):
            stat["positive"] += 1
        if len(stat["samples"]) < 3 and r["name"]:
            stat["samples"].append(f"{r['name']}{' [ST]' if is_st_name(r['name']) else ''}")
    industry_heat = [
        {
            "industry": ind,
            "count": s["count"],
            "positive": s["positive"],
            "samples": "、".join(s["samples"]),
        }
        for ind, s in sorted(industry_stat.items(), key=lambda kv: (-kv[1]["count"], ind))
    ]
    industry_heat = industry_heat[:15]

    # 6. 研报明细排序：评级正向优先，再按日期降序
    def _report_sort_key(r):
        positive = 1 if any(k in r["rating"] for k in _POSITIVE_RATING_KEYWORDS) else 0
        return (-positive, r["date"])
    scoped_reports.sort(key=_report_sort_key)
    scoped_reports = scoped_reports[:top]

    scan_date = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    md = _report_markdown(
        scan_date, industry_heat, scoped_reports, state_events, event_groups, total, tag_map
    )
    return md


def main() -> None:
    parser = argparse.ArgumentParser(description="盘后利好扫描（主板 + ST）")
    parser.add_argument("--days", type=int, default=GLOBAL_LOOKBACK_DEFAULT,
                        help="增量型公告全局回溯天数(默认7)")
    parser.add_argument("--state-lookback", type=int, default=90,
                        help="主板状态型事件长窗口召回回溯天数(默认90)")
    parser.add_argument("--st-lookback", type=int, default=45,
                        help="ST 状态型事件定向补查回溯天数(默认45)")
    parser.add_argument("--top", type=int, default=200, help="个股研报展示上限(默认200)")
    parser.add_argument("--inspect", action="store_true", help="仅打印数据源原始列名")
    parser.add_argument("--output-dir", type=str, default="", help="报告输出目录(默认仓库 outputs/)")
    args = parser.parse_args()

    md = asyncio.run(run(args.days, args.state_lookback, args.st_lookback, args.top, args.inspect))
    if args.inspect:
        return

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out_dir = args.output_dir or os.path.join(root, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    date_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    path = os.path.join(out_dir, f"盘后利好扫描_{date_str}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)

    print(md)
    print(f"\n✅ 报告已写入: {path}")


if __name__ == "__main__":
    main()