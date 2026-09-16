"""Dashboard 2.0 外部联动因子采集"""

from datetime import datetime
from typing import List, Optional
import math

import akshare as ak
import pandas as pd
import requests

from .schemas import ExternalFactorItem


def _safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        if isinstance(v, str):
            s = v.strip().replace(",", "")
            if s.endswith("%"):
                s = s[:-1]
            if s == "":
                return default
            v = s
        if pd.isna(v):
            return default
        out = float(v)
        if not math.isfinite(out):
            return default
        return out
    except Exception:
        return default


class ExternalFactorCollector:
    def collect(self) -> List[ExternalFactorItem]:
        items: List[ExternalFactorItem] = []
        items.extend(self._collect_hk_indices())
        items.extend(self._collect_fx())
        items.extend(self._collect_us_indices())
        items.extend(self._collect_us_thematic())
        items.extend(self._collect_a50())
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
            "usIXIC": ("us_nasdaq", "纳斯达克"),
            "usINX": ("us_sp500", "标普500"),
        }
        for quote_code, (key, label) in mapping.items():
            fields = self._parse_tencent_quote(quote_code)
            if fields and len(fields) >= 33:
                latest = _safe_float(fields[3], default=0.0)
                pct = _safe_float(fields[32], default=0.0)
                out.append(ExternalFactorItem(
                    key=key,
                    label=label,
                    price=latest,
                    change_pct=pct,
                    trade_time=fields[30] if len(fields) > 30 else None,
                    market="US",
                ))
                continue

            # 腾讯源异常时回退至 AkShare 日线
            try:
                symbol = ".IXIC" if key == "us_nasdaq" else ".INX"
                df = ak.index_us_stock_sina(symbol=symbol)
                if isinstance(df, pd.DataFrame) and len(df) > 0:
                    row = df.iloc[-1]
                    close = _safe_float(row.get("close"))
                    prev_close = _safe_float(row.get("open"))
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

    def _collect_us_thematic(self) -> List[ExternalFactorItem]:
        """补齐外盘主题映射所需的主题因子."""
        out: List[ExternalFactorItem] = []

        def read_quote(code: str):
            fs = self._parse_tencent_quote(code)
            if not fs or len(fs) < 33:
                return None
            return {
                "price": _safe_float(fs[3], default=None),
                "change_pct": _safe_float(fs[32], default=None),
                "trade_time": fs[30] if len(fs) > 30 else None,
            }

        # 美股AI/半导体：优先半导体ETF SOXX，回退SMH
        semi = read_quote("usSOXX") or read_quote("usSMH")
        if semi and semi["price"] is not None:
            out.append(
                ExternalFactorItem(
                    key="us_ai_semiconductor",
                    label="美股AI/半导体",
                    price=semi["price"],
                    change_pct=semi["change_pct"] or 0.0,
                    trade_time=semi["trade_time"],
                    market="US",
                )
            )

        # 美股新能源/电动车：ICLN(清洁能源) + TSLA(电动车龙头) 均值
        clean = read_quote("usICLN")
        ev = read_quote("usTSLA")
        if clean and ev and clean["price"] is not None and ev["price"] is not None:
            out.append(
                ExternalFactorItem(
                    key="us_ev_clean_energy",
                    label="美股新能源/电动车",
                    price=round((clean["price"] + ev["price"]) / 2, 4),
                    change_pct=round(((clean["change_pct"] or 0.0) + (ev["change_pct"] or 0.0)) / 2, 2),
                    trade_time=ev["trade_time"] or clean["trade_time"],
                    market="US",
                )
            )
        elif clean and clean["price"] is not None:
            out.append(
                ExternalFactorItem(
                    key="us_ev_clean_energy",
                    label="美股新能源/电动车",
                    price=clean["price"],
                    change_pct=clean["change_pct"] or 0.0,
                    trade_time=clean["trade_time"],
                    market="US",
                )
            )
        elif ev and ev["price"] is not None:
            out.append(
                ExternalFactorItem(
                    key="us_ev_clean_energy",
                    label="美股新能源/电动车",
                    price=ev["price"],
                    change_pct=ev["change_pct"] or 0.0,
                    trade_time=ev["trade_time"],
                    market="US",
                )
            )

        return out

    def _parse_tencent_quote(self, code: str) -> Optional[list[str]]:
        try:
            url = f"https://qt.gtimg.cn/q={code}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            text = resp.text.strip()
            if '="";' in text:
                return None
            if 'none_match' in text and code != 'hf_CHA50CFD':
                return None
            raw = text.split('="', 1)[1].rsplit('";', 1)[0]
            return raw.split('~') if '~' in raw else raw.split(',')
        except Exception:
            return None

    def _collect_tencent_indices(self) -> List[ExternalFactorItem]:
        out: List[ExternalFactorItem] = []

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

    def _collect_a50(self) -> List[ExternalFactorItem]:
        out: List[ExternalFactorItem] = []
        try:
            resp = requests.get(
                'https://hq.sinajs.cn/list=hf_CHA50CFD',
                timeout=10,
                headers={'Referer': 'https://finance.sina.com.cn'}
            )
            resp.raise_for_status()
            text = resp.text.strip()
            if '="";' in text:
                return out
            raw = text.split('="', 1)[1].rsplit('";', 1)[0]
            parts = raw.split(',')
            if len(parts) >= 14:
                latest = _safe_float(parts[0])
                prev_close = _safe_float(parts[8])
                change_pct = 0.0
                if prev_close:
                    change_pct = round((latest - prev_close) / prev_close * 100, 2)
                out.append(ExternalFactorItem(
                    key='a50',
                    label='富时中国A50',
                    price=latest,
                    change_pct=change_pct,
                    trade_time=f"{parts[12]} {parts[6]}".strip(),
                    market='A50',
                ))
        except Exception:
            pass
        return out

    def _collect_us10y(self) -> List[ExternalFactorItem]:
        out: List[ExternalFactorItem] = []
        try:
            df = ak.bond_zh_us_rate()
            if isinstance(df, pd.DataFrame) and len(df) > 0:
                target_col = None
                for col in ["美国国债收益率10年", "10年", "10Y", "美债10Y", "美国10年期国债收益率"]:
                    if col in df.columns:
                        target_col = col
                        break
                if target_col is None:
                    for col in df.columns:
                        col_s = str(col)
                        if "10" in col_s and ("美国" in col_s or "美债" in col_s):
                            target_col = col
                            break

                value = 0.0
                trade_time = str(df.iloc[-1].get("日期")) if df.iloc[-1].get("日期") is not None else None
                if target_col is not None:
                    # 最新行常见 NaN，向前回溯最近一个有效值
                    for i in range(len(df) - 1, -1, -1):
                        row = df.iloc[i]
                        v = _safe_float(row.get(target_col), default=None)
                        if v is not None:
                            value = v
                            trade_time = str(row.get("日期")) if row.get("日期") is not None else trade_time
                            break

                out.append(ExternalFactorItem(
                    key="rates_us10y",
                    label="美债10Y",
                    price=value,
                    change_pct=0,
                    trade_time=trade_time,
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
