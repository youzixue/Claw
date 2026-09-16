"""实时行情 + 日K线 API（含技术指标计算 + V2.2量比/换手分类）"""

from datetime import date, datetime
from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy import select, desc, and_
from sqlalchemy.ext.asyncio import AsyncSession
import numpy as np

from app.db.session import get_db
from app.data.main_fund import load_current_main_fund_map
from app.models.stock import StockSpot, StockKline
from app.signal.bull_score import BullScoreModel

router = APIRouter()

# V2.2分类器实例
_bull_model = BullScoreModel()


# =========================================================================
# 技术指标计算模块（纯函数，无状态，可复用）
# =========================================================================


def _orderbook_payload(spot: StockSpot) -> dict:
    """统一输出五档盘口字段，供 spot 列表/详情接口复用"""
    return {
        "bid1_price": spot.bid1_price,
        "bid1_volume": spot.bid1_volume,
        "bid2_price": spot.bid2_price,
        "bid2_volume": spot.bid2_volume,
        "bid3_price": spot.bid3_price,
        "bid3_volume": spot.bid3_volume,
        "bid4_price": spot.bid4_price,
        "bid4_volume": spot.bid4_volume,
        "bid5_price": spot.bid5_price,
        "bid5_volume": spot.bid5_volume,
        "ask1_price": spot.ask1_price,
        "ask1_volume": spot.ask1_volume,
        "ask2_price": spot.ask2_price,
        "ask2_volume": spot.ask2_volume,
        "ask3_price": spot.ask3_price,
        "ask3_volume": spot.ask3_volume,
        "ask4_price": spot.ask4_price,
        "ask4_volume": spot.ask4_volume,
        "ask5_price": spot.ask5_price,
        "ask5_volume": spot.ask5_volume,
        "bid_depth_5": spot.bid_depth_5,
        "ask_depth_5": spot.ask_depth_5,
        "orderbook_imbalance": spot.orderbook_imbalance,
        "bid_ask_spread": spot.bid_ask_spread,
        "seal_quality_score": spot.seal_quality_score,
        "support_strength_score": spot.support_strength_score,
        "withdrawal_ratio": spot.withdrawal_ratio,
    }

def _sma(arr: np.ndarray, period: int) -> np.ndarray:
    """简单移动平均"""
    if len(arr) < period:
        return np.full(len(arr), np.nan)
    result = np.full(len(arr), np.nan)
    cumsum = np.cumsum(arr)
    result[period - 1:] = (cumsum[period - 1:] - np.concatenate([[0], cumsum[:-period]])) / period
    return result


def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    """指数移动平均 — 前period-1个为NaN, 第period个用SMA初始化"""
    n = len(arr)
    if n < period:
        return np.full(n, np.nan)
    alpha = 2.0 / (period + 1)
    result = np.full(n, np.nan)
    # 用前period天的SMA作为起始值(标准做法)
    result[period - 1] = np.mean(arr[:period])
    for i in range(period, n):
        result[i] = alpha * arr[i] + (1 - alpha) * result[i - 1]
    return result


def calc_macd(closes: np.ndarray, fast=12, slow=26, signal=9) -> tuple:
    """MACD → (dif, dea, macd_bar) 三个等长数组
    
    DIF = EMA(close, fast) - EMA(close, slow)
    DEA = EMA(DIF, signal) — 从DIF第一个有效位置开始计算
    MACD柱 = 2 * (DIF - DEA)
    """
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    dif = ema_fast - ema_slow
    # DEA: 从DIF第一个有效位置(slow-1)开始算EMA
    # 前面dif为NaN, 用有效段算DEA再拼回去
    valid_start = slow - 1
    dif_valid = dif[valid_start:]
    dea_valid = _ema(dif_valid, signal)
    # DEA的有效起始位置: valid_start + (signal-1)
    dea = np.full(len(closes), np.nan)
    dea[valid_start:] = dea_valid
    macd_bar = np.full(len(closes), np.nan)
    # MACD柱只在DEA有效时才有值
    dea_valid_start = valid_start + signal - 1
    for i in range(dea_valid_start, len(closes)):
        if not np.isnan(dif[i]) and not np.isnan(dea[i]):
            macd_bar[i] = 2 * (dif[i] - dea[i])
    return dif, dea, macd_bar


