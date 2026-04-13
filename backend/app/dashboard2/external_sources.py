"""Dashboard 2.0 外部联动因子采集"""

from datetime import datetime
from typing import List, Optional

import akshare as ak
import pandas as pd
import requests

from .schemas import ExternalFactorItem


def _safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


class ExternalFactorCollector:
    def collect(self) -> List[ExternalFactorItem]:
        items: List[ExternalFactorItem] = []
        items.extend(self._collect_hk_indices())
        items.extend(self._collect_fx())
        items.extend(self._collect_us_indices())
        items.extend(self._collect_tencent_indices())
        items.extend(self._collect_us10y())
        items.extend(self._collect_commodities())
        return items

    def _collect_hk_indices(self) -> List[ExternalFactorItem]:
        out: List[ExternalFactorItem] = []
        try:
            df = ak.stock_hk_index_spot_sina()
            mapping = {
                "HSI": ("hk_hsi", "恒生指数"),
                "HSTECH": ("hk_hstech", "恒生科技"),
            }
            for code, (key, label) in mapping.items():
                hit = df[df["代码"].astype(str) == code]
                if len(hit) == 0:
                    continue
                row = hit.iloc[-1]
                out.append(ExternalFactorItem(
                    key=key,
                    label=label,
                    price=_safe_float(row.get("最新价")),
                    change_pct=_safe_float(row.get("涨跌幅")),
                    trade_time=datetime.now().isoformat(timespec="seconds"),
                    market="HK",
                ))
        except Exception:
            pass
        return out

    def _collect_fx(self) -> List[ExternalFactorItem]:
        out: List[ExternalFactorItem] = []
        try:
            df = ak.fx_spot_quote()
            for pair, key, label in [
                ("USD/CNY", "fx_usdcny", "美元人民币"),
                ("EUR/CNY", "fx_eurcny", "欧元人民币"),
                ("100JPY/CNY", "fx_jpycny", "日元人民币(100JPY)"),
            ]:
                hit = df[df["货币对"].astype(str) == pair]
                if len(hit) == 0:
                    continue
                row = hit.iloc[-1]
                mid = (_safe_float(row.get("买报价")) + _safe_float(row.get("卖报价"))) / 2
                out.append(ExternalFactorItem(
                    key=key,
                    label=label,
                    price=mid,
                    change_pct=0,
                    trade_time=datetime.now().isoformat(timespec="seconds"),
                    market="FX",
                ))
        except Exception:
            pass
        return out

    def _collect_us_indices(self) -> List[ExternalFactorItem]:
        out: List[ExternalFactorItem] = []
        mapping = {
            ".IXIC": ("us_nasdaq", "纳斯达克"),
            ".INX": ("us_sp500", "标普500"),
        }
        for symbol, (key, label) in mapping.items():
            try:
                df = ak.index_us_stock_sina(symbol=symbol)
                if isinstance(df, pd.DataFrame) and len(df) > 0:
                    row = df.iloc[-1]
                    prev_close = _safe_float(row.get("open"))
                    close = _safe_float(row.get("close"))
                    change_pct = 0.0
                    if prev_close:
                        change_pct = round((close - prev_close) / prev_close * 100, 2)
                    out.append(ExternalFactorItem(
                        key=key,
                        label=label,
                        price=close,
                        change_pct=change_pct,
                        trade_time=str(row.get("date")) if row.get("date") is not None else None,
                        market="US",
                    ))
            except Exception:
                continue
        return out

    def _parse_tencent_quote(self, code: str) -> Optional[list[str]]:
        try:
            url = f"https://qt.gtimg.cn/q={code}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            text = resp.text.strip()
            if '="";' in text or 'none_match' in text:
                return None
            raw = text.split('="', 1)[1].rsplit('";', 1)[0]
            return raw.split('~') if '~' in raw else raw.split(',')
        except Exception:
            return None

    def _collect_tencent_indices(self) -> List[ExternalFactorItem]:
        out: List[ExternalFactorItem] = []

        # A50 CFD
        a50 = self._parse_tencent_quote('hf_CHA50CFD')
        if a50 and len(a50) >= 14:
            out.append(ExternalFactorItem(
                key='a50',
                label='富时中国A50',
                price=_safe_float(a50[0]),
                change_pct=_safe_float(a50[1]),
                trade_time=f"{a50[12]} {a50[6]}".strip() if len(a50) > 12 else None,
                market='A50',
            ))

        # 纳斯达克中国金龙指数
        hxc = self._parse_tencent_quote('usHXC')
        if hxc and len(hxc) >= 32:
            out.append(ExternalFactorItem(
                key='china_adr',
                label='纳斯达克中国金龙',
                price=_safe_float(hxc[3]),
                change_pct=_safe_float(hxc[32]),
                trade_time=hxc[30] if len(hxc) > 30 else None,
                market='US_CN',
            ))

        return out

    def _collect_us10y(self) -> List[ExternalFactorItem]:
        out: List[ExternalFactorItem] = []
        try:
            df = ak.bond_zh_us_rate()
            if isinstance(df, pd.DataFrame) and len(df) > 0:
                row = df.iloc[-1]
                out.append(ExternalFactorItem(
                    key="rates_us10y",
                    label="美债10Y",
                    price=_safe_float(row.get("美国国债收益率10年")),
                    change_pct=0,
                    trade_time=str(row.get("日期")) if row.get("日期") is not None else None,
                    market="US_RATE",
                ))
        except Exception:
            pass
        return out

    def _collect_commodities(self) -> List[ExternalFactorItem]:
        out: List[ExternalFactorItem] = []
        symbol_map = {
            "XAU": ("gold", "黄金"),
            "CL": ("oil", "原油"),
        }
        for symbol, (key, label) in symbol_map.items():
            try:
                df = ak.futures_foreign_commodity_realtime(symbol=symbol)
                if len(df) == 0:
                    continue
                row = df.iloc[-1]
                out.append(ExternalFactorItem(
                    key=key,
                    label=label,
                    price=_safe_float(row.get("最新价")),
                    change_pct=_safe_float(row.get("涨跌幅")),
                    trade_time=f"{row.get('日期', '')} {row.get('行情时间', '')}".strip() or None,
                    market="CMDTY",
                ))
            except Exception:
                continue
        return out


external_factor_collector = ExternalFactorCollector()
