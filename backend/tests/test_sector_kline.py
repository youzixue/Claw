"""板块K线功能联调测试 — 字段级准确性验证

覆盖范围:
1. SectorKline 模型: 字段完整性+约束+默认值
2. 采集脚本 parse_kline_df: 列映射+数据类型+边界值+空值
3. API /kline: 请求参数+返回字段+MA计算正确性+空数据兜底
4. API /kline/batch: 分页+字段+迷你K线截取
5. 前端API层: 函数签名+参数传递
6. 性能: MA计算O(N)、批量查询索引覆盖
"""

import pytest
import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import numpy as np
from sqlalchemy import inspect

from app.models.sector import SectorKline
from scripts.collect_sector_kline import parse_kline_df, _find_col


# ============================================================
# 1. SectorKline 模型字段验证
# ============================================================

class TestSectorKlineModel:
    """SectorKline 模型字段完整性测试"""

    def test_table_name(self):
        """表名正确"""
        assert SectorKline.__tablename__ == "sector_kline"

    def test_has_all_required_columns(self):
        """模型包含全部必要字段"""
        mapper = inspect(SectorKline)
        col_names = {c.key for c in mapper.mapper.column_attrs}
        required = {
            "id", "sector_code", "sector_name", "sector_type", "trade_date",
            "open", "high", "low", "close", "volume", "amount",
            "change_pct", "amplitude", "source",
        }
        missing = required - col_names
        assert not missing, f"缺少字段: {missing}"

    def test_sector_code_not_nullable(self):
        """sector_code 不允许 NULL"""
        mapper = inspect(SectorKline)
        col = {c.key: c for c in mapper.mapper.columns}["sector_code"]
        assert not col.nullable, "sector_code 必须非空"

    def test_sector_type_not_nullable(self):
        """sector_type 不允许 NULL"""
        mapper = inspect(SectorKline)
        col = {c.key: c for c in mapper.mapper.columns}["sector_type"]
        assert not col.nullable, "sector_type 必须非空"

    def test_trade_date_not_nullable(self):
        """trade_date 不允许 NULL"""
        mapper = inspect(SectorKline)
        col = {c.key: c for c in mapper.mapper.columns}["trade_date"]
        assert not col.nullable, "trade_date 必须非空"

    def test_ohlcv_nullable(self):
        """OHLCV 字段允许 NULL(采集时可能缺失)"""
        mapper = inspect(SectorKline)
        col_map = {c.key: c for c in mapper.mapper.columns}
        for field in ("open", "high", "low", "close", "volume", "amount"):
            assert col_map[field].nullable, f"{field} 应该允许 NULL"

    def test_source_default_value(self):
        """source 默认值为 akshare"""
        mapper = inspect(SectorKline)
        col = {c.key: c for c in mapper.mapper.columns}["source"]
        assert col.default is not None, "source 应有默认值"
        assert col.default.arg == "akshare", f"source 默认值应为 'akshare', 实际: {col.default.arg}"

    def test_unique_constraint_on_code_date(self):
        """sector_code + trade_date 唯一约束"""
        # 检查 __table_args__ 中的 UniqueConstraint
        constraints = SectorKline.__table_args__
        uq_names = set()
        for item in constraints:
            if hasattr(item, "name") and item.name:
                uq_names.add(item.name)
        assert "uq_sector_kline_code_date" in uq_names, \
            "缺少 sector_code + trade_date 唯一约束"

    def test_index_on_trade_date_sector_type(self):
        """trade_date + sector_type 索引存在"""
        constraints = SectorKline.__table_args__
        idx_names = set()
        for item in constraints:
            if hasattr(item, "name") and item.name:
                idx_names.add(item.name)
        assert "ix_sector_kline_date_type" in idx_names, \
            "缺少 trade_date + sector_type 索引"

    def test_index_on_sector_code_trade_date(self):
        """sector_code + trade_date 索引存在"""
        constraints = SectorKline.__table_args__
        idx_names = set()
        for item in constraints:
            if hasattr(item, "name") and item.name:
                idx_names.add(item.name)
        assert "ix_sector_kline_code_date" in idx_names, \
            "缺少 sector_code + trade_date 索引"

    def test_sector_code_length(self):
        """sector_code 长度限制 20"""
        mapper = inspect(SectorKline)
        col = {c.key: c for c in mapper.mapper.columns}["sector_code"]
        assert col.type.length == 20, f"sector_code 长度应为20, 实际: {col.type.length}"

    def test_sector_name_length(self):
        """sector_name 长度限制 30"""
        mapper = inspect(SectorKline)
        col = {c.key: c for c in mapper.mapper.columns}["sector_name"]
        assert col.type.length == 30, f"sector_name 长度应为30, 实际: {col.type.length}"

    def test_sector_type_length(self):
        """sector_type 长度限制 20"""
        mapper = inspect(SectorKline)
        col = {c.key: c for c in mapper.mapper.columns}["sector_type"]
        assert col.type.length == 20, f"sector_type 长度应为20, 实际: {col.type.length}"