def calc_rsi(closes: np.ndarray, period=14) -> np.ndarray:
    """RSI 相对强弱指数"""
    n = len(closes)
    rsi = np.full(n, np.nan)
    if n < period + 1:
        return rsi
    delta = np.diff(closes)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.zeros(n)
    avg_loss = np.zeros(n)
    avg_gain[period] = np.mean(gain[1:period + 1])
    avg_loss[period] = np.mean(loss[1:period + 1])
    # 注意: np.diff产生n-1长度的数组，所以gain/loss的有效索引是0..n-2
    # 循环变量i是closes的索引(0..n-1)，对应的diff值在i-1位置
    for i in range(period + 1, n):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i - 1]) / period
    rs = np.where(avg_loss[period + 1:] != 0, avg_gain[period + 1:] / avg_loss[period + 1:], 100.0)
    rsi[period + 1:] = 100.0 - (100.0 / (1.0 + rs))
    # 第一条有效RSI用SMA方式
    if avg_loss[period] != 0:
        rsi[period] = 100.0 - (100.0 / (1.0 + avg_gain[period] / avg_loss[period]))
    else:
        rsi[period] = 100.0
    return rsi


def calc_kdj(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, n=9, m1=3, m2=3) -> tuple:
    """KDJ 随机指标 → (k, d, j)"""
    length = len(closes)
    k_arr = np.full(length, 50.0)
    d_arr = np.full(length, 50.0)
    j_arr = np.full(length, 50.0)
    if length < n:
        return k_arr, d_arr, j_arr
    rsv = np.full(length, np.nan)
    for i in range(n - 1, length):
        h_n = highs[i - n + 1:i + 1].max()
        l_n = lows[i - n + 1:i + 1].min()
        if h_n != l_n:
            rsv[i] = (closes[i] - l_n) / (h_n - l_n) * 100
        else:
            rsv[i] = 50.0
    # K = SMA(RSV, M1), D = SMA(K, M2), J = 3*K - 2*D
    prev_k = 50.0
    prev_d = 50.0
    for i in range(n - 1, length):
        if not np.isnan(rsv[i]):
            k_arr[i] = (2.0 * prev_k + rsv[i]) / m1
            d_arr[i] = (2.0 * prev_d + k_arr[i]) / m2
            j_arr[i] = 3.0 * k_arr[i] - 2.0 * d_arr[i]
            prev_k = k_arr[i]
            prev_d = d_arr[i]
        else:
            k_arr[i] = prev_k
            d_arr[i] = prev_d
            j_arr[i] = 3.0 * prev_k - 2.0 * prev_d
    return k_arr, d_arr, j_arr


def calc_boll(closes: np.ndarray, period=20, std_mult=2.0) -> tuple:
    """布林带 → (upper, mid, lower)"""
    mid = _sma(closes, period)
    std = np.full(len(closes), np.nan)
    for i in range(period - 1, len(closes)):
        std[i] = np.std(closes[i - period + 1:i + 1], ddof=0)
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    return upper, mid, lower


