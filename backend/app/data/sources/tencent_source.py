"""腾讯实时行情数据源 — qt.gtimg.cn

能力:
- 批量获取A股实时行情(88字段/只, 我们解析23个核心字段)
- 100只/请求, 5000只≈50批次≈4秒
- 盘中30秒/轮采集 → stock_spot 表单行覆盖

字段映射(2026-04-15 APP验证):
  [3]最新价 [4]昨收 [5]开 [6]量 [32]涨幅% [33]高 [34]低 [38]换手%
  [9-18]买1-5价量 [19-28]卖1-5价量
  [39]PE_TTM [44]流通市值 [46]PB [47]涨停 [48]跌停 [49]量比
  [50]五档委差(手，2026-09-09原始响应复核；不是主力资金) [51]VWAP
  [64]股息率TTM [74]委比 [79]净利润增速%
  [62]不是5分钟涨跌，分钟动能由扫描器使用连续spot快照计算

高估值偏差: [44]流通市值较准, [46]PB高价股偏差+13%, 用[44]/(总股本*价)自算更准
"""

import asyncio
import re
import time as _time
from datetime import datetime
from typing import Optional

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.sources.base import DataSourceBase
from app.config.settings import settings
from app.data.sources.tencent_fund_flow import (
    FUND_URL, SOURCE_VERSION, parse_tencent_fund_payload, tencent_fund_symbol,
)