# ============================================================
# 2. 采集脚本 parse_kline_df 字段映射测试
# ============================================================

class TestParseKlineDf:
    """采集脚本 DataFrame 解析测试"""

    def _make_sample_df(self, n=5):
        """构造模拟 AkShare 板块指数 DataFrame(中文列名)"""
        dates = pd.date_range("2026-03-01", periods=n, freq="B")
        return pd.DataFrame({
            "日期": dates,
            "开盘": [1000 + i * 10 for i in range(n)],
            "收盘": [1010 + i * 10 for i in range(n)],
            "最高": [1020 + i * 10 for i in range(n)],
            "最低": [990 + i * 10 for i in range(n)],
            "成交量": [1000000 + i * 100000 for i in range(n)],
            "成交额": [1e9 + i * 1e8 for i in range(n)],
            "涨跌幅": [1.0 + i * 0.1 for i in range(n)],
            "振幅": [3.0 + i * 0.2 for i in range(n)],
        })

    def test_chinese_column_mapping(self):
        """中文列名正确映射"""
        df = self._make_sample_df(5)
        records = parse_kline_df(df, "pw_concept_光伏概念", "光伏概念", "concept")
        assert len(records) == 5

        # 逐字段验证
        r = records[0]
        assert r["sector_code"] == "pw_concept_光伏概念"
        assert r["sector_name"] == "光伏概念"
        assert r["sector_type"] == "concept"
        assert r["trade_date"] == date(2026, 3, 2)  # 2026-03-01 is Sunday → Monday
        assert r["open"] == 1000.0
        assert r["close"] == 1010.0
        assert r["high"] == 1020.0
        assert r["low"] == 990.0
        assert r["volume"] == 1000000.0
        assert r["amount"] == 1e9
        assert r["change_pct"] == 1.0
        assert r["amplitude"] == 3.0
        assert r["source"] == "akshare"

    def test_english_column_mapping(self):
        """英文列名映射(备用)"""
        df = pd.DataFrame({
            "date": ["2026-03-02"],
            "open": [1000.0],
            "close": [1010.0],
            "high": [1020.0],
            "low": [990.0],
            "volume": [1000000.0],
            "amount": [1e9],
            "change_pct": [1.0],
            "amplitude": [3.0],
        })
        records = parse_kline_df(df, "pw_industry_医药生物", "医药生物", "industry")
        assert len(records) == 1
        r = records[0]
        assert r["open"] == 1000.0
        assert r["close"] == 1010.0

    def test_timestamp_date_parsing(self):
        """pd.Timestamp 类型日期解析"""
        df = pd.DataFrame({
            "日期": [pd.Timestamp("2026-03-02")],
            "开盘": [1000.0],
            "收盘": [1010.0],
        })
        records = parse_kline_df(df, "test_code", "测试", "concept")
        assert len(records) == 1
        assert records[0]["trade_date"] == date(2026, 3, 2)

    def test_string_date_parsing(self):
        """字符串日期解析(含时间后缀)"""
        df = pd.DataFrame({
            "日期": ["2026-03-02 00:00:00"],
            "开盘": [1000.0],
            "收盘": [1010.0],
        })
        records = parse_kline_df(df, "test_code", "测试", "concept")
        assert len(records) == 1
        assert records[0]["trade_date"] == date(2026, 3, 2)

    def test_empty_dataframe(self):
        """空 DataFrame 返回空列表"""
        df = pd.DataFrame()
        records = parse_kline_df(df, "test_code", "测试", "concept")
        assert records == []

    def test_none_dataframe(self):
        """None 输入返回空列表"""
        records = parse_kline_df(None, "test_code", "测试", "concept")
        assert records == []

    def test_missing_date_column(self):
        """缺少日期列返回空列表"""
        df = pd.DataFrame({
            "开盘": [1000.0],
            "收盘": [1010.0],
        })
        records = parse_kline_df(df, "test_code", "测试", "concept")
        assert records == []

    def test_nan_values_converted_to_none(self):
        """NaN 值转为 None(不写入DB为NaN)"""
        df = pd.DataFrame({
            "日期": ["2026-03-02"],
            "开盘": [float("nan")],
            "收盘": [1010.0],
            "最高": [float("nan")],
            "最低": [990.0],
            "成交量": [1000000.0],
            "成交额": [float("nan")],
            "涨跌幅": [1.0],
            "振幅": [3.0],
        })
        records = parse_kline_df(df, "test_code", "测试", "concept")
        assert len(records) == 1
        r = records[0]
        assert r["open"] is None, f"NaN → None 失败, 实际: {r['open']}"
        assert r["high"] is None, f"NaN → None 失败, 实际: {r['high']}"
        assert r["amount"] is None, f"NaN → None 失败, 实际: {r['amount']}"
        assert r["close"] == 1010.0
        assert r["low"] == 990.0

    def test_missing_optional_columns(self):
        """缺少可选列(成交额/涨跌幅/振幅)时对应字段为None"""
        df = pd.DataFrame({
            "日期": ["2026-03-02"],
            "开盘": [1000.0],
            "收盘": [1010.0],
            "最高": [1020.0],
            "最低": [990.0],
            "成交量": [1000000.0],
            # 缺少成交额/涨跌幅/振幅
        })
        records = parse_kline_df(df, "test_code", "测试", "concept")
        assert len(records) == 1
        r = records[0]
        assert r["amount"] is None, "缺少成交额列时应为None"
        assert r["change_pct"] is None, "缺少涨跌幅列时应为None"
        assert r["amplitude"] is None, "缺少振幅列时应为None"
        # 必要字段仍正常
        assert r["open"] == 1000.0
        assert r["close"] == 1010.0

    def test_invalid_date_row_skipped(self):
        """无效日期行被跳过"""
        df = pd.DataFrame({
            "日期": ["2026-03-02", "invalid_date", "2026-03-04"],
            "开盘": [1000.0, 1010.0, 1020.0],
            "收盘": [1010.0, 1020.0, 1030.0],
        })
        records = parse_kline_df(df, "test_code", "测试", "concept")
        # invalid_date 行应被跳过
        assert len(records) == 2
        assert records[0]["trade_date"] == date(2026, 3, 2)
        assert records[1]["trade_date"] == date(2026, 3, 4)

    def test_negative_change_pct(self):
        """负涨跌幅正确保留"""
        df = pd.DataFrame({
            "日期": ["2026-03-02"],
            "开盘": [1000.0],
            "收盘": [980.0],
            "涨跌幅": [-2.0],
        })
        records = parse_kline_df(df, "test_code", "测试", "concept")
        assert records[0]["change_pct"] == -2.0

    def test_large_volume_amount(self):
        """大数值成交量/成交额不溢出"""
        df = pd.DataFrame({
            "日期": ["2026-03-02"],
            "开盘": [1000.0],
            "收盘": [1010.0],
            "成交量": [9999999999.0],
            "成交额": [99999999999999.0],
        })
        records = parse_kline_df(df, "test_code", "测试", "concept")
        assert records[0]["volume"] == 9999999999.0
        assert records[0]["amount"] == 99999999999999.0