def compute_indicators(klines_data: list) -> list:
    """对K线数据追加所有技术指标，返回增强后的列表

    输入: [{open, close, high, low, volume, ...}]
    输出: 同结构 + ma5/ma10/ma20/ma60/boll_upper/boll_mid/boll_lower/
          dif/dea/macd_bar/rsi6/rsi14/k/d/j/vol_ma5/vol_ma10
    """
    n = len(klines_data)
    if n < 5:
        return klines_data

    closes = np.array([float(k['close']) for k in klines_data])
    highs = np.array([float(k['high']) for k in klines_data])
    lows = np.array([float(k['low']) for k in klines_data])
    volumes = np.array([float(k.get('volume', 0)) or 0 for k in klines_data])

    # MA均线
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)

    # BOLL
    boll_up, boll_mid, boll_low = calc_boll(closes, 20, 2.0)

    # MACD(12,26,9)
    dif, dea, macd_bar = calc_macd(closes)

    # RSI
    rsi6 = calc_rsi(closes, 6)
    rsi14 = calc_rsi(closes, 14)

    # KDJ(9,3,3)
    k_arr, d_arr, j_arr = calc_kdj(highs, lows, closes, 9, 3, 3)

    # 成交量MA
    vol_ma5 = _sma(volumes.astype(float), 5)
    vol_ma10 = _sma(volumes.astype(float), 10)

    # 写回每条K线
    for i, k in enumerate(klines_data):
        def _v(val):
            x = float(val[i]) if i < len(val) else None
            return round(x, 4) if not (np.isnan(x) if isinstance(x, (float, np.floating)) else False) else None

        k['ma5'] = _v(ma5)
        k['ma10'] = _v(ma10)
        k['ma20'] = _v(ma20)
        k['ma60'] = _v(ma60)
        k['boll_upper'] = _v(boll_up)
        k['boll_mid'] = _v(boll_mid)
        k['boll_lower'] = _v(boll_low)
        k['dif'] = _v(dif)
        k['dea'] = _v(dea)
        k['macd'] = round(float(macd_bar[i]), 4) if i < len(macd_bar) and not np.isnan(float(macd_bar[i])) else None
        k['rsi6'] = _v(rsi6)
        k['rsi14'] = _v(rsi14)
        k['kdj_k'] = round(float(k_arr[i]), 4) if i < len(k_arr) and not np.isnan(float(k_arr[i])) else None
        k['kdj_d'] = round(float(d_arr[i]), 4) if i < len(d_arr) and not np.isnan(float(d_arr[i])) else None
        k['kdj_j'] = round(float(j_arr[i]), 4) if i < len(j_arr) and not np.isnan(float(j_arr[i])) else None
        k['vol_ma5'] = _v(vol_ma5)
        k['vol_ma10'] = _v(vol_ma10)

    return klines_data


def _main_fund_payload(item: dict | None, reason: str = "missing") -> dict:
    """Explicit current-fund truth; rejected values never enter display or sort."""
    item = item or {}
    status = "ok" if item else reason
    return {
        "main_net_inflow": item.get("main_net_inflow"),
        "main_net_inflow_pct": item.get("main_net_inflow_pct"),
        "main_fund_status": "unknown" if status == "missing" else status,
        "main_fund_reason": status,
        "main_fund_available": bool(item),
        "main_fund_source": item.get("source"),
        "main_fund_source_version": item.get("source_version"),
        "main_fund_source_quote_at": item["source_quote_at"].isoformat() if item else None,
        "main_fund_received_at": item["received_at"].isoformat() if item else None,
        "main_fund_observed_at": item["observed_at"].isoformat() if item else None,
    }


# ===== 实时行情 =====