class TencentSource(DataSourceBase):
    """腾讯实时行情数据源"""

    source_name = "tencent"
    rate_limit = 0.08  # 100只/请求, 约0.08秒/请求

    # 腾讯行情URL模板
    SPOT_URL = "https://qt.gtimg.cn/q="

    def __init__(self, *, capture_response_observed_at: bool = False):
        super().__init__()
        # Auction-only provenance; ordinary spot payloads retain their schema.
        self._capture_response_observed_at = capture_response_observed_at
        # 盘口差分只属于当前实例、当前源交易日；进程内多实例和跨日均不得串用。
        self._previous_orderbook: dict[str, dict] = {}
        self._orderbook_trade_date = None
        self._fund_busy = False
        self._fund_last_code = None
        self._fund_retry_at = 0.0
        self._fund_failed_rounds = 0

    # 代码前缀映射
    CODE_PREFIX = {
        "6": "sh",   # 沪市主板 60xxxx
        "0": "sz",   # 深市主板 00xxxx
        "3": "sz",   # 创业板 300xxx
        "68": "sh",  # 科创板 688xxx
        "4": "bj",   # 北交所 4xxxxx/8xxxxx
        "8": "bj",
    }

    def _prefix(self, code: str) -> str:
        """获取市场前缀"""
        if code.startswith("68"):
            return "sh"
        return self.CODE_PREFIX.get(code[0], "sz")

    def _tencent_code(self, code: str) -> str:
        """转为腾讯代码格式: sh000001"""
        return f"{self._prefix(code)}{code}"

    async def _fetch_batch(
        self,
        codes: list[str],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, list[str]]:
        """批量获取行情(100只/请求)

        Returns:
            {code: [field0, field1, ...]}  88字段列表
        """
        tencent_codes = [self._tencent_code(c) for c in codes]
        url = self.SPOT_URL + ",".join(tencent_codes)

        if client is None:
            async with httpx.AsyncClient(timeout=10) as owned_client:
                return await self._fetch_batch(codes, client=owned_client)

        resp = await client.get(url)
        resp.raise_for_status()
        text = resp.text

        # 解析: v_sh000001="1~上证指数~000001~3261.56~..."
        result = {}
        pattern = re.compile(r'v_[^=]+="([^"]*)"')
        for match in pattern.finditer(text):
            fields = match.group(1).split("~")
            if len(fields) >= 80:
                # field[2]是纯数字代码
                code = fields[2] if len(fields[2]) == 6 else ""
                if code:
                    result[code] = fields

        return result

    def _fetch_missing_batches_sync(self, codes: list[str]) -> dict[str, list[str]]:
        """事件循环拥挤时在线程内顺序补抓缺失批次。

        开盘阶段新闻/竞价/快照任务可能短时占满单进程事件循环，异步请求的
        2秒连接预算会在真正获得调度前耗尽。同步补抓运行在线程中，且连续
        失败5批即停止，既恢复行情覆盖率，也避免数据源故障时拖住下一轮。
        """
        result: dict[str, list[str]] = {}
        consecutive_failures = 0
        pattern = re.compile(r'v_[^=]+="([^"]*)"')
        try:
            with httpx.Client(
                timeout=httpx.Timeout(2.5, connect=1.5),
                trust_env=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            ) as client:
                for start in range(0, len(codes), 100):
                    batch = codes[start:start + 100]
                    url = self.SPOT_URL + ",".join(
                        self._tencent_code(code) for code in batch
                    )
                    try:
                        response = client.get(url)
                        response.raise_for_status()
                    except Exception:
                        consecutive_failures += 1
                        if consecutive_failures >= 5:
                            break
                        continue
                    consecutive_failures = 0
                    for match in pattern.finditer(response.text):
                        fields = match.group(1).split("~")
                        if len(fields) < 80:
                            continue
                        code = fields[2] if len(fields[2]) == 6 else ""
                        if code:
                            result[code] = fields
        except Exception as exc:
            logger.warning(f"[tencent] 线程补抓初始化失败: {exc}")
        return result

    # 腾讯字段[6]「成交量」的单位按板块不同：
    #   主板/创业板 → 「手」；科创板 688xxx → 「股」。
    # 该差异于 2026-09-16 用换手率交叉验证确认（全样本 5197 只）：
    #   按「手」解释时，创业板/沪主板/深主板的 |推算换手率-实际换手率| 相对误差中位
    #   分别为 0.0009 / 0.0018 / 0.0012；而科创板为 98.99。
    #   按「股」解释时科创板为 0.0013。两象限完全分离，无歧义。
    # stock_spot.volume 的既有契约是「手」（见 scheduler.py 的 spot→kline 补全与
    # anomaly_scanner._spot_volume_in_kline_unit），因此科创板在此归一化，
    # 使下游所有 ×100 转换继续成立。
    STAR_MARKET_PREFIX = "68"

    @classmethod
    def _volume_in_hands(cls, code: str, raw_volume: int) -> int:
        """把腾讯字段[6]的成交量归一化为「手」，与 stock_spot.volume 契约一致。

        科创板成交量以「股」返回，且不保证是 100 的整数倍（科创板允许 200 股以上
        按 1 股递增申报）。四舍五入到整数手的相对误差 < 0.001%，远小于此前
        统一按「手」处理造成的 100 倍口径错误。
        """
        if str(code).startswith(cls.STAR_MARKET_PREFIX):
            return int(round(int(raw_volume) / 100.0))
        return int(raw_volume)

    @staticmethod
    def _parse_source_quote_at(fields: list[str]) -> datetime | None:
        """解析腾讯字段[30]的源行情时间；异常值保持为空，禁止伪造。"""
        raw = str(fields[30] if len(fields) > 30 else "").strip()
        if not raw:
            return None
        for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    def _parse_spot(
        self,
        code: str,
        fields: list[str],
        *,
        received_at: datetime | None = None,
    ) -> Optional[dict]:
        """解析88字段 → 23个核心字段dict

        字段索引参照2026-04-14 APP验证结果
        """
        try:
            def _f(idx: int, default=0.0) -> float:
                """安全取float"""
                try:
                    val = fields[idx] if idx < len(fields) else ""
                    if val in ("", "-"):
                        return default
                    return float(val)
                except (ValueError, IndexError):
                    return default

            def _i(idx: int, default=0) -> int:
                """安全取int"""
                try:
                    val = fields[idx] if idx < len(fields) else ""
                    if val in ("", "-"):
                        return default
                    return int(float(val))
                except (ValueError, IndexError):
                    return default

            name = fields[1] if len(fields) > 1 else ""
            source_quote_at = self._parse_source_quote_at(fields)
            received_at = received_at or datetime.now()
            price = _f(3)
            prev_close = _f(4)
            open_price = _f(5)
            volume_hands = self._volume_in_hands(code, _i(6))  # 成交量(手，已按板块归一化)
            bid_prices = [_f(9), _f(11), _f(13), _f(15), _f(17)]
            bid_volumes = [_i(10), _i(12), _i(14), _i(16), _i(18)]
            ask_prices = [_f(19), _f(21), _f(23), _f(25), _f(27)]
            ask_volumes = [_i(20), _i(22), _i(24), _i(26), _i(28)]
            change_pct = _f(32)
            high = _f(33)
            low = _f(34)
            turnover = _f(38)       # 换手率%
            pe_ttm = _f(39)
            # 2026-04 实测腾讯该字段已是“亿”口径，不能再额外 / 1e8，
            # 否则会把流通市值缩小 1e8 倍，直接污染详情页和异动卡片。
            circ_market_cap = _f(44)
            pb = _f(46)
            limit_up = _f(47)
            limit_down = _f(48)
            volume_ratio = _f(49)
            # 2026-09-09 原始88字段与五档挂单复核：[50]为五档委差(手)，
            # 不是主力净流入(元)。真实主力资金由合格 FundFlow 提供；
            # 保留兼容字段但明确缺失，不把盘口差额或缺值0冒充资金。
            main_net_inflow = None
            avg_price = _f(51)        # VWAP
            # 2026-08-04 再次用原始88字段逐项核验：A股下标62会返回
            # -40.26、37.91、32.33一类长周期涨跌值，并非5分钟涨跌幅。
            # 继续入库会把普通反弹误报为分钟级急拉；真实5分钟动能改由
            # AnomalyScanner基于连续实时快照计算，源字段在此保守置空。
            min5_change = None
            dividend_yield = _f(64)
            net_profit_growth = _f(79)

            # 计算派生字段
            change_amt = round(price - prev_close, 2) if prev_close > 0 else 0
            amplitude = round((high - low) / prev_close * 100, 2) if prev_close > 0 else 0
            # 腾讯 volume 已由 _volume_in_hands 归一化为「手」，成交额按 股价 * 手数 * 100股 估算
            amount = avg_price * volume_hands * 100 if avg_price > 0 else 0
            bid_ratio = _f(74)
            bid_depth_5 = sum(v for v in bid_volumes if v > 0)
            ask_depth_5 = sum(v for v in ask_volumes if v > 0)
            total_depth = bid_depth_5 + ask_depth_5
            # 缺盘口与真实均衡(0)必须区分；没有可见深度时保留 None，不能
            # 用中性分数50或价差0伪装成已观测数据。
            orderbook_imbalance = (
                round((bid_depth_5 - ask_depth_5) / total_depth, 4)
                if total_depth > 0
                else None
            )
            bid_ask_spread = (
                round(max(ask_prices[0] - bid_prices[0], 0), 4)
                if ask_prices[0] > 0 and bid_prices[0] > 0
                else None
            )
            best_bid_share = (bid_volumes[0] / bid_depth_5) if bid_depth_5 > 0 else 0.0
            best_ask_share = (ask_volumes[0] / ask_depth_5) if ask_depth_5 > 0 else 0.0
            support_strength_score = (
                min(
                    100.0,
                    max(
                        0.0,
                        50
                        + orderbook_imbalance * 35
                        + best_bid_share * 20
                        - best_ask_share * 10,
                    ),
                )
                if orderbook_imbalance is not None
                else None
            )

            seal_quality_score = 0.0
            if limit_up > 0 and price >= limit_up and bid_prices[0] >= limit_up:
                seal_quality_score = min(
                    100.0,
                    55
                    + min(bid_volumes[0] / 5000, 25)
                    + min(bid_depth_5 / 12000, 20),
                )

            quote_date = (source_quote_at or received_at).date()
            if self._orderbook_trade_date is None or quote_date > self._orderbook_trade_date:
                self._previous_orderbook.clear()
                self._orderbook_trade_date = quote_date
            stale_cross_day_quote = (
                self._orderbook_trade_date is not None
                and quote_date < self._orderbook_trade_date
            )
            prev_book = {} if stale_cross_day_quote else (self._previous_orderbook.get(code) or {})
            withdrawal_ratio = None
            # 价格上移时买五档会整体换档，买五总量下降不等于撤单。
            # 只比较前后快照中仍然存在的相同买价队列，且绝不跨交易日做差分。
            prev_bid_levels = dict(prev_book.get("bid_levels") or {})
            current_bid_levels = {
                round(level_price, 4): level_volume
                for level_price, level_volume in zip(bid_prices, bid_volumes)
                if level_price > 0 and level_volume > 0
            }
            common_prices = set(prev_bid_levels) & set(current_bid_levels)
            previous_common_depth = sum(prev_bid_levels[level] for level in common_prices)
            current_common_depth = sum(current_bid_levels[level] for level in common_prices)
            if len(common_prices) >= 2 and previous_common_depth > 0:
                withdrawal_ratio = round(
                    min(
                        1.0,
                        max(
                            0.0,
                            (previous_common_depth - current_common_depth)
                            / previous_common_depth,
                        ),
                    ),
                    4,
                )
            if not stale_cross_day_quote:
                self._previous_orderbook[code] = {
                    "bid_depth_5": bid_depth_5,
                    "ask_depth_5": ask_depth_5,
                    "bid_levels": current_bid_levels,
                    "quote_date": quote_date.isoformat(),
                    "ts": _time.time(),
                }

            return {
                "code": code,
                "name": name,
                "price": price,
                "prev_close": prev_close,
                "open": open_price,
                "high": high,
                "low": low,
                "limit_up": limit_up,
                "limit_down": limit_down,
                "change_pct": change_pct,
                "change_amt": change_amt,
                "amplitude": amplitude,
                "min5_change": min5_change,
                "volume": volume_hands,
                "amount": round(amount, 2),
                "turnover": turnover,
                "volume_ratio": volume_ratio,
                "main_net_inflow": main_net_inflow,
                "avg_price": avg_price,
                "bid_ratio": bid_ratio,
                "bid1_price": bid_prices[0],
                "bid1_volume": bid_volumes[0],
                "bid2_price": bid_prices[1],
                "bid2_volume": bid_volumes[1],
                "bid3_price": bid_prices[2],
                "bid3_volume": bid_volumes[2],
                "bid4_price": bid_prices[3],
                "bid4_volume": bid_volumes[3],
                "bid5_price": bid_prices[4],
                "bid5_volume": bid_volumes[4],
                "ask1_price": ask_prices[0],
                "ask1_volume": ask_volumes[0],
                "ask2_price": ask_prices[1],
                "ask2_volume": ask_volumes[1],
                "ask3_price": ask_prices[2],
                "ask3_volume": ask_volumes[2],
                "ask4_price": ask_prices[3],
                "ask4_volume": ask_volumes[3],
                "ask5_price": ask_prices[4],
                "ask5_volume": ask_volumes[4],
                "bid_depth_5": bid_depth_5,
                "ask_depth_5": ask_depth_5,
                "orderbook_imbalance": orderbook_imbalance,
                "bid_ask_spread": bid_ask_spread,
                "seal_quality_score": round(seal_quality_score, 2),
                "support_strength_score": (
                    round(support_strength_score, 2)
                    if support_strength_score is not None
                    else None
                ),
                "withdrawal_ratio": withdrawal_ratio,
                "circ_market_cap": circ_market_cap,
                "pe_ttm": pe_ttm,
                "pb": pb,
                "dividend_yield": dividend_yield,
                "net_profit_growth": net_profit_growth,
                "source_quote_at": source_quote_at,
                "received_at": received_at,
            }
        except Exception as e:
            logger.warning(f"[tencent] 解析 {code} 失败: {e}")
            return None

    async def collect_spot_batch(self, codes: list[str]) -> list[dict]:
        """批量采集实时行情 → list[dict]

        Args:
            codes: 股票代码列表 如 ["000001", "600519"]

        Returns:
            解析后的dict列表, 每个dict含23个字段
        """
        batch_size = 100
        batches = [codes[i:i + batch_size] for i in range(0, len(codes), batch_size)]
        max_concurrency = 6
        semaphore = asyncio.Semaphore(max_concurrency)

        # 同一轮全市场共用连接池并做有界并发。串行采集在实盘
        # 已漂移到57~63秒/轮，会让真正60秒急拉窗口失效。
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(6.0, connect=2.0),
            # 行情链路不能继承桌面进程的临时 HTTP(S)_PROXY。盘中曾因代理
            # DNS/连接异常让全部31个批次连续超时，直接丢失封板前窗口。
            trust_env=False,
            limits=httpx.Limits(
                max_connections=max_concurrency,
                max_keepalive_connections=max_concurrency,
            ),
        ) as client:
            async def collect_one(batch_index: int, batch: list[str]) -> list[dict]:
                async with semaphore:
                    if batch_index:
                        await asyncio.sleep(
                            self.rate_limit * (batch_index % max_concurrency)
                        )
                    raw: dict[str, list[str]] = {}
                    batch_received_at: datetime | None = None
                    for attempt in range(2):
                        started_at = _time.monotonic()
                        try:
                            raw = await self._fetch_batch(batch, client=client)
                            batch_received_at = datetime.now()
                            break
                        except Exception as exc:
                            elapsed = _time.monotonic() - started_at
                            logger.error(
                                f"[tencent] 批次{batch_index}采集失败"
                                f"(attempt={attempt + 1} elapsed={elapsed:.2f}s): {exc}"
                            )
                            # 完整请求已经耗尽超时预算时不再立刻重试，避免31批
                            # 级联阻塞调度器；仅对瞬时DNS/连接错误做一次短退避。
                            if attempt == 0 and elapsed < 1.0:
                                await asyncio.sleep(0.15)
                                continue
                            break
                parsed: list[dict] = []
                for code in batch:
                    if code not in raw:
                        continue
                    spot = self._parse_spot(
                        code,
                        raw[code],
                        received_at=batch_received_at,
                    )
                    if spot and spot["price"] > 0:  # 过滤停牌/无效
                        if self._capture_response_observed_at:
                            # Actual per-response parse observation, before gather
                            # or an unrelated slow batch/rescue can finish.
                            spot["observed_at"] = datetime.now()
                        parsed.append(spot)
                return parsed

            grouped = await asyncio.gather(
                *(collect_one(index, batch) for index, batch in enumerate(batches))
            )

        all_spots: list[dict] = []
        for parsed in grouped:
            all_spots.extend(parsed)

        coverage_ratio = len(all_spots) / len(codes) if codes else 1.0
        if len(codes) >= 500 and coverage_ratio < 0.90:
            captured_codes = {str(item.get("code") or "") for item in all_spots}
            missing_codes = [code for code in codes if code not in captured_codes]
            try:
                rescue_raw = await asyncio.wait_for(
                    asyncio.to_thread(self._fetch_missing_batches_sync, missing_codes),
                    timeout=14.0,
                )
            except (asyncio.TimeoutError, TimeoutError):
                rescue_raw = {}
                logger.warning(
                    f"[tencent] 线程补抓超时: missing={len(missing_codes)}"
                )
            except Exception as exc:
                rescue_raw = {}
                logger.warning(f"[tencent] 线程补抓失败: {exc}")
            rescue_received_at = datetime.now()
            for code in missing_codes:
                fields = rescue_raw.get(code)
                if not fields:
                    continue
                spot = self._parse_spot(
                    code,
                    fields,
                    received_at=rescue_received_at,
                )
                if spot and spot["price"] > 0:
                    if self._capture_response_observed_at:
                        # Rescue clocks are measured here, never backdated to
                        # the earlier async collection or provider timestamp.
                        spot["observed_at"] = datetime.now()
                    all_spots.append(spot)
            coverage_ratio = len(all_spots) / len(codes) if codes else 1.0
            if rescue_raw:
                logger.info(
                    f"[tencent] 线程补抓恢复 {len(rescue_raw)}/{len(missing_codes)} 条，"
                    f"全轮覆盖率={coverage_ratio:.1%}"
                )
        if coverage_ratio < 0.90:
            logger.warning(
                f"[tencent] 本轮行情覆盖率不足: {len(all_spots)}/{len(codes)} "
                f"({coverage_ratio:.1%})，扫描层只使用本轮新鲜报价"
            )

        return all_spots

    async def get_individual_fund_flow(self, codes: list[str]):
        """Bounded rolling hsfundtab collection; no EastMoney/quote-field fallback.

        Unlike quotes (100 codes/request), this endpoint is single-stock. Keep
        the same six-connection ceiling, but rotate a bounded slice per 30s tick
        rather than launching 5,000 requests or waiting for the entire market.
        """
        import bisect
        from collections import Counter
        from email.utils import parsedate_to_datetime
        from datetime import timezone
        import pandas as pd

        universe = []
        for code in codes:
            try:
                tencent_fund_symbol(code)
                universe.append(code)
            except ValueError:
                continue
        universe = sorted(set(universe))
        rows, errors = [], Counter()
        expected = min(len(universe), settings.TENCENT_FUND_FLOW_CODES_PER_ROUND)
        frame_attrs = {
            "fund_flow_source": "tencent", "fund_flow_source_version": SOURCE_VERSION,
            "fund_flow_expected_count": expected, "fund_flow_universe_count": len(universe),
            "fund_flow_attempted_count": 0, "fund_flow_errors": {},
        }

        def frame():
            result = pd.DataFrame(rows, dtype=object)
            result.attrs.update(frame_attrs)
            result.attrs["fund_flow_errors"] = dict(errors)
            return result

        if not universe or self._fund_busy or _time.monotonic() < self._fund_retry_at:
            errors["empty_universe" if not universe else "busy" if self._fund_busy else "backoff"] += 1
            return frame()
        self._fund_busy = True
        start = bisect.bisect_right(universe, self._fund_last_code) if self._fund_last_code else 0
        ordered = universe[start:] + universe[:start]
        pending = iter(ordered[:expected])
        max_concurrency = settings.TENCENT_FUND_FLOW_CONCURRENCY
        stop = False

        async def worker(client):
            nonlocal stop
            while not stop:
                try:
                    code = next(pending)
                except StopIteration:
                    return
                self._fund_last_code = code  # advance attempts, not just successes
                frame_attrs["fund_flow_attempted_count"] += 1
                try:
                    response = await client.get(
                        FUND_URL,
                        params={"code": tencent_fund_symbol(code),
                                "type": "todayFundFlow,todayFundTrend", "klineNeedDay": 1},
                    )
                    received_at = datetime.now()
                    if response.status_code in (403, 429):
                        # Respect denial/Retry-After; do not switch proxy or retry
                        # per stock. Other in-flight requests are bounded by six.
                        delay = 300.0 if response.status_code == 403 else 60.0
                        retry = response.headers.get("Retry-After", "")
                        try:
                            if retry.isascii() and retry.isdigit():
                                delay = max(delay, min(86400.0, float(retry)))
                            elif retry:
                                retry_at = parsedate_to_datetime(retry)
                                delay = max(delay, min(86400.0, (
                                    retry_at - datetime.now(timezone.utc)
                                ).total_seconds()))
                        except (ValueError, TypeError, OverflowError):
                            pass
                        self._fund_retry_at = max(self._fund_retry_at, _time.monotonic() + delay)
                        stop = True
                        errors[f"http_{response.status_code}"] += 1
                        return
                    response.raise_for_status()
                    if len(response.content) > 512_000:
                        raise ValueError("fund_response_too_large")
                    row = parse_tencent_fund_payload(
                        response.json(), code=code, received_at=received_at,
                        max_age_seconds=settings.FUND_FLOW_SOURCE_MAX_AGE_SEC,
                    )
                    rows.append(row)
                except ValueError as exc:
                    # Parser errors are static codes; JSON errors are not logged.
                    reason = str(exc)
                    errors[reason if reason.startswith(("fund_", "missing_", "invalid_", "negative_"))
                           and len(reason) < 80 else "invalid_payload"] += 1
                except httpx.HTTPStatusError as exc:
                    errors[f"http_{exc.response.status_code}"] += 1
                except httpx.HTTPError as exc:
                    errors[type(exc).__name__] += 1
                await asyncio.sleep(settings.TENCENT_FUND_FLOW_REQUEST_INTERVAL_SEC)

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(6.0, connect=2.0), trust_env=False,
                limits=httpx.Limits(max_connections=max_concurrency,
                                   max_keepalive_connections=max_concurrency),
            ) as client:
                tasks = [asyncio.create_task(worker(client)) for _ in range(max_concurrency)]
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks),
                                           settings.TENCENT_FUND_FLOW_ROUND_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    errors["round_timeout"] += 1
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            if rows:
                self._fund_failed_rounds = 0
            else:
                self._fund_failed_rounds = min(self._fund_failed_rounds + 1, 4)
                self._fund_retry_at = max(
                    self._fund_retry_at,
                    _time.monotonic() + min(300, 30 * 2 ** (self._fund_failed_rounds - 1)),
                )
            return frame()
        finally:
            self._fund_busy = False

    async def collect(self, session: AsyncSession, **kwargs) -> list[dict]:
        """通用采集入口"""
        codes = kwargs.get("codes", [])
        return await self.collect_spot_batch(codes)

    async def health_check(self, session: AsyncSession) -> bool:
        """健康检查 — 获取平安银行行情"""
        try:
            spots = await self.collect_spot_batch(["000001"])
            return len(spots) > 0 and spots[0]["price"] > 0
        except Exception:
            return False