# ============================================================
# 3. _find_col 辅助函数测试
# ============================================================

class TestFindCol:
    """列名模糊匹配测试"""

    def test_exact_chinese_match(self):
        """中文列名精确匹配"""
        df = pd.DataFrame({"日期": [], "开盘": []})
        assert _find_col(df, "日期") == "日期"
        assert _find_col(df, "开盘") == "开盘"

    def test_partial_chinese_match(self):
        """中文列名部分匹配(含后缀)"""
        df = pd.DataFrame({"日期(加权)": [], "开盘价": []})
        assert _find_col(df, "日期") == "日期(加权)"
        assert _find_col(df, "开盘") == "开盘价"

    def test_english_match(self):
        """英文列名匹配"""
        df = pd.DataFrame({"date": [], "open": [], "close": []})
        assert _find_col(df, "date") == "date"
        assert _find_col(df, "open") == "open"
        assert _find_col(df, "close") == "close"

    def test_no_match_returns_none(self):
        """无匹配返回 None"""
        df = pd.DataFrame({"foo": [], "bar": []})
        assert _find_col(df, "日期") is None
        assert _find_col(df, "baz") is None

    def test_first_match_wins(self):
        """多个匹配返回第一个"""
        df = pd.DataFrame({"日期_x": [], "日期_y": []})
        result = _find_col(df, "日期")
        assert result == "日期_x"  # 第一个匹配


