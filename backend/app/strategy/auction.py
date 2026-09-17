"""竞价分析模块 — 9:15-9:25集合竞价异动识别

核心能力:
- 竞价数据采集(AkShare接口)
- 竞价异动识别(高开/量比/撤单)
- 竞价因子计算(auction_strength/auction_volume_ratio/auction_open_change)
- 竞价强势股筛选
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.data.auction_evidence import auction_evidence_status, volume_in_shares
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

# 腾讯竞价帧的来源标识：与东财/新浪的 `*_unverified` 明确区分，
# 因为只有这条路径带 provider 行情时间戳、能通过 auction_provenance_v1。
TENCENT_AUCTION_SOURCE_VERSION = "tencent_qt_auction_open_v1"
# 东财行情：`f124` 是**行情更新时间戳**，本项目已在
# `app/data/fund_flow_clock.eastmoney_quote_clock` 里解码并信任
# （见 `main_fund.py` 的 `individual_fund_flow_v3_f124`）。
# 主域名在本机长期拒连（与 eastmoney/market_fund_flow 的 chronic 失败同源），
# 因此按可用性顺序回退；实测 push2delay 可用且字段齐全。
EASTMONEY_AUCTION_SOURCE_VERSION = "eastmoney_qt_auction_open_v1"
EASTMONEY_QUOTE_HOSTS = ("push2delay.eastmoney.com", "push2.eastmoney.com")
EASTMONEY_AUCTION_BATCH = 300
EASTMONEY_AUCTION_FIELDS = "f12,f14,f17,f18,f5,f6,f10,f124"


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
        """采集竞价数据(AkShare接口)

        AkShare实时接口:
        - ak.stock_zh_a_spot_em()  东财主源(含量比)
        - ak.stock_zh_a_spot()     新浪观察兜底(不冒充竞价虚拟匹配量)
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
            ("eastmoney", ak.stock_zh_a_spot_em),
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
        result = await session.execute(
            select(StockKline.code, StockKline.volume)
            .where(
                StockKline.code.in_(codes),
                StockKline.trade_date < trade_date,
            )
            .order_by(StockKline.code, desc(StockKline.trade_date))
        )
        grouped: dict[str, list[float]] = {}
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

        codes = list(dict.fromkeys(code for code, _row in normalized_rows))
        avg_volume_map = await self._load_average_daily_volume(session, codes, trade_date)
        existing_result = await session.execute(
            select(AuctionData).where(
                AuctionData.trade_date == trade_date,
                AuctionData.auction_time == snapshot_time,
                AuctionData.code.in_(codes),
            )
        )
        existing = {item.code: item for item in existing_result.scalars().all()}

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

            auction = existing.get(code)
            if auction is None:
                auction = AuctionData(
                    code=code,
                    trade_date=trade_date,
                    auction_time=snapshot_time,
                )
                session.add(auction)
                existing[code] = auction

            auction.auction_price = open_price
            auction.auction_volume = int(auction_volume) if auction_volume > 0 else 0
            auction.auction_amount = auction_amount
            auction.prev_close = prev_close
            auction.open_change = open_change
            auction.volume_ratio = volume_ratio
            auction.is_cancelled = False
            auction.observed_at = observed_at
            auction.source_quote_at = local_clock(row.get("source_quote_at"))
            auction.received_at = local_clock(row.get("received_at"))
            for field_name in (
                "source", "source_version", "price_basis", "volume_basis",
                "volume_unit", "amount_unit",
            ):
                value = row.get(field_name)
                setattr(auction, field_name, value if isinstance(value, str) and value else None)
            count += 1

        await session.commit()
        logger.info(f"竞价数据保存: {count}条, {trade_date} {snapshot_time}")
        return count

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
        """
        accepted: list[dict] = []
        rejected: dict[str, int] = {}
        for candidate in candidates:
            status = auction_evidence_status(candidate, decision_at=observed_at)
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
                "price_basis": candidate.price_basis,
                "volume_basis": candidate.volume_basis,
                "volume_unit": candidate.volume_unit,
                "amount_unit": candidate.amount_unit,
            })
        if not accepted:
            logger.warning(
                f"{source_label} 竞价证据自检全部未通过，未写入任何行: "
                f"observed={observed_at.strftime('%H:%M:%S')} "
                f"candidates={len(candidates)} rejected={rejected}"
            )
            return {"status": "no_verified_rows", "written": 0,
                    "candidates": len(candidates), "rejected": rejected,
                    "source_frame": observed_at.strftime("%H:%M:%S")}
        frame = pd.DataFrame(accepted)
        frame.attrs["auction_observed_at"] = observed_at
        written = await self.save_auction_data(session, frame, trade_date)
        distinct = len({item["source_quote_at"] for item in accepted if item["source_quote_at"]})
        return {
            "status": "ok", "written": written, "accepted": len(accepted),
            "candidates": len(candidates), "rejected": rejected,
            "source_frame": observed_at.strftime("%H:%M:%S"),
            "distinct_source_quote_at": distinct,
        }

    @staticmethod
    def _eastmoney_secid(code: str) -> str:
        code = str(code).zfill(6)
        return ("1." if code[0] in "56" else "0.") + code

    async def _fetch_eastmoney_quotes(self, codes: list[str]) -> dict[str, dict]:
        """按 secid 批量取东财行情；返回 {code: row}。主机按可用性回退。

        `clist/get` 的 `pz` 实测上限 100，全市场要 56 页；而
        `ulist.np/get` 支持一次传 300 个 secid（实测 0.27s 返回 300 行），
        所以这里用 ulist 批量取，全市场约 10 个请求。
        """
        import httpx

        wanted = {str(code).zfill(6) for code in codes}
        result: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0),
                                     trust_env=False) as client:
            for host in EASTMONEY_QUOTE_HOSTS:
                batches = [
                    [c for c in sorted(wanted)][i:i + EASTMONEY_AUCTION_BATCH]
                    for i in range(0, len(wanted), EASTMONEY_AUCTION_BATCH)
                ]
                collected: dict[str, dict] = {}
                try:
                    for batch in batches:
                        response = await client.get(
                            f"https://{host}/api/qt/ulist.np/get",
                            params={
                                "secids": ",".join(self._eastmoney_secid(c) for c in batch),
                                "fltt": 2, "invt": 2,
                                "fields": EASTMONEY_AUCTION_FIELDS,
                            },
                            headers={"User-Agent": "Mozilla/5.0",
                                     "Referer": "https://quote.eastmoney.com/"},
                        )
                        response.raise_for_status()
                        diff = ((response.json().get("data") or {}).get("diff") or [])
                        for row in diff:
                            code = str(row.get("f12") or "").zfill(6)
                            if code in wanted:
                                collected[code] = row
                    if collected:
                        return collected
                except Exception as exc:           # noqa: BLE001 — 换主机重试
                    logger.warning(f"东财竞价行情 {host} 失败: {type(exc).__name__}: {exc}")
                    continue
        return result

    async def collect_eastmoney_auction_evidence(
        self,
        session: AsyncSession,
        trade_date: date | None = None,
        *,
        now: datetime | None = None,
        codes: list[str] | None = None,
    ) -> dict:
        """用东财行情做**第二来源**的 09:25 终场竞价帧。

        为什么需要第二个来源
        --------------------
        闸门分子是 `multi_frame_complete_count`，要求每只标的有
        **>=2 个不同 `source_quote_at` 的 ok 帧**（`minimum_positive_frames=2`）。
        而 09:25–09:30 是无成交窗口：
          * 实测腾讯 field[30] 在报价不变时**不推进**
            （盘后连采 3 轮、间隔 8 秒，全部钉在 16:14:xx）；
          * `<09:25` 的那条分支（`indicative_match/indicative_matched`）实测不可用：
            9/14–9/17 全部 09:15–09:24 帧的 `auction_volume` **100% 为 0**。
        两条路都堵住时，同一来源在 30 秒窗口里只能给 1 帧。
        东财 `f124` 是按代码的行情更新时间戳（实测 300 只里有 42~51 个不同值），
        与腾讯是两个独立来源，其时间戳通常与腾讯不同 →
        一轮采样即可凑出 2 个不同 `source_quote_at`。

        单位：东财 `f5` 为**手**（实测 `f6/(f5*100)` 与均价吻合，
        且 600000 的 f5=456711 与腾讯的 volume=456711 完全一致）。
        所以 `volume_unit="lot100"`，`amount_unit="CNY"`。

        自检同上：只写契约判定 `ok` 的行。
        """
        target_date = trade_date or date.today()
        precheck = local_clock(now if now is not None else datetime.now())
        if precheck is None or precheck.date() != target_date:
            return {"status": "not_today", "written": 0}
        if not time(9, 25) <= precheck.time() <= time(9, 25, 30):
            return {"status": "outside_evidence_window", "written": 0,
                    "observed_at": precheck.isoformat()}
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

        quotes = await self._fetch_eastmoney_quotes(list(codes))
        observed_at = local_clock(now if now is not None else datetime.now())
        if observed_at is None or not time(9, 25) <= observed_at.time() <= time(9, 25, 30):
            return {"status": "outside_evidence_window", "written": 0,
                    "observed_at": observed_at.isoformat() if observed_at else None,
                    "reason": "取数耗时已越过 09:25:30 证据窗"}
        snapshot_time = observed_at.strftime("%H:%M:%S")
        candidates = []
        for code, row in quotes.items():
            candidates.append(AuctionData(
                code=code, trade_date=target_date, auction_time=snapshot_time,
                auction_price=self._safe_float(row.get("f17")),
                auction_volume=int(self._safe_float(row.get("f5")) or 0),
                auction_amount=self._safe_float(row.get("f6")),
                prev_close=self._safe_float(row.get("f18")),
                volume_ratio=self._safe_float(row.get("f10")),
                source="eastmoney",
                source_version=EASTMONEY_AUCTION_SOURCE_VERSION,
                source_quote_at=eastmoney_quote_clock(row.get("f124")),
                received_at=observed_at,
                observed_at=observed_at,
                price_basis="auction_opening",
                volume_basis="auction_matched",
                volume_unit="lot100",
                amount_unit="CNY",
            ))
        result = await self._save_verified_auction_candidates(
            session, candidates, observed_at, target_date, source_label="东财",
        )
        result["quotes_fetched"] = len(quotes)
        return result

    async def collect_tencent_auction_evidence(
        self,
        session: AsyncSession,
        trade_date: date | None = None,
        *,
        now: datetime | None = None,
        codes: list[str] | None = None,
    ) -> dict:
        """用**腾讯实时行情**生成 09:25 终场竞价证据（唯一可满足契约的来源）。

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
        因此该窗口内：今开 = 竞价撮合价；累计成交量/额 = 竞价撮合量/额；
        且因竞价只有唯一成交价，`成交额 = 成交价 × 成交量` 是恒等式而非估算。
        若该假设不成立，本方法产出的帧就只是"开盘瞬间快照"，
        **不应被当作竞价证据** —— 因此下面逐行用 `auction_evidence_status`
        自检，只保留判定为 `ok` 的行，绝不批量写入未认证的行。

        只写自检通过的行
        ----------------
        每个候选行先构造一个**不加入 session** 的 `AuctionData`，交给
        `auction_evidence_status(row, decision_at=now)` 判定；
        只有 `ok` 才进入落库，其余按状态计数返回。因此本方法
        **不可能伪造证据**：它用的是策略同一个契约函数。
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
            return {"status": "outside_evidence_window", "written": 0,
                    "observed_at": precheck.isoformat()}

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

        records = await TencentSource().collect_spot_batch(list(codes))
        # observed_at 必须在**取数之后**取：契约要求
        # source_quote_at <= received_at <= observed_at，而记录的 received_at
        # 是取数过程中写入的。若在取数前取 observed_at，
        # 记录的 received_at 会晚于它，契约判"future"并拒绝整批
        # （本方法第一版就是这么写的，被自检当场拦下）。
        observed_at = local_clock(now if now is not None else datetime.now())
        if observed_at is None or not time(9, 25) <= observed_at.time() <= time(9, 25, 30):
            return {"status": "outside_evidence_window", "written": 0,
                    "observed_at": observed_at.isoformat() if observed_at else None,
                    "reason": "取数耗时已越过 09:25:30 证据窗"}
        snapshot_time = observed_at.strftime("%H:%M:%S")
        candidates = []
        for record in records:
            if not isinstance(record, dict):
                continue
            code = self._normalize_code(record.get("code", ""))
            if not code:
                continue
            candidates.append(AuctionData(
                code=code, trade_date=target_date, auction_time=snapshot_time,
                auction_price=self._safe_float(record.get("open")),
                auction_volume=int(self._safe_float(record.get("volume")) or 0),
                auction_amount=self._safe_float(record.get("amount")),
                prev_close=self._safe_float(record.get("prev_close")),
                volume_ratio=self._safe_float(record.get("volume_ratio")),
                source="tencent",
                source_version=TENCENT_AUCTION_SOURCE_VERSION,
                source_quote_at=local_clock(record.get("source_quote_at")),
                received_at=local_clock(record.get("received_at")) or observed_at,
                observed_at=observed_at,
                price_basis="auction_opening",
                volume_basis="auction_matched",
                volume_unit="lot100",
                amount_unit="CNY",
            ))
        result = await self._save_verified_auction_candidates(
            session, candidates, observed_at, target_date, source_label="腾讯",
        )
        result["candidates"] = len(records)
        result["observed_at"] = observed_at.isoformat()
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
        latest_time_subquery = (
            select(
                AuctionData.code.label("code"),
                func.max(AuctionData.auction_time).label("latest_auction_time"),
            )
            .where(
                AuctionData.trade_date == trade_date,
                AuctionData.auction_time.between("09:15:00", "09:25:30"),
            )
            .group_by(AuctionData.code)
            .subquery()
        )
        result = await session.execute(
            select(AuctionData)
            .join(
                latest_time_subquery,
                (latest_time_subquery.c.code == AuctionData.code)
                & (latest_time_subquery.c.latest_auction_time == AuctionData.auction_time),
            )
            .where(AuctionData.trade_date == trade_date)
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

        # 从数据库取竞价数据
        result = await session.execute(
            select(AuctionData).where(
                AuctionData.trade_date == trade_date,
                AuctionData.auction_time.between("09:15:00", "09:25:30"),
            )
        )
        auction_records = result.scalars().all()

        if not auction_records:
            logger.warning(f"无竞价数据: {trade_date}")
            return []

        latest_by_code: dict[str, AuctionData] = {}
        for record in auction_records:
            current = latest_by_code.get(record.code)
            if current is None or str(record.auction_time or "") > str(current.auction_time or ""):
                latest_by_code[record.code] = record

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
            ).order_by(desc(AuctionData.auction_time)).limit(1)
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