@router.get("/spot")
async def spot_list(
    codes: str = Query("", description="股票代码(逗号分隔)"),
    limit: int = Query(50, description="返回数量"),
    sort_by: str = Query("change_pct", description="排序字段: change_pct/main_net_inflow/volume_ratio/turnover"),
    min_change: float = Query(-100, description="最小涨跌幅%"),
    db: AsyncSession = Depends(get_db),
):
    """实时行情列表"""
    query = select(StockSpot).where(StockSpot.price > 0)

    if codes:
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
        query = query.where(StockSpot.code.in_(code_list))

    if min_change > -100:
        query = query.where(StockSpot.change_pct >= min_change)

    # Funds must be ranked using the same qualified projection returned below,
    # never by the legacy Tencent order-book column (including pre-fix rows).
    now = datetime.now()
    fund_sort = sort_by == "main_net_inflow"
    if not fund_sort:
        sort_col = getattr(StockSpot, sort_by, StockSpot.change_pct)
        query = query.order_by(desc(sort_col)).limit(limit)
    result = await db.execute(query)
    spots = result.scalars().all()
    fund_diagnostics = {}
    fund_map = await load_current_main_fund_map(
        db, trade_date=now.date(), decision_at=now, codes=[s.code for s in spots],
        diagnostics=fund_diagnostics,
    )
    if fund_sort:
        spots.sort(key=lambda s: (
            s.code not in fund_map, -fund_map.get(s.code, {}).get("main_net_inflow", 0),
            s.code,
        ))
        spots = spots[:max(0, limit)]

    return {
        "spots": [
            {
                "code": s.code,
                "name": s.name,
                "price": s.price,
                "prev_close": s.prev_close,
                "open": s.open,
                "high": s.high,
                "low": s.low,
                "change_pct": s.change_pct,
                "change_amt": s.change_amt,
                "amplitude": s.amplitude,
                "volume": s.volume,
                "amount": s.amount,
                "turnover": s.turnover,
                "volume_ratio": s.volume_ratio,
                **_main_fund_payload(fund_map.get(s.code), fund_diagnostics.get(s.code, "missing")),
                "avg_price": s.avg_price,
                "bid_ratio": s.bid_ratio,
                "circ_market_cap": s.circ_market_cap,
                "pe_ttm": s.pe_ttm,
                "pb": s.pb,
                "dividend_yield": s.dividend_yield,
                "net_profit_growth": s.net_profit_growth,
                "limit_up": s.limit_up,
                "limit_down": s.limit_down,
                "min5_change": s.min5_change,
                "source_quote_at": s.source_quote_at.isoformat(sep=" ") if s.source_quote_at else None,
                "received_at": s.received_at.isoformat(sep=" ") if s.received_at else None,
                "updated_at": s.updated_at.isoformat(sep=" ") if s.updated_at else None,
                "orderbook": _orderbook_payload(s),
                # V2.2融合: 量比/换手/量价分类 — 用is not None防止吞0
                "volume_ratio_level": _bull_model._classify_volume_ratio(s.volume_ratio if s.volume_ratio is not None else 1.0),
                "turnover_level": _bull_model._classify_turnover(s.turnover if s.turnover is not None else 0),
                "price_volume_relation": _bull_model._classify_price_volume(s.change_pct if s.change_pct is not None else 0, s.volume_ratio if s.volume_ratio is not None else 1.0),
            }
            for s in spots
        ],
        "count": len(spots),
    }