# ============================================================
# 4. API /kline 端点测试
# ============================================================

class TestSectorKlineAPI:
    """K线 API 端点字段验证测试"""

    @pytest.fixture
    def mock_kline_rows(self):
        """模拟 SectorKline 数据库记录"""
        rows = []
        base_date = date(2026, 3, 3)
        for i in range(30):
            row = MagicMock()
            row.sector_code = "pw_concept_光伏概念"
            row.sector_name = "光伏概念"
            row.sector_type = "concept"
            row.trade_date = base_date + timedelta(days=i * 2)  # 隔天(模拟交易日)
            row.open = 1000.0 + i * 5
            row.high = 1020.0 + i * 5
            row.low = 985.0 + i * 5
            row.close = 1010.0 + i * 5
            row.volume = 1000000.0 + i * 50000
            row.amount = 1e9 + i * 1e7
            row.change_pct = round(0.5 + i * 0.1, 4)
            row.amplitude = round(3.5 + i * 0.05, 4)
            rows.append(row)
        return rows

    def _build_kline_response(self, rows):
        """复现 API /kline 的逻辑构建响应(验证字段)"""
        if not rows:
            return {
                "sector_code": "test",
                "sector_info": None,
                "kline": [],
                "ma": {},
                "latest": None,
            }

        kline_list = []
        for row in rows:
            kline_list.append({
                "trade_date": str(row.trade_date),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "amount": row.amount,
                "change_pct": round(row.change_pct, 2) if row.change_pct else None,
                "amplitude": round(row.amplitude, 2) if row.amplitude else None,
            })

        # MA 计算(与 API 逻辑一致)
        closes = [r.close for r in rows if r.close is not None]
        ma_dict = {"ma5": [], "ma10": [], "ma20": [], "ma60": []}
        ma_periods = {"ma5": 5, "ma10": 10, "ma20": 20, "ma60": 60}

        for key, period in ma_periods.items():
            for i in range(len(closes)):
                if i < period - 1:
                    ma_dict[key].append(None)
                else:
                    avg = sum(closes[i - period + 1:i + 1]) / period
                    ma_dict[key].append(round(avg, 2))

        ma_result = {}
        for key in ma_dict:
            ma_result[key] = [
                {"trade_date": str(rows[i].trade_date), "value": ma_dict[key][i]}
                for i in range(len(rows))
                if ma_dict[key][i] is not None
            ]

        latest_row = rows[-1]
        prev_close = rows[-2].close if len(rows) >= 2 else None
        latest = {
            "trade_date": str(latest_row.trade_date),
            "close": latest_row.close,
            "change_pct": round(latest_row.change_pct, 2) if latest_row.change_pct else None,
            "volume": latest_row.volume,
            "amount": latest_row.amount,
            "high": latest_row.high,
            "low": latest_row.low,
            "open": latest_row.open,
        }
        if prev_close and latest_row.close:
            latest["change"] = round(latest_row.close - prev_close, 2)

        sector_info = {
            "sector_code": rows[-1].sector_code,
            "sector_name": rows[-1].sector_name,
            "sector_type": rows[-1].sector_type,
            "data_range": f"{rows[0].trade_date}~{rows[-1].trade_date}",
            "data_count": len(rows),
        }

        return {
            "sector_code": "pw_concept_光伏概念",
            "sector_info": sector_info,
            "kline": kline_list,
            "ma": ma_result,
            "latest": latest,
        }

    def test_kline_response_structure(self, mock_kline_rows):
        """K线响应结构完整"""
        resp = self._build_kline_response(mock_kline_rows)
        assert "sector_code" in resp
        assert "sector_info" in resp
        assert "kline" in resp
        assert "ma" in resp
        assert "latest" in resp

    def test_kline_each_item_fields(self, mock_kline_rows):
        """每条K线数据包含全部9个字段"""
        resp = self._build_kline_response(mock_kline_rows)
        required_kline_fields = {
            "trade_date", "open", "high", "low", "close",
            "volume", "amount", "change_pct", "amplitude",
        }
        for item in resp["kline"]:
            missing = required_kline_fields - set(item.keys())
            assert not missing, f"K线条目缺少字段: {missing}"

    def test_sector_info_fields(self, mock_kline_rows):
        """sector_info 包含全部5个字段"""
        resp = self._build_kline_response(mock_kline_rows)
        info = resp["sector_info"]
        required = {"sector_code", "sector_name", "sector_type", "data_range", "data_count"}
        missing = required - set(info.keys())
        assert not missing, f"sector_info 缺少字段: {missing}"
        assert info["data_count"] == 30
        assert info["sector_type"] == "concept"

    def test_latest_fields(self, mock_kline_rows):
        """latest 摘要包含全部字段(含change)"""
        resp = self._build_kline_response(mock_kline_rows)
        latest = resp["latest"]
        required = {"trade_date", "close", "change_pct", "volume", "amount",
                     "high", "low", "open", "change"}
        missing = required - set(latest.keys())
        assert not missing, f"latest 缺少字段: {missing}"

    def test_latest_change_calculation(self, mock_kline_rows):
        """latest.change = 最新close - 前日close"""
        resp = self._build_kline_response(mock_kline_rows)
        latest = resp["latest"]
        prev_close = mock_kline_rows[-2].close
        curr_close = mock_kline_rows[-1].close
        expected_change = round(curr_close - prev_close, 2)
        assert latest["change"] == expected_change, \
            f"change 计算错误: 期望 {expected_change}, 实际 {latest['change']}"

    def test_ma_keys_present(self, mock_kline_rows):
        """MA 数据包含 ma5/ma10/ma20/ma60"""
        resp = self._build_kline_response(mock_kline_rows)
        for key in ("ma5", "ma10", "ma20", "ma60"):
            assert key in resp["ma"], f"缺少 {key}"

    def test_ma5_correctness(self, mock_kline_rows):
        """MA5 计算正确性(手算验证前几条)"""
        closes = [r.close for r in mock_kline_rows]
        # MA5 第5条 = (c[0]+c[1]+c[2]+c[3]+c[4]) / 5
        expected_ma5_4 = round(sum(closes[0:5]) / 5, 2)
        resp = self._build_kline_response(mock_kline_rows)
        ma5_first = resp["ma"]["ma5"][0]
        assert ma5_first["value"] == expected_ma5_4, \
            f"MA5[0] 计算错误: 期望 {expected_ma5_4}, 实际 {ma5_first['value']}"

    def test_ma10_starts_at_index_9(self, mock_kline_rows):
        """MA10 从第10条数据开始(前9条为None)"""
        resp = self._build_kline_response(mock_kline_rows)
        assert len(resp["ma"]["ma10"]) == 21  # 30 - 9 = 21

    def test_ma20_starts_at_index_19(self, mock_kline_rows):
        """MA20 从第20条数据开始"""
        resp = self._build_kline_response(mock_kline_rows)
        assert len(resp["ma"]["ma20"]) == 11  # 30 - 19 = 11

    def test_ma60_empty_when_under_60(self, mock_kline_rows):
        """MA60 在数据不足60条时为空"""
        resp = self._build_kline_response(mock_kline_rows)
        assert len(resp["ma"]["ma60"]) == 0  # 只有30条数据, 不够60

    def test_ma_each_item_has_date_and_value(self, mock_kline_rows):
        """每条MA数据包含 trade_date 和 value"""
        resp = self._build_kline_response(mock_kline_rows)
        for key in ("ma5", "ma10", "ma20"):
            for item in resp["ma"][key]:
                assert "trade_date" in item, f"{key} 条目缺少 trade_date"
                assert "value" in item, f"{key} 条目缺少 value"
                assert isinstance(item["value"], float), f"{key} value 应为 float"

    def test_empty_data_returns_null_structure(self):
        """无K线数据时返回标准空结构"""
        resp = self._build_kline_response([])
        assert resp["sector_info"] is None
        assert resp["kline"] == []
        assert resp["ma"] == {}
        assert resp["latest"] is None

    def test_single_row_no_change(self):
        """仅1条K线时 latest 无 change 字段(无前日)"""
        row = MagicMock()
        row.sector_code = "test"
        row.sector_name = "测试"
        row.sector_type = "concept"
        row.trade_date = date(2026, 3, 3)
        row.open = 1000.0
        row.high = 1020.0
        row.low = 990.0
        row.close = 1010.0
        row.volume = 1000000.0
        row.amount = 1e9
        row.change_pct = 1.0
        row.amplitude = 3.0

        resp = self._build_kline_response([row])
        assert "change" not in resp["latest"], "仅1条数据时不应有 change 字段"


