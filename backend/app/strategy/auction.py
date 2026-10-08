"""竞价分析模块 — 9:15-9:25集合竞价异动识别

核心能力:
- 竞价数据采集(AkShare接口)
- 竞价异动识别(高开/量比/撤单)
- 竞价因子计算(auction_strength/auction_volume_ratio/auction_open_change)
- 竞价强势股筛选
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import desc, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.data.auction_evidence import (
    auction_evidence_status,
    auction_source_frame_key,
    auction_latest_order,
    latest_auction_ids,
    diagnose_missing_evidence_fields,
    merge_diagnoses,
    volume_in_shares,
)
from app.data.fund_flow_clock import eastmoney_quote_clock, local_clock
from app.core.data_date import resolve_latest_trade_date
from app.models.stock import AuctionData, StockKline, StockTag
from app.core.stock_tagger import stock_tagger, TAG_TRADEABLE


# ========== 数据结构 ==========

class AuctionAnomaly(str, Enum):
    """竞价异动类型"""
    HIGH_OPEN = "high_open"            # 高开>3%
    ULTRA_HIGH_OPEN = "ultra_high_open"  # 超高开>7%
    VOLUME_SPIKE = "volume_spike"      # 量比>3
    CANCEL_SUSPICION = "cancel_suspicion"  # 来源显式撤单嫌疑，价格轨迹不等于撤单
    LIMIT_UP_BID = "limit_up_bid"      # 竞价涨停价挂单
    LOW_OPEN = "low_open"              # 低开<-3%


@dataclass
class AuctionSignal:
    """竞价信号"""
    code: str
    name: str = ""
    trade_date: date = None
    auction_price: float = 0
    prev_close: float = 0
    open_change: float = 0         # 竞价涨跌幅%
    volume_ratio: float = 0        # 量比
    auction_amount: float = 0      # 竞价金额
    anomalies: list[AuctionAnomaly] = field(default_factory=list)
    strength_score: float = 0      # 竞价强度评分(0-100)
    is_tradeable: bool = True
    evidence_status: str = "unknown"


# ========== 竞价数据采集 ==========

# 来源版本与无时钟的 *_unverified 观察帧明确区分；各源仍须逐行通过契约。
TENCENT_AUCTION_SOURCE_VERSION = "tencent_qt_auction_open_v1"
# 2026-09-29 实测定稿：腾讯同一端点还服务**早中段虚拟撮合**，用独立版本号留证。
#
# 冻结样本见 outputs/auction_source_capability_20260929/samples.jsonl：
#   * 腾讯早中段买卖一价/量相等且为正；只支持该小样本的字段映射，非全市场验收；
#   * 该"买卖相等量"随时间**推进**（600000：139→174→226→291→366→405 手）；
#   * 同一窗口 field[6] 成交量恒为 0、field[37] 成交额恒为 0、field[49] 量比恒为 0；
#   * field[30] 每约 3 秒推进一次（源时钟可用）。
# 交易所规格（上交所行情规格 3.10、深交所 STEP 规范）：集合竞价时段内
# 当前买价/卖价**均为虚拟开盘参考价**，「申买量一」「申卖量一」= 该时刻的
# **虚拟匹配量**；买卖两侧数量相等正说明它是同一个虚拟撮合量。
# 因此早中段帧取 field[9]/[10]（买一价/量）与 field[19]/[20]（卖一价/量），
# 而不是 field[3]/[6]。
#
# 小样本不等于全市场认证；双边价量缺失或不一致时拒收，不能取min伪装合格。
TENCENT_AUCTION_EARLY_SOURCE_VERSION = "tencent_qt_auction_indicative_v1"
# 东财行情：`f124` 是**行情更新时间戳**，本项目已在
# `app/data/fund_flow_clock.eastmoney_quote_clock` 里解码并信任
# （见 `main_fund.py` 的 `individual_fund_flow_v3_f124`）。
# 主域名在本机长期拒连（与 eastmoney/market_fund_flow 的 chronic 失败同源），
# 因此按可用性顺序回退；实测 push2delay 可用且字段齐全。
EASTMONEY_AUCTION_SOURCE_VERSION = "eastmoney_qt_auction_open_v1"
EASTMONEY_QUOTE_HOSTS = ("push2delay.eastmoney.com", "push2.eastmoney.com")
# 300 是实测上限：`ulist.np/get` 一次传 300 个 secid 约 0.27s 返回 300 行，
# 全市场约 2,994 只 → 10 个请求/轮。旧路径 `clist/get` 的 `pz` 硬上限 100，
# 全市场要 30~56 页，且每交易日 09:15–09:25 每分钟跑一轮 —— 那才是过去被限频的成因。
EASTMONEY_AUCTION_BATCH = 300
# 批次间隔仅压平请求突发；非高峰测速和sleep之和不能保证自然窗口时效。
# 终场collector传入原09:25:30截止，逐请求/退避预算不足即停，已收帧照原契约判定。
EASTMONEY_AUCTION_PACE_SEC = 0.08
EASTMONEY_AUCTION_RETRY = 3          # 单个批次的重试次数
EASTMONEY_AUCTION_BACKOFF_SEC = 0.35  # 被限频/出错后的退避基数（× 尝试序号）
EASTMONEY_AUCTION_FIELDS = "f12,f14,f17,f18,f5,f6,f10,f124"

# 新浪实时行情 `hq.sinajs.cn`：**盘中以外的盘前时段也带 provider 时间戳**
# （字段[30]=日期、[31]=时间，见 `_SINA_LIST_FIELDS`）。
# 为什么必须走这个端点而不是 `ak.stock_zh_a_spot`（新浪行情中心）：
#   1. 行情中心的行**没有源时间戳**，采集器只能把 source_quote_at 置空，
#      `auction_provenance_v1` 判 unknown → D2 的 session 三段分桶全部落空
#      （9/28 存下的 1,520 行新浪观察帧 auction_volume 全为 0、status 全 unknown）；
#   2. 行情中心返回的"今开"在 09:25 前尚未形成，值域可疑（9/28 实测出现
#      2.8~1735 的跨度），不是逐秒推进的虚拟匹配价。
# D2 的分桶判据用的是 `source_clock.time()`（status=ok 时），所以带源时钟
# 的早中段帧本身就是这条路线缺的那一环。
SINA_AUCTION_SOURCE_VERSION = "sina_hq_auction_indicative_v1"
SINA_AUCTION_URL = "https://hq.sinajs.cn/list="
# 800 只/请求：实测单请求约 0.1–0.3s，全市场约 4 个请求/轮；
# 与腾讯 100 只/请求、东财 300 只/请求同量级，不引入新的突发压力。
SINA_AUCTION_BATCH = 800
SINA_AUCTION_PACE_SEC = 0.12
SINA_AUCTION_TIMEOUT_SEC = 6.0
# 新浪对无 Referer 的请求会返回 403，必须带上财经首页来源。
SINA_AUCTION_HEADERS = {"Referer": "https://finance.sina.com.cn"}
# 字段顺序（2026-09-29 实测原始响应核对）：
#   0 名称 1 今开 2 昨收 3 最新价(实测早中段为0) 4 最高 5 最低
#   6 买一 7 卖一 8 成交量(股) 9 成交额(元) 10.. 五档量价 …
#   30 日期 YYYY-MM-DD 31 时间 HH:MM:SS 32 状态
# 2026-09-29 实测更正：竞价期**最新价[3] 恒为 0.000、累计量[8] 恒为 0**，
# 虚拟撮合信息在**档位**上：
#   [6]买一价 [7]卖一价 = 虚拟开盘参考价（实测 10/10 只恒等，且 == 腾讯 field[9]）
#   [10]买一量(股) [20]卖一量(股) = 虚拟匹配量（同源两侧需一致）；
# 腾讯以整手报告可能有舍入，跨源异步采样也可有差异，不能断言逐样本精确×100。
_SINA_LIST_FIELDS = {
    "name": 0, "open": 1, "prev_close": 2, "current": 3,
    "bid_price": 6, "ask_price": 7, "volume": 8, "amount": 9,
    "bid_volume": 10, "ask_volume": 20,
    "date": 30, "time": 31,
}


class EastmoneyThrottled(RuntimeError):
    """东财限频/拒服务（HTTP 429、5xx、或返回非 JSON 的错误页）。

    单独建类是为了把"被限频"和"网络不可达"区分开：前者的正确反应是退避并
    换主机，后者是重试当前主机。混在一起会让日志无法回答"到底是不是被限频"。
    """


class AuctionCollector:
    """集合竞价数据采集器"""

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            if value is None or isinstance(value, bool):
                return default
            parsed = float(value)
            if pd.isna(parsed) or np.isinf(parsed):
                return default
            return parsed
        except (TypeError, ValueError):
            text = str(value or "").strip().replace(",", "")
            if not text or text.lower() == "nan":
                return default
            multiplier = 1.0
            if text.endswith("%"):
                text = text[:-1]
            if text.endswith("亿"):
                multiplier = 1e8
                text = text[:-1]
            elif text.endswith("万"):
                multiplier = 1e4
                text = text[:-1]
            try:
                parsed = float(text) * multiplier
                return parsed if np.isfinite(parsed) else default
            except (TypeError, ValueError):
                return default

    @staticmethod
    def _normalize_code(raw) -> str:
        code = str(raw or "").strip()
        for suffix in (".SH", ".SZ", ".BJ", ".sh", ".sz", ".bj"):
            if code.endswith(suffix):
                code = code[:-3]
                break
        for prefix in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
            if code.lower().startswith(prefix):
                code = code[2:]
                break
        return code.zfill(6) if code.isdigit() else code

    @staticmethod
    def _is_live_auction_window(trade_date: date, now: datetime | None = None) -> bool:
        current = now or datetime.now()
        return bool(
            trade_date == current.date()
            and time(9, 15) <= current.time() <= time(9, 25, 30)
        )

    @staticmethod
    def _verified_observed_at(value, trade_date: date) -> datetime | None:
        """只接受竞价窗口内的真实观测/接收时钟，不接受调用方目标标签。"""
        value = local_clock(value)
        if value is None or value.date() != trade_date:
            return None
        if not time(9, 15) <= value.time() <= time(9, 25, 30):
            return None
        return value

    async def collect_auction_data(self, session: AsyncSession,
                                    trade_date: date = None) -> pd.DataFrame:
        """采集盘前观察帧（不作为可执行证据）

        09:25 可认证证据仅由独立腾讯采集器负责，已停止东财主备调用。
        - ak.stock_zh_a_spot() 新浪观察帧，不冒充竞价虚拟匹配量
        """
        trade_date = trade_date or date.today()

        # 离开集合竞价窗口后不再补采。开盘后行情与累计量额不能重标为09:25。
        if not self._is_live_auction_window(trade_date):
            logger.warning(f"竞价采集不在实时窗口，拒绝补造竞价帧: {trade_date}")
            return pd.DataFrame()

        import akshare as ak

        # 两个public接口只证明spot今开/累计量额，不证明虚拟匹配价格/数量。
        # 仍可保存真实接收的观察帧，但不将其声明为可执行竞价证据。
        # 新浪的时间戳没有完整交易日期，不能用本机今日补成源日期。
        loop = __import__("asyncio").get_event_loop()
        sources = (
            ("sina", ak.stock_zh_a_spot),
        )
        col_map = {
            "代码": "code",
            "名称": "name",
            "今开": "open",
            "昨收": "prev_close",
            "成交量": "volume",
            "成交额": "amount",
            "涨跌幅": "change_pct",
            "量比": "volume_ratio",
        }
        for source_name, loader in sources:
            try:
                df = await loop.run_in_executor(None, loader)
            except Exception as exc:
                logger.warning(f"竞价数据源 {source_name} 失败: {exc}")
                continue
            if df is None or df.empty:
                logger.warning(f"竞价数据源 {source_name} 返回空")
                continue
            df = df.rename(
                columns={key: value for key, value in col_map.items() if key in df.columns}
            )
            required_columns = {"code", "open", "prev_close", "volume", "amount"}
            if not required_columns.issubset(df.columns):
                missing = sorted(required_columns.difference(df.columns))
                logger.warning(f"竞价数据源 {source_name} 缺少字段: {missing}")
                continue
            observed_at = datetime.now()
            if not self._verified_observed_at(observed_at, trade_date):
                logger.warning(
                    f"竞价数据源 {source_name} 返回已越过终场窗口: received_at={observed_at.isoformat()}"
                )
                continue
            # 2026-09-18：>=09:25 的帧由腾讯证据路径负责，这里不再写。
            #
            # 原因：`get_snapshot_health` 只按每只代码 `MAX(auction_time)` 取"最新帧"
            # 参与 `feed_complete_count`。本路径（东财/新浪 spot）在 >=09:25 只能标
            # `spot_open_unverified` + `source_quote_at=None`，必然判 `unknown`；
            # 若它的 auction_time 晚于腾讯证据帧，就会把已验证的那一帧**顶掉**，
            # 分子重新变 0、闸门继续 blocked —— 即"用一条不可用的记录遮蔽可用记录"。
            # 该帧本来就不被任何判定采纳（`unknown` 从不计数），跳过不损失任何证据，
            # 只消除这个遮蔽竞态。09:15–09:24 的观察帧照常保存。
            if observed_at.time() >= time(9, 25):
                logger.info(
                    f"竞价数据源 {source_name} 的 {observed_at.strftime('%H:%M:%S')} 帧交由"
                    "腾讯证据路径处理，本路径跳过（该帧无法认证且会遮蔽已验证帧）"
                )
                continue
            df["_auction_source"] = source_name
            df.attrs["auction_observed_at"] = observed_at
            df.attrs["auction_clock_basis"] = "client_received_at"
            # Adapter-owned metadata overrides any upstream dataframe attrs.
            df["source"] = source_name
            df["source_version"] = "akshare_spot_open_v1_unverified"
            df["source_quote_at"] = None
            df["received_at"] = observed_at
            df["price_basis"] = "spot_open_unverified"
            df["volume_basis"] = "intraday_cumulative"
            df["volume_unit"] = "lot100" if source_name == "eastmoney" else "share"
            df["amount_unit"] = "CNY"
            # 2026-09-29：同一段遮蔽竞态的 09:15–09:25 版本。
            #
            # `get_snapshot_health` 按每只代码 `MAX(auction_time)` 取"最新帧"。
            # 本路径的行没有源时钟、`price_basis=spot_open_unverified`，**必然判
            # unknown、从不计数**；而它的 auction_time 是本机接收时刻，完全可能
            # 晚于新浪早中段证据帧的接收时刻 —— 一旦晚，就把已认证帧从"最新帧"
            # 里顶掉，`multi_frame_complete_count` 与 `verified_timely_snapshot_ratio`
            # 一起回落。这些行既不进任何判定，删掉不损失证据，故对"本日已有可认证
            # 帧的代码"直接不写。
            verified_codes = await self._verified_auction_codes(
                session, trade_date, decision_at=observed_at,
            )
            if verified_codes:
                before = len(df)
                df = df[~df["code"].astype(str).isin(verified_codes)]
                skipped = before - len(df)
                if skipped:
                    logger.info(
                        f"竞价观察帧让出 {skipped} 只：这些代码本日已有可认证证据帧，"
                        "无源时钟的观察行会以更晚的 auction_time 遮蔽它们"
                    )
                if df.empty:
                    logger.info(
                        f"竞价观察帧全部让出（{before} 只本日已有可认证帧）: {source_name}"
                    )
                    return df
            logger.info(
                f"竞价数据采集完成: source={source_name}, observed_at={observed_at.isoformat()}, "
                f"{len(df)}只"
            )
            return df

        logger.error("竞价实时数据源全部失败；拒绝使用可变 stock_spot 补造竞价帧")
        return pd.DataFrame()

    async def _collect_from_stock_spot(self, session: AsyncSession, trade_date: date) -> pd.DataFrame:
        """明确禁用可变 spot 兜底；其源时钟和竞价量额无法形成不可变证据。"""
        logger.warning(f"竞价数据不使用 stock_spot 兜底: {trade_date}")
        return pd.DataFrame()

    async def _load_average_daily_volume(
        self,
        session: AsyncSession,
        codes: list[str],
        trade_date: date,
    ) -> dict[str, float]:
        if not codes:
            return {}
        # Same latest five positive finite observations, not five calendar days.
        # A row_number window bounds output but still scans every historical row.
        # Use the existing (code, trade_date) unique index and stop after five
        # qualifying rows per code. 100-code chunks bound SQL parameters, compound
        # terms and Python work while avoiding thousands of async round trips.
        unique_codes = sorted(set(codes))
        grouped: dict[str, list[float]] = {}
        for start in range(0, len(unique_codes), 100):
            parts = []
            for code in unique_codes[start:start + 100]:
                recent = select(
                    StockKline.code, StockKline.volume, StockKline.trade_date,
                ).where(
                    StockKline.code == code,
                    StockKline.trade_date < trade_date,
                    StockKline.volume > 0,
                    StockKline.volume < float("inf"),
                ).order_by(StockKline.trade_date.desc()).limit(5).subquery()
                parts.append(select(recent.c.code, recent.c.volume, recent.c.trade_date))
            bounded = union_all(*parts).subquery()
            result = await session.execute(
                select(bounded.c.code, bounded.c.volume)
                .order_by(bounded.c.code, bounded.c.trade_date.desc())
            )
            for code, volume in result.all():
                normalized = self._normalize_code(code)
                if len(grouped.get(normalized, [])) >= 5:
                    continue
                parsed = self._safe_float(volume)
                if parsed > 0:
                    grouped.setdefault(normalized, []).append(parsed)
        return {
            code: float(np.mean(volumes))
            for code, volumes in grouped.items()
            if volumes
        }

    def _resolve_volume_ratio(self, row: pd.Series, avg_daily_volume: float) -> float:
        provided = self._safe_float(row.get("volume_ratio"), 0.0)
        if provided > 0:
            return round(provided, 3)

        auction_shares = volume_in_shares(row.get("volume"), row.get("volume_unit"))
        if (
            auction_shares is None
            or row.get("volume_basis") not in {"indicative_matched", "auction_matched"}
            or avg_daily_volume <= 0
        ):
            return 0.0

        # StockKline.volume is stored in shares. Preserve the existing intensity
        # formula, but never guess the source unit or use ordinary cumulative volume.
        opening_intensity = auction_shares / avg_daily_volume * 20.0
        return round(min(max(opening_intensity, 0.0), 20.0), 3)

    async def save_auction_data(
        self,
        session: AsyncSession,
        df: pd.DataFrame,
        trade_date: date,
        auction_time: str | None = None,
    ) -> int:
        """按供应商响应的实际接收时钟保存；``auction_time`` 仅作调度提示。"""
        observed_at = self._verified_observed_at(
            df.attrs.get("auction_observed_at"), trade_date
        )
        if observed_at is None:
            logger.warning(
                "竞价帧缺少竞价窗口内的真实观测时钟，拒绝保存: "
                f"requested={auction_time or ''}, trade_date={trade_date}"
            )
            return 0
        snapshot_time = observed_at.strftime("%H:%M:%S")
        if auction_time and auction_time != snapshot_time:
            logger.info(
                "竞价任务目标时点与真实响应时点不同，按真实时点保存: "
                f"requested={auction_time}, observed={snapshot_time}"
            )
        normalized_rows: list[tuple[str, pd.Series]] = []
        for _, row in df.iterrows():
            code = self._normalize_code(row.get("code", ""))
            if not code:
                continue
            normalized_rows.append((code, row))

        if not normalized_rows:
            return 0

        # Only rows that can reach the existing intensity formula need history.
        # Ordinary spot fallback uses intraday_cumulative: its missing ratio must
        # remain zero regardless of daily volume. Reading every stock's history
        # for that fallback blocks critical auction windows without changing data.
        ratio_missing_codes = [
            code for code, row in normalized_rows
            if self._safe_float(row.get("volume_ratio"), 0.0) <= 0
            and self._safe_float(row.get("prev_close")) > 0
            and self._safe_float(row.get("open")) > 0
            and row.get("volume_basis") in {"indicative_matched", "auction_matched"}
            and volume_in_shares(row.get("volume"), row.get("volume_unit")) is not None
        ]
        avg_volume_map = (
            await self._load_average_daily_volume(
                session, list(dict.fromkeys(ratio_missing_codes)), trade_date,
            )
            if ratio_missing_codes
            else {}
        )
        # 036: append independent source frames, not a mutable per-second cell.
        # A repeated provider update keeps its first payload and receipt clocks.
        pending_frames: dict[str, dict] = {}
        count = 0
        for code, row in normalized_rows:
            prev_close = self._safe_float(row.get("prev_close"))
            open_price = self._safe_float(row.get("open"))
            # 东财在开盘价尚未形成时会返回 0；旧逻辑把它换算成 -100%，
            # 后续轨迹识别会误报“深水翻红”。无有效竞价价/昨收的帧不落库。
            if prev_close <= 0 or open_price <= 0:
                continue
            open_change = ((open_price / prev_close) - 1) * 100
            auction_volume = self._safe_float(row.get("volume"))
            auction_amount = self._safe_float(row.get("amount"))
            volume_ratio = self._resolve_volume_ratio(row, avg_volume_map.get(code, 0.0))

            row_observed = self._verified_observed_at(
                row.get("observed_at") if df.attrs.get("auction_per_row_observed_at") is True else observed_at, trade_date,
            )
            if row_observed is None:
                continue
            auction = AuctionData(
                code=code, trade_date=trade_date,
                auction_time=row_observed.strftime("%H:%M:%S"),
            )
            auction.auction_price = open_price
            auction.auction_volume = int(auction_volume) if auction_volume > 0 else 0
            auction.auction_amount = auction_amount
            auction.prev_close = prev_close
            auction.open_change = open_change
            auction.volume_ratio = volume_ratio
            auction.is_cancelled = False
            auction.observed_at = row_observed
            auction.source_quote_at = local_clock(row.get("source_quote_at"))
            auction.received_at = local_clock(row.get("received_at"))
            for field_name in (
                "source", "source_version", "price_basis", "volume_basis",
                "volume_unit", "amount_unit",
            ):
                value = row.get(field_name)
                setattr(auction, field_name, value if isinstance(value, str) and value else None)
            auction.source_frame_key = auction_source_frame_key(auction)
            pending_frames.setdefault(auction.source_frame_key, {
                column.name: getattr(auction, column.name)
                for column in AuctionData.__table__.columns if column.name != "id"
            })
            count += 1

        # Database uniqueness also resolves concurrent retries without allowing
        # either source to overwrite another frame or any legacy NULL-key row.
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            raise RuntimeError("auction source-frame inserts require SQLite or PostgreSQL")
        values = list(pending_frames.values())
        written = 0
        for offset in range(0, len(values), 100):
            statement = insert(AuctionData).values(values[offset:offset + 100])
            result = await session.execute(
                statement.on_conflict_do_nothing(index_elements=["source_frame_key"])
                .returning(AuctionData.id)
            )
            written += len(result.scalars().all())
        await session.commit()
        logger.info(f"竞价数据保存: {written}条 (候选={count}), {trade_date} {snapshot_time}")
        return written

    async def _resolve_frame_volume_ratios(
        self, session: AsyncSession, candidates: list, trade_date: date,
        *, decision_at: datetime,
    ) -> None:
        """给"提供方没给量比"的候选帧就地补算量比（**复用既有公式**）。

        为什么需要：`auction_evidence_status` 的字段完整性判据要求
        `volume_ratio > 0`。腾讯 `qt` 字段[49]在 09:25 终场窗口实测给不出正量比
        —— 冻结日志里腾讯候选 **100% 因 `volume_ratio` 缺失被拒**
        （9/23 `2995/2996`、9/24 `2994/2995`、9/28 `2995/2995`），
        于是这条来源在 D 闸门的两个帧里贡献恒为 0。

        这里只把**既有函数** `_resolve_volume_ratio`（竞价量/5 日均量×20）
        的结果填进去，与 `save_auction_data` 走同一个实现，不改判据本身：
        量比仍然是"必须为正"才落库，取不到 5 日均量就保持 0、继续被拒。
        单位必须先按 `volume_unit` 换算成股，否则 lot100 会被放大 100 倍。
        """
        pending = [
            candidate for candidate in candidates
            if self._safe_float(getattr(candidate, "volume_ratio", 0.0)) <= 0
            and auction_evidence_status(
                candidate, decision_at=decision_at, require_ratio=False,
            ) == "ok"
        ]
        if not pending:
            return
        avg_volume_map = await self._load_average_daily_volume(
            session, list(dict.fromkeys(item.code for item in pending)), trade_date,
        )
        for candidate in pending:
            candidate.volume_ratio = self._resolve_volume_ratio(
                {
                    "volume_ratio": 0.0,
                    "volume": candidate.auction_volume,
                    "volume_unit": candidate.volume_unit,
                    "volume_basis": candidate.volume_basis,
                },
                avg_volume_map.get(candidate.code, 0.0),
            )

    async def _verified_auction_codes(
        self, session: AsyncSession, trade_date: date, *, decision_at: datetime | None = None,
    ) -> set[str]:
        """当日已有可认证竞价帧的代码集合（只有 ok 状态才算）。

        只作"让出"判据：不可认证的观察行不得以更晚的 auction_time 遮蔽它们。
        判定用同一个 `auction_evidence_status`，不另立口径。

        判定时刻默认取调用方给的本机观测时刻（`collect_auction_data` 在竞价窗口内
        得到的接收时钟），不另取一个更早的时钟 —— 否则刚写入的证据帧会被判成
        `future`，让出判据失效。该函数只在竞价窗口内被调用。
        """
        rows = (await session.scalars(
            select(AuctionData).where(
                AuctionData.trade_date == trade_date,
                AuctionData.price_basis.in_(("indicative_match", "auction_opening")),
            )
        )).all()
        decision = local_clock(decision_at) or datetime.now()
        return {
            str(row.code) for row in rows
            if row.code and auction_evidence_status(row, decision_at=decision) == "ok"
        }

    async def _save_verified_auction_candidates(
        self,
        session: AsyncSession,
        candidates: list,
        observed_at: datetime,
        trade_date: date,
        *,
        source_label: str,
    ) -> dict:
        """逐行自检（策略同一个契约函数），只把 `ok` 的行落库。

        两个来源（腾讯 / 东财）共用这一段，避免出现"两份不同的证据判定"。

        diagnostic 只记录缺失字段计数和有限样例，不填补数据或放宽证据要求。
        """
        accepted: list[dict] = []
        rejected: dict[str, int] = {}
        per_row_diagnoses: list[dict] = []
        for candidate in candidates:
            diagnosis = diagnose_missing_evidence_fields(candidate, decision_at=observed_at)
            status = diagnosis["status"]
            per_row_diagnoses.append(diagnosis)
            if status != "ok":
                rejected[status] = rejected.get(status, 0) + 1
                continue
            accepted.append({
                "code": candidate.code,
                "open": candidate.auction_price,
                "prev_close": candidate.prev_close,
                "volume": candidate.auction_volume,
                "amount": candidate.auction_amount,
                "volume_ratio": candidate.volume_ratio,
                "source": candidate.source,
                "source_version": candidate.source_version,
                "source_quote_at": candidate.source_quote_at,
                "received_at": candidate.received_at,
                "observed_at": candidate.observed_at,
                "price_basis": candidate.price_basis,
                "volume_basis": candidate.volume_basis,
                "volume_unit": candidate.volume_unit,
                "amount_unit": candidate.amount_unit,
            })
        diagnostic = merge_diagnoses(per_row_diagnoses)
        # 标记"诊断跑了哪一轮"——便于运维把多个 09:25 采样区分开
        diagnostic["source_frame"] = observed_at.strftime("%H:%M:%S")
        diagnostic["source_label"] = source_label
        if not accepted:
            logger.warning(
                f"{source_label} 竞价证据自检全部未通过，未写入任何行: "
                f"observed={observed_at.strftime('%H:%M:%S')} "
                f"candidates={len(candidates)} rejected={rejected} "
                f"missing_fields={diagnostic.get('missing_positive_fields')}"
            )
            return {"status": "no_verified_rows", "written": 0,
                    "candidates": len(candidates), "rejected": rejected,
                    "source_frame": observed_at.strftime("%H:%M:%S"),
                    "diagnostic": diagnostic}
        frame = pd.DataFrame(accepted)
        frame.attrs["auction_observed_at"] = max(item["observed_at"] for item in accepted)
        frame.attrs["auction_per_row_observed_at"] = True
        written = await self.save_auction_data(session, frame, trade_date)
        distinct = len({item["source_quote_at"] for item in accepted if item["source_quote_at"]})
        return {
            "status": "ok", "written": written, "accepted": len(accepted),
            "candidates": len(candidates), "rejected": rejected,
            "source_frame": observed_at.strftime("%H:%M:%S"),
            "distinct_source_quote_at": distinct,
            "diagnostic": diagnostic,
        }

    @staticmethod
    def _eastmoney_secid(code: str) -> str:
        code = str(code).zfill(6)
        return ("1." if code[0] in "56" else "0.") + code

    @staticmethod
    def _eastmoney_batches(sorted_codes: list[str]) -> list[list[str]]:
        return [
            sorted_codes[i:i + EASTMONEY_AUCTION_BATCH]
            for i in range(0, len(sorted_codes), EASTMONEY_AUCTION_BATCH)
        ]

    async def _fetch_eastmoney_quotes(
        self,
        codes: list[str],
        *,
        diagnostics: dict | None = None,
        now: datetime | None = None,
        deadline_at: datetime | None = None,
    ) -> dict[str, dict]:
        """按 secid 批量取东财行情；返回 {code: row}。主机按可用性回退。

        `clist/get` 的 `pz` 实测上限 100，全市场要 30~56 页；而
        `ulist.np/get` 支持一次传 300 个 secid（实测 0.27s 返回 300 行），
        所以这里用 ulist 批量取，全市场约 10 个请求 —— 请求数比旧路径低一个量级。

        限频对策（这是本方法的主要复杂度来源）
        --------------------------------------
        1. **批次间加 `EASTMONEY_AUCTION_PACE_SEC` 间隔**，把 10 个请求的突发压平；
        2. **每个批次独立重试**（`EASTMONEY_AUCTION_RETRY` 次，退避递增）。
           旧版是"任一批次抛异常 → 整个主机放弃"，于是被限频时会换主机
           **从第 0 批重新开始**，请求量翻倍、还白扔 30 秒证据窗；
        3. **区分限频与网络故障**：限频（429/5xx/非 JSON 错误页）→ 换主机；
           普通网络故障 → 重试当前主机后跳过该批次，**保留已取到的批次**；
        4. collector传入原终场截止；墙钟/单调钟取较小预算，阻塞请求与退避
           不得延长采证窗，保留已收响应的原钟；取消由调用者正常传播。
        5. 诊断计数写入 `diagnostics`，供次日核对"到底有没有被限频"。

        部分成功也返回：闸门是按 code 统计帧数的，拿到 8 成比拿 0 成好；
        缺口由 `diagnostics["missing_codes"]` 如实记录，不伪装成全量。
        """
        import asyncio as _asyncio
        from time import monotonic

        import httpx

        wanted = {str(code).zfill(6) for code in codes}
        sorted_codes = sorted(wanted)
        batches = self._eastmoney_batches(sorted_codes)
        diag = diagnostics if diagnostics is not None else {}
        diag.update({
            "hosts_tried": [], "host_used": None, "batches": len(batches),
            "attempts": 0, "retries": 0, "rate_limited": False,
            "throttled_batches": 0, "failed_batches": 0, "batch_errors": [],
            "deadline_exhausted": False,
        })
        deadline = local_clock(deadline_at)
        if deadline_at is not None and deadline is None:
            raise ValueError("invalid Eastmoney auction request deadline")
        initial_clock = local_clock(now if now is not None else datetime.now())
        expires = (
            monotonic() + max((deadline - initial_clock).total_seconds(), 0.0)
            if deadline is not None and initial_clock is not None else None
        )
        diag["deadline_at"] = deadline.isoformat() if deadline else None

        def remaining():
            if deadline is None:
                return None
            clock = local_clock(now if now is not None else datetime.now())
            budget = min((deadline - clock).total_seconds(), expires - monotonic()) \
                if clock is not None and expires is not None else 0.0
            if budget <= 0:
                diag["deadline_exhausted"] = True
            return max(budget, 0.0)

        async def pause(seconds):
            budget = remaining()
            if budget is not None and budget <= seconds:
                # No useful next attempt fits after pacing/backoff; do not extend
                # the evidence window or burn it waiting solely to retry too late.
                diag["deadline_exhausted"] = True
                return False
            await _asyncio.sleep(seconds)
            return remaining() != 0

        result: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0),
                                     trust_env=False) as client:
            for host in EASTMONEY_QUOTE_HOSTS:
                if len(result) >= len(wanted) or diag["deadline_exhausted"] or remaining() == 0:
                    break
                diag["hosts_tried"].append(host)
                # 只打**仍有缺口**的批次。旧实现在换主机后从第 0 批重来，
                # 把已取到的批次又取一遍 —— 被限频时正好是双倍加压。
                pending = [
                    (index, [code for code in batch if code not in result])
                    for index, batch in enumerate(batches)
                    if any(code not in result for code in batch)
                ]
                diag.setdefault("batches_by_host", {})[host] = len(pending)
                collected: dict[str, dict] = {}
                host_throttled = False
                for position, (index, batch) in enumerate(pending):
                    if diag["deadline_exhausted"] or remaining() == 0:
                        break
                    if position and not await pause(EASTMONEY_AUCTION_PACE_SEC):
                        break
                    diff = None
                    batch_throttled = False
                    for attempt in range(1, EASTMONEY_AUCTION_RETRY + 1):
                        budget = remaining()
                        if budget == 0 or diag["deadline_exhausted"]:
                            break
                        diag["attempts"] += 1
                        try:
                            request = client.get(
                                f"https://{host}/api/qt/ulist.np/get",
                                params={
                                    "secids": ",".join(self._eastmoney_secid(c) for c in batch),
                                    "fltt": 2, "invt": 2,
                                    "fields": EASTMONEY_AUCTION_FIELDS,
                                },
                                headers={"User-Agent": "Mozilla/5.0",
                                         "Referer": "https://quote.eastmoney.com/"},
                            )
                            response = (
                                await _asyncio.wait_for(request, timeout=budget)
                                if budget is not None else await request
                            )
                            # Stamp the actual response, not the end of all
                            # batches/retries. Explicit now is the existing test clock.
                            received_at = local_clock(now if now is not None else datetime.now())
                            if response.status_code == 429 or response.status_code >= 500:
                                raise EastmoneyThrottled(f"HTTP {response.status_code}")
                            response.raise_for_status()
                            try:
                                payload = response.json()
                            except ValueError as exc:
                                raise EastmoneyThrottled("响应无法解析为JSON（疑似错误页）") from exc
                            if not isinstance(payload, dict):
                                # 正常响应必是 JSON 对象；否则是反爬/错误页
                                raise EastmoneyThrottled("响应不是 JSON 对象（疑似错误页）")
                            diff = ((payload.get("data") or {}).get("diff") or [])
                            break
                        except EastmoneyThrottled as exc:
                            batch_throttled = True
                            diag["rate_limited"] = True
                            diag["retries"] += 1
                            diag["batch_errors"].append(
                                f"{host} batch#{index} 限频/拒服务: {exc}")
                            logger.warning(
                                f"东财竞价行情疑似被限频 {host} batch#{index} "
                                f"(第 {attempt}/{EASTMONEY_AUCTION_RETRY} 次): {exc}")
                            if attempt < EASTMONEY_AUCTION_RETRY and not await pause(
                                EASTMONEY_AUCTION_BACKOFF_SEC * attempt,
                            ):
                                break
                        except Exception as exc:       # noqa: BLE001 — 网络类故障
                            diag["retries"] += 1
                            diag["batch_errors"].append(
                                f"{host} batch#{index} {type(exc).__name__}: {exc}")
                            logger.warning(
                                f"东财竞价行情 {host} batch#{index} 失败 "
                                f"(第 {attempt}/{EASTMONEY_AUCTION_RETRY} 次): "
                                f"{type(exc).__name__}: {exc}")
                            if attempt < EASTMONEY_AUCTION_RETRY and not await pause(
                                EASTMONEY_AUCTION_BACKOFF_SEC * attempt,
                            ):
                                break
                    if diff is None:
                        # 该批次最终失败。限频时换主机（继续硬打只会加重限频），
                        # 其他故障则跳过本批次、继续后面的批次。
                        diag["failed_batches"] += 1
                        if batch_throttled:
                            diag["throttled_batches"] += 1
                            host_throttled = True
                            break
                        continue
                    for row in diff:
                        if not isinstance(row, dict):
                            continue
                        code = str(row.get("f12") or "").zfill(6)
                        if code in batch and code not in result and code not in collected:
                            collected[code] = {
                                **row,
                                "received_at": received_at,
                                "observed_at": local_clock(now if now is not None else datetime.now()),
                            }
                if collected:
                    result.update(collected)
                    diag["host_used"] = host
                    diag["fetched"] = len(result)
                if host_throttled and len(result) < len(wanted):
                    logger.warning(
                        f"东财竞价行情 {host} 被限频，改用下一个主机补缺口"
                        f"（已取 {len(result)}/{len(wanted)}）")
                    continue
                if len(result) >= len(wanted):
                    break
        remaining()  # Include exhaustion during the final response/last attempt.
        diag["missing_codes"] = len(wanted) - len(result)
        return result

    async def collect_eastmoney_auction_evidence(
        self,
        session: AsyncSession,
        trade_date: date | None = None,
        *,
        now: datetime | None = None,
        codes: list[str] | None = None,
    ) -> dict:
        """东财 f124 终场候选帧，独立于腾讯调度。

        f5 为手、f6 为元；fltt=2 的 f17/f18 已是元，不再除以100。
        不同来源不是不同时间帧的替代品：同股同 source_quote_at 仍只计一帧；
        f124 是否推进、能否在原30秒窗口内收到，由真实响应及契约决定。
        """
        target_date = trade_date or date.today()
        precheck = local_clock(now if now is not None else datetime.now())
        if precheck is None or precheck.date() != target_date:
            return {"status": "not_today", "written": 0}
        if not time(9, 25) <= precheck.time() <= time(9, 25, 30):
            # 2026-09-21: 同腾讯，把"前置窗口越界"和"取数耗时越界"区分；
            # reason 让 health 面板能直接看到越界成因，不靠猜。
            return {"status": "outside_evidence_window", "written": 0,
                    "observed_at": precheck.isoformat(),
                    "reason": "前置窗口不在 09:25:00–09:25:30",
                    "window_kind": "precheck"}
        if codes is None:
            code_rows = (await session.execute(
                select(StockTag.code).where(
                    StockTag.board_tag == TAG_TRADEABLE,
                    func.coalesce(StockTag.is_st, False).is_(False),
                    func.coalesce(StockTag.is_suspended, False).is_(False),
                    func.coalesce(StockTag.is_delisting, False).is_(False),
                )
            )).all()
            codes = [str(code) for (code,) in code_rows if code]
        if not codes:
            return {"status": "no_universe", "written": 0}

        diagnostics: dict = {}
        quotes = await self._fetch_eastmoney_quotes(
            list(codes), diagnostics=diagnostics, now=now,
            deadline_at=datetime.combine(target_date, time(9, 25, 30)),
        )
        # 限频必须留痕到 data_quality_guard，而不是只写一行 warning ——
        # 否则"到底是不是被限频"无法在 health 面板上回答（用户此前正是问这个）。
        if diagnostics.get("rate_limited"):
            # 正确路径是 `app.core.data_quality`（`data_quality_guard` 是那里的单例）。
            # 写成 `from app.data import ...` 会 ImportError，而本调用在
            # try/except 里，结果是**限频记账静默失效** —— 已由测试锁住。
            from app.core.data_quality import data_quality_guard

            # 注意：`record_failure(error_msg: str, latency_ms: int = 0)` 内部会做
            # `error_msg[:500]`，传异常对象会 TypeError；`latency_ms=None` 也不是合法入参。
            # 这条路径只在被限频时才走，所以必须显式给 str 和 int。
            await data_quality_guard.record_failure(
                session, "eastmoney", "auction_quote_rate_limit",
                "限频批次={} 失败批次={} 主机={} 明细={}".format(
                    diagnostics.get("throttled_batches"),
                    diagnostics.get("failed_batches"),
                    diagnostics.get("host_used"),
                    "; ".join(diagnostics.get("batch_errors") or [])[:300],
                ),
                latency_ms=0,
            )
        observed_at = local_clock(now if now is not None else datetime.now())
        if observed_at is None:
            return {"status": "invalid_observation_clock", "written": 0}
        # A late trailing response must not erase earlier evidence from this
        # same in-flight collection. Each row retains its genuine response clock;
        # late rows still fail the unchanged evidence gate.
        candidates = []
        for code, row in quotes.items():
            row_observed = local_clock(row.get("observed_at"))
            candidates.append(AuctionData(
                code=code, trade_date=target_date,
                auction_time=row_observed.strftime("%H:%M:%S") if row_observed else "",
                auction_price=self._safe_float(row.get("f17")),
                auction_volume=int(self._safe_float(row.get("f5")) or 0),
                auction_amount=self._safe_float(row.get("f6")),
                prev_close=self._safe_float(row.get("f18")),
                volume_ratio=self._safe_float(row.get("f10")),
                source="eastmoney",
                source_version=EASTMONEY_AUCTION_SOURCE_VERSION,
                source_quote_at=eastmoney_quote_clock(row.get("f124")),
                received_at=local_clock(row.get("received_at")),
                observed_at=row_observed,
                price_basis="auction_opening",
                volume_basis="auction_matched",
                volume_unit="lot100",
                amount_unit="CNY",
            ))
        # 缺量比时与腾讯/新浪共用原公式；缺历史、非法钟或单位仍拒收。
        # 供应商已有正量比保持原值，不用另一来源替换字段。
        await self._resolve_frame_volume_ratios(
            session, candidates, target_date, decision_at=observed_at,
        )
        result = await self._save_verified_auction_candidates(
            session, candidates, observed_at, target_date, source_label="东财",
        )
        result["quotes_fetched"] = len(quotes)
        result["fetch_diagnostics"] = diagnostics
        return result

    async def collect_tencent_auction_evidence(
        self,
        session: AsyncSession,
        trade_date: date | None = None,
        *,
        now: datetime | None = None,
        codes: list[str] | None = None,
    ) -> dict:
        """用**腾讯实时行情**采集 09:25 终场竞价候选帧。

        为什么走腾讯
        ------------
        东财/新浪的 `spot` 接口拿不到"供应商行情时间戳"，采集器只能把
        `source_quote_at` 置空、`price_basis` 标成 `spot_open_unverified`，
        于是 `auction_provenance_v1` 永远判 `unknown`，D 路由闸门永久 blocked
        （实测 3,226 次 `candidate_data_missing`）。
        腾讯 `qt` 字段[30] 提供 provider 行情时间戳，`stock_spot` 实测
        5,224/5,231 行有值，因此可以写出真正可认证的竞价帧。

        契约依据（与 `app/data/auction_evidence.py` 的第二组分支一致）
        --------------------------------------------------------------
        `price_basis="auction_opening"` + `volume_basis="auction_matched"`
        + `source_quote_at.time() >= 09:25`。
        **金融假设（显式为假设，非从公开文档抄来）**：A 股 09:25 集合竞价撮合，
        09:25–09:30 为过渡期、不产生连续竞价成交（本仓库
        `TRADE_SESSIONS` 也把 09:25–09:30 留空，没有对应时段）。
        只有该假设及供应商字段语义成立，今开/累计量才可解释为终场撮合价/量。
        当前 TencentSource 的 amount 实际由 VWAP × 手数 × 100 估算，
        不是供应商原始竞价成交额，不能在此无条件宣称为撮合恒等式。
        若上述假设不成立，本方法产出的帧只是"开盘瞬间快照"，
        不应当作竞价证据；下面的字段/时钟自检不能独立认证该金融语义。

        只写自检通过的行
        ----------------
        每个候选行先构造一个**不加入 session** 的 `AuctionData`，交给
        `auction_evidence_status(row, decision_at=now)` 判定；
        只有 `ok` 才进入落库，其余按状态计数返回。
        共用判据保证字段/时钟检查一致，不等于独立验证供应商金融语义。
        """
        from app.data.sources.tencent_source import TencentSource

        target_date = trade_date or date.today()
        precheck = local_clock(now if now is not None else datetime.now())
        if precheck is None or precheck.date() != target_date:
            return {"status": "not_today", "written": 0}
        # 契约要求三个时钟都落在 09:15:00–09:25:30，且 >=09:25 分支需要
        # source_quote_at.time() >= 09:25，故本地观测窗只能是 09:25:00–09:25:30。
        # 这里只做前置判断以免在窗外发起无谓请求；真正的 observed_at 在取数**之后**再取。
        if not time(9, 25) <= precheck.time() <= time(9, 25, 30):
            # 2026-09-21: 把"前置窗口越界"和"取数耗时越界"统一带上 reason，
            # 让运维在 health 面板上能直接区分两种越界成因，
            # 而不是只看 observed_at 时间点猜。
            return {"status": "outside_evidence_window", "written": 0,
                    "observed_at": precheck.isoformat(),
                    "reason": "前置窗口不在 09:25:00–09:25:30",
                    "window_kind": "precheck"}

        if codes is None:
            code_rows = (await session.execute(
                select(StockTag.code).where(
                    StockTag.board_tag == TAG_TRADEABLE,
                    func.coalesce(StockTag.is_st, False).is_(False),
                    func.coalesce(StockTag.is_suspended, False).is_(False),
                    func.coalesce(StockTag.is_delisting, False).is_(False),
                )
            )).all()
            codes = [str(code) for (code,) in code_rows if code]
        if not codes:
            return {"status": "no_universe", "written": 0}

        records = await TencentSource(
            capture_response_observed_at=True,
        ).collect_spot_batch(list(codes))
        # This is the validation/collection-completion clock, not every row\'s
        # observation. A late tail must not erase earlier valid responses.
        observed_at = local_clock(now if now is not None else datetime.now())
        if observed_at is None:
            return {"status": "invalid_observation_clock", "written": 0}
        candidates = []
        for record in records:
            if not isinstance(record, dict):
                continue
            code = self._normalize_code(record.get("code", ""))
            if not code:
                continue
            # Legacy payloads without this key can only be observed now; never
            # backdate them from receipt/source. Explicit missing clocks stay
            # missing and are rejected by the unchanged evidence contract.
            row_observed = local_clock(record.get("observed_at", observed_at))
            candidates.append(AuctionData(
                code=code, trade_date=target_date,
                auction_time=row_observed.strftime("%H:%M:%S") if row_observed else "",
                auction_price=self._safe_float(record.get("open")),
                auction_volume=int(self._safe_float(record.get("volume")) or 0),
                auction_amount=self._safe_float(record.get("amount")),
                prev_close=self._safe_float(record.get("prev_close")),
                volume_ratio=self._safe_float(record.get("volume_ratio")),
                source="tencent",
                source_version=TENCENT_AUCTION_SOURCE_VERSION,
                source_quote_at=local_clock(record.get("source_quote_at")),
                received_at=local_clock(record.get("received_at")),
                observed_at=row_observed,
                price_basis="auction_opening",
                volume_basis="auction_matched",
                volume_unit="lot100",
                amount_unit="CNY",
            ))
        # 供应商没给量比时按既有公式补算；取不到 5 日均量就保持 0 并被契约拒收，
        # 不做任何"补齐"，也不放宽判据。
        await self._resolve_frame_volume_ratios(
            session, candidates, target_date, decision_at=observed_at,
        )
        result = await self._save_verified_auction_candidates(
            session, candidates, observed_at, target_date, source_label="腾讯",
        )
        result["candidates"] = len(records)
        result["observed_at"] = observed_at.isoformat()
        return result

    async def _fetch_sina_auction_quotes(
        self, codes: list[str], *, diagnostics: dict | None = None,
    ) -> dict[str, dict]:
        """批量取新浪实时行情，**解析出 provider 日期+时间**。

        与 `tencent_source.collect_spot_batch` 的三点不同：
          1. 新浪的源时钟是"日期+时间"两列，必须**两列都在同一天**才算有时钟；
             缺日期时绝不拿本机日期补（旧路径正是因为不补、所以只能判 unknown）；
          2. 早中段虚拟数据取买卖一档位；最新价/累计量为0不能说明档位无数据；
          3. 档位量单位为股，量比由下游既有公式计算，原始时钟逐响应保留。

        解析不了的字段保持缺失（None），由同一个 `auction_evidence_status` 判定，
        不在这一层做任何"补齐"。
        """
        diag = diagnostics if diagnostics is not None else {}
        diag.setdefault("batches", 0)
        diag.setdefault("failed_batches", 0)
        diag.setdefault("batch_errors", [])
        parsed: dict[str, dict] = {}
        pattern = re.compile(r'hq_str_([a-z]{2})(\d{6})="([^"]*)"')
        import httpx
        batches = [codes[i:i + SINA_AUCTION_BATCH] for i in range(0, len(codes), SINA_AUCTION_BATCH)]
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(SINA_AUCTION_TIMEOUT_SEC, connect=3.0),
            # 同腾讯：行情链路不得继承桌面会话的临时代理，否则整批超时。
            trust_env=False,
            headers=SINA_AUCTION_HEADERS,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        ) as client:
            for index, batch in enumerate(batches):
                if index:
                    await asyncio.sleep(SINA_AUCTION_PACE_SEC * index)
                diag["batches"] += 1
                url = SINA_AUCTION_URL + ",".join(self._sina_symbol(code) for code in batch)
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    text = response.text
                except Exception as exc:                  # noqa: BLE001 — 单批失败不弃整轮
                    diag["failed_batches"] += 1
                    diag["batch_errors"].append(f"batch#{index} {type(exc).__name__}")
                    continue
                received_at = local_clock(datetime.now())
                for match in pattern.finditer(text):
                    code = str(match.group(2))
                    fields = match.group(3).split(",")
                    row: dict = {"code": code, "received_at": received_at}
                    for key, position in _SINA_LIST_FIELDS.items():
                        row[key] = fields[position] if len(fields) > position else ""
                    row["observed_at"] = local_clock(datetime.now())
                    parsed[code] = row
        return parsed

    @staticmethod
    def _sina_symbol(code: str) -> str:
        """新浪代码前缀：与腾讯一致，688 归沪市。"""
        text = str(code)
        if text.startswith("68"):
            return "sh" + text
        return {"6": "sh", "0": "sz", "3": "sz", "4": "bj", "8": "bj"}.get(text[0], "sz") + text

    @staticmethod
    def _sina_source_clock(row: dict, trade_date: date) -> datetime | None:
        """只接受"日期列 == 当日 + 时间列可解析"的源时钟。

        新浪返回独立日期列；缺失日期时**不允许**用本机日期补造。
        日期不匹配（例如盘前仍是上一交易日的 15:xx 快照）时保持
        缺失，由证据契约判 `unknown`，绝不伪造源钟。
        """
        raw_date = str(row.get("date") or "").strip()
        raw_time = str(row.get("time") or "").strip()
        if not raw_date or not raw_time:
            return None
        try:
            clock = datetime.strptime(f"{raw_date} {raw_time}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return clock if clock.date() == trade_date else None

    async def collect_tencent_auction_early_evidence(
        self,
        session: AsyncSession,
        trade_date: date | None = None,
        *,
        now: datetime | None = None,
        codes: list[str] | None = None,
    ) -> dict:
        """腾讯早中段竞价帧（09:15:00–09:25:00）—— 虚拟匹配价 + 虚拟匹配量。

        腾讯与新浪冻结样本均在买卖一档位提供早中段虚拟价/量；
        最新价或累计量为0不代表档位无数据。两源各自验证，不互相补字段。

        契约落点（`auction_provenance_v1` 的第一组分支）
        ----------------------------------------------
        `price_basis="indicative_match"` + `volume_basis="indicative_matched"`
        + `source_quote_at.time() < 09:25`。

        金融假设（显式为假设）
        ----------------------
        * 竞价期买一价 == 卖一价 == 交易所发布的**虚拟开盘参考价**（实测 10/10 只恒等）；
        * 竞价期买一量/卖一量 == **虚拟匹配量**（手）；双边必须正值且一致。
          两侧不等时拒收并计入诊断，不用较低价格/数量冒充认证。
        * 竞价期 field[6] 成交量/field[37] 成交额/field[49] 量比均为 0：
          成交额按"虚拟参考价 × 匹配量"换算（同项目既有 `avg_price × 手数 × 100` 口径），
          量比走既有 `_resolve_frame_volume_ratios`（5 日均量为分母），
          取不到均量就保持 0 并被契约拒收 —— 不补齐、不放宽。
        """
        from app.data.sources.tencent_source import TencentSource

        target_date = trade_date or date.today()
        precheck = local_clock(now if now is not None else datetime.now())
        if precheck is None or precheck.date() != target_date:
            return {"status": "not_today", "written": 0}
        if not time(9, 15) <= precheck.time() < time(9, 25):
            return {"status": "outside_evidence_window", "written": 0,
                    "observed_at": precheck.isoformat(),
                    "reason": "前置窗口不在 09:15:00–09:25:00",
                    "window_kind": "precheck"}

        if codes is None:
            code_rows = (await session.execute(
                select(StockTag.code).where(
                    StockTag.board_tag == TAG_TRADEABLE,
                    func.coalesce(StockTag.is_st, False).is_(False),
                    func.coalesce(StockTag.is_suspended, False).is_(False),
                    func.coalesce(StockTag.is_delisting, False).is_(False),
                )
            )).all()
            codes = [str(code) for (code,) in code_rows if code]
        if not codes:
            return {"status": "no_universe", "written": 0}

        records = await TencentSource(capture_response_observed_at=True).collect_spot_batch(list(codes))
        observed_at = local_clock(now if now is not None else datetime.now())
        if observed_at is None:
            return {"status": "invalid_observation_clock", "written": 0}

        candidates = []
        level1_unequal = 0
        level1_missing = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            code = self._normalize_code(record.get("code", ""))
            if not code:
                continue
            source_clock = local_clock(record.get("source_quote_at"))
            if source_clock is None or not time(9, 15) <= source_clock.time() < time(9, 25):
                # 终场（>=09:25）帧由 `collect_tencent_auction_evidence` 负责。
                continue
            bid_price = self._safe_float(record.get("bid1_price"))
            ask_price = self._safe_float(record.get("ask1_price"))
            bid_volume = self._safe_float(record.get("bid1_volume"))
            ask_volume = self._safe_float(record.get("ask1_volume"))
            if bid_price <= 0 or ask_price <= 0 or bid_volume <= 0 or ask_volume <= 0:
                level1_missing += 1
                continue
            if bid_volume != ask_volume or bid_price != ask_price:
                # 两侧不一致无法认证虚拟撮合语义，取 min 并不保守。
                level1_unequal += 1
                continue
            indicative_price = bid_price
            matched_hands = bid_volume
            row_observed = local_clock(record.get("observed_at"))
            candidates.append(AuctionData(
                code=code, trade_date=target_date,
                auction_time=row_observed.strftime("%H:%M:%S") if row_observed else "",
                auction_price=indicative_price,
                auction_volume=int(matched_hands),
                # 竞价期没有成交额字段：按虚拟参考价 × 匹配手数 × 100 股换算，
                # 与项目既有 spot 口径一致（TencentSource 也是这么算 amount 的）。
                auction_amount=round(indicative_price * matched_hands * 100, 2),
                prev_close=self._safe_float(record.get("prev_close")),
                volume_ratio=self._safe_float(record.get("volume_ratio")),
                source="tencent",
                source_version=TENCENT_AUCTION_EARLY_SOURCE_VERSION,
                source_quote_at=source_clock,
                received_at=local_clock(record.get("received_at")),
                observed_at=row_observed,
                price_basis="indicative_match",
                volume_basis="indicative_matched",
                volume_unit="lot100",
                amount_unit="CNY",
            ))
        await self._resolve_frame_volume_ratios(
            session, candidates, target_date, decision_at=observed_at,
        )
        result = await self._save_verified_auction_candidates(
            session, candidates, observed_at, target_date, source_label="腾讯早中段",
        )
        result["candidates"] = len(candidates)
        result["level1_unequal_or_missing"] = level1_unequal
        result["level1_incomplete"] = level1_missing
        result["observed_at"] = observed_at.isoformat()
        return result

    async def collect_sina_auction_evidence(
        self,
        session: AsyncSession,
        trade_date: date | None = None,
        *,
        now: datetime | None = None,
        codes: list[str] | None = None,
    ) -> dict:
        """新浪 `hq.sinajs.cn` 早/中段竞价帧（09:15:00–09:25:30）。

        补的是什么缺口
        --------------
        D2 的判据是三段：09:20 前、09:20–09:25 不可撤单段、09:25 后终场。
        `auction_data` 里 09:25 后的可认证帧由腾讯/东财负责，但**早中段此前只有
        新浪行情中心的观察帧：source_quote_at 为空、volume 为 0、status 恒 unknown**，
        于是 `verified_early` 与 `cancel_phase_positive_samples` 永远为空，
        D2 每天只能发一条 MARKET coverage_blocked。

        本方法用带源时钟的新浪实时行情补这一段。契约与两条既有路径**完全一致**：
        逐行用 `auction_evidence_status(decision_at=观测时刻)` 自检，只写 `ok` 的行。

        金融假设（显式为假设，非从公开文档抄来）
        ----------------------------------------
        * 竞价期双边一致的买卖一价/量解释为虚拟参考价/匹配股数，
          而非最新价[3]/累计成交量[8]。金额为价×股数的派生值，不冒充实成交额。
        * 双边缺失、非正或不一致一律拒收；原三时钟与量比判据不变。
          冻结小样本只能验证字段映射，全市场实际覆盖仍待自然时段验收。
        """
        target_date = trade_date or date.today()
        precheck = local_clock(now if now is not None else datetime.now())
        if precheck is None or precheck.date() != target_date:
            return {"status": "not_today", "written": 0}
        if not time(9, 15) <= precheck.time() <= time(9, 25, 30):
            return {"status": "outside_evidence_window", "written": 0,
                    "observed_at": precheck.isoformat(),
                    "reason": "前置窗口不在 09:15:00–09:25:30",
                    "window_kind": "precheck"}

        if codes is None:
            code_rows = (await session.execute(
                select(StockTag.code).where(
                    StockTag.board_tag == TAG_TRADEABLE,
                    func.coalesce(StockTag.is_st, False).is_(False),
                    func.coalesce(StockTag.is_suspended, False).is_(False),
                    func.coalesce(StockTag.is_delisting, False).is_(False),
                )
            )).all()
            codes = [str(code) for (code,) in code_rows if code]
        if not codes:
            return {"status": "no_universe", "written": 0}

        diagnostics: dict = {}
        quotes = await self._fetch_sina_auction_quotes(list(codes), diagnostics=diagnostics)
        observed_at = local_clock(now if now is not None else datetime.now())
        if observed_at is None:
            return {"status": "invalid_observation_clock", "written": 0}

        # 只有本段（<09:25）的帧才由本路径负责；>=09:25 的终场帧必须留给
        # 腾讯/东财路径，否则低可信帧会把已验证帧从"最新帧"里顶掉。
        wanted_codes = {str(code) for code in codes}
        candidates = []
        missing_clock = 0
        level1_unequal = 0
        level1_missing = 0
        for code, row in quotes.items():
            if code not in wanted_codes:
                continue
            source_clock = self._sina_source_clock(row, target_date)
            if source_clock is None:
                missing_clock += 1
                continue
            if source_clock.time() >= time(9, 25):
                continue
            # 竞价期必须取档位：最新价[3]/累计量[8] 在 09:25 前是 0。
            bid_price = self._safe_float(row.get("bid_price"))
            ask_price = self._safe_float(row.get("ask_price"))
            bid_volume = self._safe_float(row.get("bid_volume"))
            ask_volume = self._safe_float(row.get("ask_volume"))
            if bid_price <= 0 or ask_price <= 0 or bid_volume <= 0 or ask_volume <= 0:
                level1_missing += 1
                continue
            if bid_volume != ask_volume or bid_price != ask_price:
                level1_unequal += 1
                continue
            indicative_price = bid_price
            matched_shares = bid_volume
            row_observed = local_clock(row.get("observed_at"))
            candidates.append(AuctionData(
                code=code, trade_date=target_date,
                auction_time=row_observed.strftime("%H:%M:%S") if row_observed else "",
                auction_price=indicative_price,
                # 新浪档位量单位是**股**（实测 = 腾讯手数 × 100）。
                auction_volume=int(matched_shares),
                auction_amount=round(indicative_price * matched_shares, 2),
                prev_close=self._safe_float(row.get("prev_close")),
                volume_ratio=0.0,
                source="sina",
                source_version=SINA_AUCTION_SOURCE_VERSION,
                source_quote_at=source_clock,
                received_at=local_clock(row.get("received_at")),
                observed_at=row_observed,
                price_basis="indicative_match",
                volume_basis="indicative_matched",
                volume_unit="share",
                amount_unit="CNY",
            ))
        # 新浪不给量比：与腾讯终场帧共用同一段既有公式补算，不另立口径。
        await self._resolve_frame_volume_ratios(
            session, candidates, target_date, decision_at=observed_at,
        )
        result = await self._save_verified_auction_candidates(
            session, candidates, observed_at, target_date, source_label="新浪早中段",
        )
        result["candidates"] = len(candidates)
        result["missing_source_clock"] = missing_clock
        result["level1_unequal_or_missing"] = level1_unequal
        result["level1_incomplete"] = level1_missing
        result["fetch_diagnostics"] = diagnostics
        return result

    async def get_snapshot_health(
        self,
        session: AsyncSession,
        trade_date: date,
        *,
        as_of_at: datetime | None = None,
    ) -> dict:
        """检查每只股票最终竞价帧，严格限制在同一交易日内。

        竞价时间每天都会重复。如果外层查询只按 code + auction_time 回连，
        会把历史交易日同一时刻的记录一起算入，既夸大覆盖数，也会借历史完整
        字段把当天的价格快照误判为可执行信号。
        """
        result = await session.execute(
            select(AuctionData).where(AuctionData.id.in_(latest_auction_ids(trade_date)))
        )
        rows = list(result.scalars().all())
        all_rows = list(
            (
                await session.scalars(
                    select(AuctionData).where(
                        AuctionData.trade_date == trade_date,
                        AuctionData.auction_time.between("09:15:00", "09:25:30"),
                    )
                )
            ).all()
        )
        tagged_tradeable_codes = {
            str(code or "")
            for code in (
                await session.scalars(
                    select(StockTag.code).where(
                        StockTag.board_tag == TAG_TRADEABLE,
                        func.coalesce(StockTag.is_st, False).is_(False),
                        func.coalesce(StockTag.is_suspended, False).is_(False),
                        func.coalesce(StockTag.is_delisting, False).is_(False),
                    )
                )
            ).all()
            if code
        }
        if tagged_tradeable_codes:
            scoped_rows = [row for row in rows if row.code in tagged_tradeable_codes]
            scoped_all_rows = [row for row in all_rows if row.code in tagged_tradeable_codes]
            coverage_scope = "tradeable_stock_tags"
        else:
            # 轻量测试库或首次启动可能尚无标签；仍只按代码规则保留生产主板。
            scoped_rows = [row for row in rows if stock_tagger.is_tradeable(row.code)]
            scoped_all_rows = [row for row in all_rows if stock_tagger.is_tradeable(row.code)]
            coverage_scope = "main_board_code_fallback"

        decision_at = as_of_at if as_of_at is not None else datetime.now()
        evidence_counts: dict[str, int] = {}
        for row in scoped_rows:
            status = auction_evidence_status(row, decision_at=decision_at)
            evidence_counts[status] = evidence_counts.get(status, 0) + 1
        feed_complete_count = evidence_counts.get("ok", 0)
        max_verified_observed_at = max((
            local_clock(row.observed_at) for row in scoped_rows
            if auction_evidence_status(row, decision_at=decision_at) == "ok"
        ), default=None)
        timely_snapshot_count = sum(
            1
            for row in scoped_rows
            if "09:24:00" <= str(row.auction_time or "") <= "09:25:30"
        )
        source_timely_count = sum(
            1 for row in scoped_rows
            if auction_evidence_status(row, decision_at=decision_at) == "ok"
            and local_clock(row.source_quote_at).time() >= time(9, 24)
        )
        positive_frame_times: dict[str, set[str]] = {}
        for row in scoped_all_rows:
            if auction_evidence_status(row, decision_at=decision_at) == "ok":
                # Re-fetching the same provider update is not another evidence frame.
                positive_frame_times.setdefault(str(row.code), set()).add(
                    local_clock(row.source_quote_at).isoformat()
                )
        minimum_positive_frames = 2
        latest_complete_codes = {
            str(row.code)
            for row in scoped_rows
            if auction_evidence_status(row, decision_at=decision_at) == "ok"
        }
        multi_frame_complete_count = sum(
            1
            for code in latest_complete_codes
            if len(positive_frame_times.get(code, set())) >= minimum_positive_frames
        )
        scoped_count = len(scoped_rows)
        complete_ratio = feed_complete_count / scoped_count if scoped_count else 0.0
        multi_frame_ratio = multi_frame_complete_count / scoped_count if scoped_count else 0.0
        timely_ratio = timely_snapshot_count / scoped_count if scoped_count else 0.0
        source_timely_ratio = source_timely_count / scoped_count if scoped_count else 0.0
        minimum_ratio = max(float(settings.DATA_COMPLETENESS_MIN), 0.0)
        missing = not scoped_rows
        stale = bool(scoped_rows and (
            timely_ratio < minimum_ratio
            or (feed_complete_count > 0 and source_timely_count / feed_complete_count < minimum_ratio)
        ))
        field_degraded = bool(scoped_rows and complete_ratio < minimum_ratio)
        path_degraded = bool(scoped_rows and multi_frame_ratio < minimum_ratio)
        degraded = bool(field_degraded or stale or path_degraded)
        latest_snapshot_time = max(
            (str(row.auction_time or "") for row in scoped_rows),
            default="",
        )
        return {
            "trade_date": str(trade_date),
            "snapshot_count": scoped_count,
            "raw_snapshot_count": len(rows),
            "latest_code_count": scoped_count,
            "coverage_scope": coverage_scope,
            "tradeable_universe_count": len(tagged_tradeable_codes) or scoped_count,
            "latest_snapshot_time": latest_snapshot_time,
            "timely_snapshot_count": timely_snapshot_count,
            "timely_snapshot_ratio": round(timely_ratio, 4),
            "minimum_timely_ratio": minimum_ratio,
            "verified_timely_snapshot_count": source_timely_count,
            "verified_timely_snapshot_ratio": round(source_timely_ratio, 4),
            "evidence_contract": "auction_provenance_v1",
            "evidence_status_counts": evidence_counts,
            "max_verified_observed_at": max_verified_observed_at.isoformat() if max_verified_observed_at else None,
            "historical_unknown_not_backfilled": True,
            "feed_complete_count": feed_complete_count,
            "feed_complete_ratio": round(complete_ratio, 4),
            "minimum_complete_ratio": minimum_ratio,
            "minimum_positive_volume_frames": minimum_positive_frames,
            "multi_frame_complete_count": multi_frame_complete_count,
            "multi_frame_complete_ratio": round(multi_frame_ratio, 4),
            "path_degraded": path_degraded,
            "missing": missing,
            "stale": stale,
            "field_degraded": field_degraded,
            "degraded": degraded,
            "status": (
                "missing"
                if missing
                else "stale"
                if stale
                else "degraded"
                if field_degraded or path_degraded
                else "ok"
            ),
        }

    async def ensure_auction_data_snapshot(
        self,
        session: AsyncSession,
        trade_date: date,
        *,
        auction_time: str = "09:25:00",
    ) -> dict:
        """确保某交易日存在可回放的竞价快照.

        调度快照依赖 auction_data 生成“竞价高开强攻”路线。仅在当日真实
        竞价窗口采集；漏采后禁止用 stock_spot 或盘后累计量额回填竞价。
        字段不全时明确返回 degraded，并把路线限制为预测观察。
        """
        before_health = await self.get_snapshot_health(session, trade_date)
        can_refresh = self._is_live_auction_window(trade_date)
        if not before_health.get("missing") and (
            not before_health.get("degraded") or not can_refresh
        ):
            return {
                "status": "degraded" if before_health.get("degraded") else "exists",
                **before_health,
                "saved": 0,
            }

        df = await self.collect_auction_data(session, trade_date)
        if df.empty:
            logger.warning(f"竞价快照确保失败: 无可用行情快照 {trade_date}")
            return {
                "status": "no_data" if before_health.get("missing") else "degraded",
                **before_health,
                "saved": 0,
            }

        saved = await self.save_auction_data(session, df, trade_date, auction_time=auction_time)
        after_health = await self.get_snapshot_health(session, trade_date)
        status = (
            "degraded"
            if after_health.get("degraded")
            else "refreshed"
            if not before_health.get("missing")
            else "created"
            if saved
            else "no_data"
        )
        return {
            "status": status,
            **after_health,
            "saved": saved,
            "auction_time": auction_time,
        }


# ========== 竞价分析器 ==========

class AuctionAnalyzer:
    """竞价分析器 — 识别竞价异动，计算竞价因子"""

    # 竞价异动阈值
    HIGH_OPEN_THRESHOLD = 3.0       # 高开>3%
    ULTRA_HIGH_OPEN_THRESHOLD = 7.0  # 超高开>7%
    LOW_OPEN_THRESHOLD = -3.0       # 低开<-3%
    VOLUME_RATIO_THRESHOLD = 3.0    # 量比>3
    LIMIT_UP_THRESHOLD = 9.5        # 涨停附近

    async def analyze(self, session: AsyncSession,
                       trade_date: date = None) -> list[AuctionSignal]:
        """分析当日竞价数据，返回竞价信号列表"""
        trade_date = await resolve_latest_trade_date(
            session,
            AuctionData.trade_date,
            requested=trade_date,
        )

        # Shared latest-per-code selection prevents same-second source ties
        # from inflating coverage or choosing an arbitrary older row.
        result = await session.execute(
            select(AuctionData).where(AuctionData.id.in_(latest_auction_ids(trade_date)))
        )
        auction_records = result.scalars().all()

        if not auction_records:
            logger.warning(f"无竞价数据: {trade_date}")
            return []

        latest_by_code = {record.code: record for record in auction_records}

        signals = []
        for record in latest_by_code.values():
            signal = self._analyze_single(record)
            if signal.strength_score >= 30:  # 只返回有一定强度的
                signals.append(signal)

        # 按强度排序
        signals.sort(key=lambda s: s.strength_score, reverse=True)

        logger.info(f"竞价分析完成: {len(signals)}个异动信号, {trade_date}")
        return signals

    @staticmethod
    def _finite_factor(value) -> float:
        return AuctionCollector._safe_float(value, 0.0)

    def _analyze_single(self, record: AuctionData) -> AuctionSignal:
        """分析单只股票竞价数据"""
        anomalies = []
        score = 0

        open_change = self._finite_factor(record.open_change)
        volume_ratio = self._finite_factor(record.volume_ratio)
        evidence_status = auction_evidence_status(record)

        # 高开
        if open_change >= self.ULTRA_HIGH_OPEN_THRESHOLD:
            anomalies.append(AuctionAnomaly.ULTRA_HIGH_OPEN)
            score += 40
        elif open_change >= self.HIGH_OPEN_THRESHOLD:
            anomalies.append(AuctionAnomaly.HIGH_OPEN)
            score += 25

        # 低开
        if open_change <= self.LOW_OPEN_THRESHOLD:
            anomalies.append(AuctionAnomaly.LOW_OPEN)
            score -= 10

        # 量比
        if volume_ratio >= self.VOLUME_RATIO_THRESHOLD:
            anomalies.append(AuctionAnomaly.VOLUME_SPIKE)
            score += 20

        # 涨停价挂单
        if open_change >= self.LIMIT_UP_THRESHOLD:
            anomalies.append(AuctionAnomaly.LIMIT_UP_BID)
            score += 30

        # 撤单嫌疑
        if record.is_cancelled:
            anomalies.append(AuctionAnomaly.CANCEL_SUSPICION)
            score -= 15

        # 量价共振加分
        if open_change > 0 and volume_ratio > 2:
            score += 10

        score = max(0, min(100, score))

        return AuctionSignal(
            code=record.code,
            trade_date=record.trade_date,
            auction_price=record.auction_price,
            prev_close=record.prev_close,
            open_change=round(open_change, 2),
            volume_ratio=round(volume_ratio, 2),
            auction_amount=record.auction_amount or 0,
            anomalies=anomalies,
            strength_score=score,
            is_tradeable=stock_tagger.is_tradeable(record.code) and evidence_status == "ok",
            evidence_status=evidence_status,
        )

    async def get_auction_factors(self, code: str,
                                   session: AsyncSession,
                                   trade_date: date = None) -> dict:
        """获取个股竞价因子(供因子引擎使用)"""
        trade_date = await resolve_latest_trade_date(
            session,
            AuctionData.trade_date,
            requested=trade_date,
        )

        result = await session.execute(
            select(AuctionData).where(
                AuctionData.code == code,
                AuctionData.trade_date == trade_date,
                AuctionData.auction_time.between("09:15:00", "09:25:30"),
            ).order_by(*auction_latest_order()).limit(1)
        )
        record = result.scalar_one_or_none()

        status = auction_evidence_status(record)
        return {
            "auction_volume_ratio": self._finite_factor(record.volume_ratio) if status == "ok" else None,
            "auction_open_change": self._finite_factor(record.open_change) if status == "ok" else None,
            "auction_evidence_status": status,
            "auction_feed_complete": status == "ok",
        }

    async def get_strong_auctions(self, session: AsyncSession,
                                   trade_date: date = None,
                                   min_score: int = 50,
                                   tradeable_only: bool = True) -> list[AuctionSignal]:
        """获取竞价强势股"""
        signals = await self.analyze(session, trade_date)
        filtered = [s for s in signals if s.strength_score >= min_score]

        if tradeable_only:
            filtered = [s for s in filtered if s.is_tradeable]

        return filtered


# ========== 竞价调度 ==========

class AuctionScheduler:
    """竞价分析调度器 — 在9:15-9:25期间定时采集"""

    # 竞价阶段
    PHASE_1_START = time(9, 15)  # 可撤单阶段
    PHASE_2_START = time(9, 20)  # 不可撤单阶段
    PHASE_END = time(9, 25)      # 竞价结束

    async def run_auction_phase(
        self,
        session: AsyncSession,
        trade_date: date = None,
        auction_time: str | None = None,
    ) -> dict:
        """执行一个竞价周期(完整采集+分析)"""
        collector = AuctionCollector()
        analyzer = AuctionAnalyzer()

        # 1. 采集竞价数据
        df = await collector.collect_auction_data(session, trade_date)

        if df.empty:
            return {"status": "no_data", "signals": []}

        # 2. 保存
        trade_date = trade_date or date.today()
        saved = await collector.save_auction_data(session, df, trade_date, auction_time=auction_time)

        health = await collector.get_snapshot_health(session, trade_date)

        # 3. 分析
        signals = await analyzer.analyze(session, trade_date)

        # 4. 统计
        tradeable_signals = [s for s in signals if s.is_tradeable]
        high_open_count = sum(1 for s in signals if s.open_change > 3)
        limit_up_bid_count = sum(1 for s in signals if AuctionAnomaly.LIMIT_UP_BID in s.anomalies)

        result = {
            "status": "degraded" if health.get("degraded") else "ok",
            "trade_date": str(trade_date),
            "saved": saved,
            "auction_health": health,
            "total_signals": len(signals),
            "tradeable_signals": len(tradeable_signals),
            "high_open_count": high_open_count,
            "limit_up_bid_count": limit_up_bid_count,
            "top_signals": [
                {
                    "code": s.code,
                    "name": s.name,
                    "open_change": s.open_change,
                    "volume_ratio": s.volume_ratio,
                    "strength_score": s.strength_score,
                    "anomalies": [a.value for a in s.anomalies],
                    "tag": stock_tagger.get_tag_label(s.code),
                }
                for s in tradeable_signals[:20]
            ],
        }

        logger.info(
            f"竞价分析完成: {len(signals)}异动, {high_open_count}高开, "
            f"{limit_up_bid_count}涨停竞价"
        )

        return result


# 全局
auction_collector = AuctionCollector()
auction_analyzer = AuctionAnalyzer()
auction_scheduler = AuctionScheduler()