@router.get("/spot/{code}")
async def spot_detail(code: str, db: AsyncSession = Depends(get_db)):
    """个股实时行情详情"""
    result = await db.execute(
        select(StockSpot).where(StockSpot.code == code)
    )
    spot = result.scalar_one_or_none()

    if not spot:
        return {"code": code, "error": "无数据"}

    now = datetime.now()
    fund_diagnostics = {}
    fund_map = await load_current_main_fund_map(
        db, trade_date=now.date(), decision_at=now, codes=[code],
        diagnostics=fund_diagnostics,
    )
    return {
        "code": spot.code,
        "name": spot.name,
        "price": spot.price,
        "prev_close": spot.prev_close,
        "open": spot.open,
        "high": spot.high,
        "low": spot.low,
        "change_pct": spot.change_pct,
        "change_amt": spot.change_amt,
        "amplitude": spot.amplitude,
        "volume": spot.volume,
        "amount": spot.amount,
        "turnover": spot.turnover,
        "volume_ratio": spot.volume_ratio,
        **_main_fund_payload(fund_map.get(code), fund_diagnostics.get(code, "missing")),
        "avg_price": spot.avg_price,
        "bid_ratio": spot.bid_ratio,
        "circ_market_cap": spot.circ_market_cap,
        "pe_ttm": spot.pe_ttm,
        "pb": spot.pb,
        "dividend_yield": spot.dividend_yield,
        "net_profit_growth": spot.net_profit_growth,
        "limit_up": spot.limit_up,
        "limit_down": spot.limit_down,
        "min5_change": spot.min5_change,
        "orderbook": _orderbook_payload(spot),
        "source_quote_at": spot.source_quote_at.isoformat(sep=" ") if spot.source_quote_at else None,
        "received_at": spot.received_at.isoformat(sep=" ") if spot.received_at else None,
        "updated_at": spot.updated_at.isoformat(sep=" ") if spot.updated_at else None,
        # V2.2融合: 量比/换手/量价分类 — 用is not None防止吞0
        "volume_ratio_level": _bull_model._classify_volume_ratio(spot.volume_ratio if spot.volume_ratio is not None else 1.0),
        "turnover_level": _bull_model._classify_turnover(spot.turnover if spot.turnover is not None else 0),
        "price_volume_relation": _bull_model._classify_price_volume(spot.change_pct if spot.change_pct is not None else 0, spot.volume_ratio if spot.volume_ratio is not None else 1.0),
    }


# ===== 日K线 =====

@router.get("/kline/{code}")
async def kline_list(
    code: str,
    limit: int = Query(800, description="返回条数(最大2000)"),
    db: AsyncSession = Depends(get_db),
):
    """个股日K线(前复权) + 全量技术指标

    返回: OHLCV + MA(5/10/20/60) + BOLL(上/中/下) + MACD(DIF/DEA/柱)
          + RSI(6/14) + KDJ(K/D/J) + VOL_MA(5/10)
    """
    limit = min(limit, 2000)
    result = await db.execute(
        select(StockKline)
        .where(StockKline.code == code)
        .order_by(desc(StockKline.trade_date))
        .limit(limit)
    )
    klines = list(reversed(result.scalars().all()))  # 从远到近

    raw_klines = []
    for k in klines:
        volume = float(k.volume or 0)
        amount = float(k.amount or 0)

        # 兼容历史 spot_fallback 记录:
        # 早期 fallback 把 stock_spot 的“手”直接写进了 stock_kline.volume，
        # 但后面已经把库里绝大多数历史脏数据回填成“股”。
        # 这里不能再无脑 *100，否则已回填的数据会再次被放大 100 倍。
        #
        # 判断依据:
        # - 正常“股”口径下，amount / volume 大致接近股价(个位到百位)
        # - 错误“手”口径下，amount / volume 会接近股价 * 100，明显偏大
        #
        # 之前这里用固定阈值 50，会把像 78 元这类正常股价也误判成“手”，
        # 导致 recent spot_fallback 记录在接口层再次被放大 100 倍。
        # 这里改成和当日价格联动的阈值：只有单股均额明显超过股价 20 倍
        # （或绝对值超过 500 元/股）时，才认为原始 volume 仍是“手”。
        if k.source == "spot_fallback" and volume > 0 and amount > 0:
            avg_amount_per_share = amount / volume
            reference_price = max(
                float(k.close or 0),
                float(k.open or 0),
                float(k.prev_close or 0),
                1.0,
            )
            if avg_amount_per_share > max(reference_price * 20, 500):
                volume *= 100

        raw_klines.append(
            {
                "trade_date": str(k.trade_date),
                "open": k.open,
                "close": k.close,
                "high": k.high,
                "low": k.low,
                "volume": volume,
                "amount": k.amount,
                "turnover": k.turnover,
                "change_pct": k.change_pct,
                "prev_close": k.prev_close,
            }
        )

    # 追加技术指标计算
    enhanced = compute_indicators(raw_klines)

    return {
        "code": code,
        "klines": enhanced,
        "count": len(enhanced),
    }