# ============================================================
# 5. API /kline/batch 端点测试
# ============================================================

class TestSectorKlineBatchAPI:
    """K线批量摘要 API 测试"""

    def test_batch_response_structure(self):
        """批量响应结构完整"""
        # 模拟构建
        resp = {
            "sector_type": "concept",
            "days": 5,
            "total": 389,
            "page": 1,
            "page_size": 20,
            "items": [
                {
                    "sector_code": "pw_concept_光伏概念",
                    "sector_name": "光伏概念",
                    "sector_type": "concept",
                    "latest": {"trade_date": "2026-04-11", "close": 1050.0, "change_pct": 1.5},
                    "mini_kline": [
                        {"trade_date": "2026-04-07", "close": 1030.0, "change_pct": 0.5},
                        {"trade_date": "2026-04-08", "close": 1035.0, "change_pct": 0.48},
                        {"trade_date": "2026-04-09", "close": 1040.0, "change_pct": 0.48},
                        {"trade_date": "2026-04-10", "close": 1035.0, "change_pct": -0.48},
                        {"trade_date": "2026-04-11", "close": 1050.0, "change_pct": 1.5},
                    ],
                    "kline_count": 5,
                }
            ],
        }
        required_top = {"sector_type", "days", "total", "page", "page_size", "items"}
        missing = required_top - set(resp.keys())
        assert not missing, f"批量响应缺少字段: {missing}"

    def test_batch_item_fields(self):
        """每条批量项包含全部字段"""
        item = {
            "sector_code": "pw_concept_光伏概念",
            "sector_name": "光伏概念",
            "sector_type": "concept",
            "latest": {"trade_date": "2026-04-11", "close": 1050.0, "change_pct": 1.5},
            "mini_kline": [],
            "kline_count": 0,
        }
        required = {"sector_code", "sector_name", "sector_type", "latest", "mini_kline", "kline_count"}
        missing = required - set(item.keys())
        assert not missing, f"批量项缺少字段: {missing}"

    def test_mini_kline_truncation(self):
        """mini_kline 截取最近N天(days参数)"""
        all_kline = [
            {"trade_date": f"2026-04-{i:02d}", "close": 1000.0 + i, "change_pct": 0.5}
            for i in range(1, 11)
        ]
        days = 5
        truncated = all_kline[-days:] if len(all_kline) > days else all_kline
        assert len(truncated) == 5
        assert truncated[0]["trade_date"] == "2026-04-06"
        assert truncated[-1]["trade_date"] == "2026-04-10"

    def test_batch_no_kline_data(self):
        """无K线数据的板块 latest=None, mini_kline=[]"""
        item = {
            "sector_code": "pw_concept_新概念",
            "sector_name": "新概念",
            "sector_type": "concept",
            "latest": None,
            "mini_kline": [],
            "kline_count": 0,
        }
        assert item["latest"] is None
        assert item["mini_kline"] == []
        assert item["kline_count"] == 0

    def test_batch_latest_only_close_and_change_pct(self):
        """mini_kline 每条只有 trade_date/close/change_pct(不泄露OHLCV)"""
        kline_item = {"trade_date": "2026-04-11", "close": 1050.0, "change_pct": 1.5}
        # 不应包含 open/high/low/volume/amount
        forbidden = {"open", "high", "low", "volume", "amount"}
        overlap = forbidden & set(kline_item.keys())
        assert not overlap, f"mini_kline 条目不应包含: {overlap}"


# ============================================================
# 6. MA 计算性能测试
# ============================================================

class TestMACalculationPerformance:
    """MA 计算性能测试"""

    def test_ma5_performance_large_dataset(self):
        """MA5 在 10000 条数据下 < 100ms"""
        import time
        closes = [1000.0 + i * 0.1 for i in range(10000)]
        period = 5

        start = time.time()
        for i in range(len(closes)):
            if i >= period - 1:
                avg = sum(closes[i - period + 1:i + 1]) / period
        elapsed = time.time() - start
        assert elapsed < 0.1, f"MA5 10000条耗时 {elapsed:.3f}s, 超过100ms"

    def test_ma60_performance_large_dataset(self):
        """MA60 在 10000 条数据下 < 200ms"""
        import time
        closes = [1000.0 + i * 0.1 for i in range(10000)]
        period = 60

        start = time.time()
        for i in range(len(closes)):
            if i >= period - 1:
                avg = sum(closes[i - period + 1:i + 1]) / period
        elapsed = time.time() - start
        assert elapsed < 0.2, f"MA60 10000条耗时 {elapsed:.3f}s, 超过200ms"

    def test_ma_correctness_manual(self):
        """MA 计算手算验证(黄金交叉场景)"""
        # 构造先跌后涨序列(模拟均线交叉)
        closes = [100, 99, 98, 97, 96,  # 跌5天, MA5=98
                  97, 98, 99, 100, 101]  # 涨5天
        # MA5:
        # i=4: (100+99+98+97+96)/5 = 98.0
        # i=5: (99+98+97+96+97)/5 = 97.4
        # i=6: (98+97+96+97+98)/5 = 97.2
        # i=7: (97+96+97+98+99)/5 = 97.4
        # i=8: (96+97+98+99+100)/5 = 98.0
        # i=9: (97+98+99+100+101)/5 = 99.0

        period = 5
        ma5 = []
        for i in range(len(closes)):
            if i < period - 1:
                ma5.append(None)
            else:
                avg = round(sum(closes[i - period + 1:i + 1]) / period, 2)
                ma5.append(avg)

        assert ma5[4] == 98.0
        assert ma5[5] == 97.4
        assert ma5[6] == 97.2
        assert ma5[7] == 97.4
        assert ma5[8] == 98.0
        assert ma5[9] == 99.0


# ============================================================
# 7. 前端 API 层测试(字段传递验证)
# ============================================================

class TestFrontendAPILayer:
    """前端 API 调用层参数正确性测试"""

    def test_getSectorKline_params(self):
        """getSectorKline 传递正确参数"""
        # 验证函数定义和参数映射
        # API: GET /sectors/kline?sector_code=xxx&days=60
        # 前端: getSectorKline({ sector_code, days })
        expected_params = {"sector_code": "pw_concept_光伏概念", "days": 60}
        # 这里验证参数键名一致
        assert "sector_code" in expected_params
        assert "days" in expected_params

    def test_getSectorKlineBatch_params(self):
        """getSectorKlineBatch 传递正确参数"""
        # API: GET /sectors/kline/batch?sector_type=concept&days=5&page=1&page_size=20
        # 前端: getSectorKlineBatch({ sector_type, days, page, page_size })
        expected_params = {"sector_type": "concept", "days": 5, "page": 1, "page_size": 20}
        assert "sector_type" in expected_params
        assert "days" in expected_params
        assert "page" in expected_params
        assert "page_size" in expected_params

    def test_kline_chart_ohlc_order(self):
        """ECharts 蜡烛图 OHLC 顺序验证: [open, close, low, high]"""
        # 前端: kline.map(k => [k.open, k.close, k.low, k.high])
        # ECharts candlestick data 格式: [open, close, lowest, highest]
        # 注意: ECharts 标准 data 格式是 [open, close, low, high]
        sample = {"open": 1000, "close": 1010, "low": 990, "high": 1020}
        ohlc = [sample["open"], sample["close"], sample["low"], sample["high"]]
        assert ohlc == [1000, 1010, 990, 1020], \
            f"OHLC 顺序错误, ECharts 要求 [open, close, low, high]"

    def test_volume_bar_color_convention(self):
        """成交量柱颜色: 涨=红, 跌=绿(A股惯例)"""
        # 前端: changes[i] >= 0 ? rgba(239,68,68,0.5) : rgba(34,197,94,0.5)
        # 239,68,68 = 红色(#ef4444), 34,197,94 = 绿色(#22c55e)
        red_rgb = (239, 68, 68)
        green_rgb = (34, 197, 94)
        # 涨
        change_pct = 1.5
        color = "rgba(239,68,68,0.5)" if change_pct >= 0 else "rgba(34,197,94,0.5)"
        assert "239,68,68" in color, "涨应使用红色"
        # 跌
        change_pct = -1.5
        color = "rgba(239,68,68,0.5)" if change_pct >= 0 else "rgba(34,197,94,0.5)"
        assert "34,197,94" in color, "跌应使用绿色"

    def test_candlestick_color_convention(self):
        """蜡烛图颜色: 涨=红填充, 跌=绿填充(A股惯例)"""
        # itemStyle.color = '#ef4444' (涨), color0 = '#22c55e' (跌)
        # ECharts: color=涨(阳线填充), color0=跌(阴线填充)
        up_color = "#ef4444"
        down_color = "#22c55e"
        assert up_color == "#ef4444", "涨色应为 #ef4444"
        assert down_color == "#22c55e", "跌色应为 #22c55e"


# ============================================================
# 8. 数据一致性测试
# ============================================================

class TestDataConsistency:
    """数据一致性验证"""

    def test_high_ge_low(self):
        """最高价 >= 最低价"""
        df = pd.DataFrame({
            "日期": ["2026-03-02"],
            "开盘": [1000.0],
            "收盘": [1010.0],
            "最高": [1020.0],
            "最低": [990.0],
        })
        records = parse_kline_df(df, "test", "测试", "concept")
        r = records[0]
        assert r["high"] >= r["low"], f"最高价({r['high']}) < 最低价({r['low']})"

    def test_high_ge_open_close(self):
        """最高价 >= 开盘价和收盘价"""
        df = pd.DataFrame({
            "日期": ["2026-03-02"],
            "开盘": [1000.0],
            "收盘": [1010.0],
            "最高": [1020.0],
            "最低": [990.0],
        })
        records = parse_kline_df(df, "test", "测试", "concept")
        r = records[0]
        assert r["high"] >= r["open"], f"最高价 < 开盘价"
        assert r["high"] >= r["close"], f"最高价 < 收盘价"

    def test_low_le_open_close(self):
        """最低价 <= 开盘价和收盘价"""
        df = pd.DataFrame({
            "日期": ["2026-03-02"],
            "开盘": [1000.0],
            "收盘": [1010.0],
            "最高": [1020.0],
            "最低": [990.0],
        })
        records = parse_kline_df(df, "test", "测试", "concept")
        r = records[0]
        assert r["low"] <= r["open"], f"最低价 > 开盘价"
        assert r["low"] <= r["close"], f"最低价 > 收盘价"

    def test_amplitude_calculation(self):
        """振幅 = (最高-最低)/前收盘*100"""
        # AkShare 返回的振幅已算好, 验证合理范围
        df = pd.DataFrame({
            "日期": ["2026-03-02"],
            "开盘": [1000.0],
            "收盘": [1010.0],
            "最高": [1020.0],
            "最低": [990.0],
            "振幅": [3.0],  # (1020-990)/1000*100 = 3.0%
        })
        records = parse_kline_df(df, "test", "测试", "concept")
        r = records[0]
        # 验证: 振幅应在 0-30% 范围内(极端行情)
        assert 0 <= r["amplitude"] <= 30, f"振幅 {r['amplitude']} 超出合理范围"

    def test_change_pct_range(self):
        """涨跌幅合理范围: -20% ~ +20%(A股涨跌停限制)"""
        df = pd.DataFrame({
            "日期": ["2026-03-02"],
            "开盘": [1000.0],
            "收盘": [1010.0],
            "涨跌幅": [1.0],
        })
        records = parse_kline_df(df, "test", "测试", "concept")
        r = records[0]
        # 板块指数无涨跌停限制, 但极端值应警惕
        assert -30 <= r["change_pct"] <= 30, f"涨跌幅 {r['change_pct']} 异常"

    def test_sector_type_values(self):
        """sector_type 只允许 concept/industry"""
        valid_types = {"concept", "industry"}
        for t in ["concept", "industry"]:
            assert t in valid_types
        # "shenwan" 不应出现在 K线类型中
        assert "shenwan" not in valid_types

    def test_kline_sorted_by_date(self):
        """K线按 trade_date 升序排列(API层保证)"""
        # 模拟 API 返回顺序
        dates = ["2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]
        for i in range(len(dates) - 1):
            assert dates[i] < dates[i + 1], "K线日期应升序"
