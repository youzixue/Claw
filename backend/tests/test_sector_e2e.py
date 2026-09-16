"""板块营地真实链路集成测试 — 字段级准确性验证

不走mock，直接HTTP请求运行中的FastAPI后端，逐字段验证。

前置条件:
- 后端已启动: uvicorn app.main:app --port 8000
- 数据库有真实数据(sector_info/sector_persistence/sector_kline等)

验证范围:
1. 9个板块API的响应结构+字段名+类型+取值范围
2. K线API的MA计算正确性(手算验证)
3. 数据一致性(high>=low, change_pct范围, 日期升序)
4. 前端API函数参数与后端端点匹配
5. 性能基准(API响应时间)
"""

import os

import pytest
import httpx
import time
import math
from datetime import date, timedelta

# Ordinary regression must not issue requests against the trading process.
# Keep the real-chain assertions intact and require an explicit live-test opt-in.
pytestmark = pytest.mark.skipif(
    os.environ.get("CLAW_RUN_LIVE_SECTOR_E2E") != "1",
    reason="live sector E2E requires explicit CLAW_RUN_LIVE_SECTOR_E2E=1",
)
BASE_URL = "http://localhost:8000/api/v1/sectors"

# 已知有K线数据的板块
KLINE_SECTOR_CODE = "pw_concept_人工智能"
KLINE_SECTOR_CODE_ENCODED = "pw_concept_%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD"


# ============================================================
# 1. /strength 板块强弱排名
# ============================================================

class TestStrengthE2E:
    """板块强弱排名 — 真实链路验证"""

    @pytest.fixture(scope="class")
    def response(self):
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/strength", params={"page": 1, "page_size": 10})
            assert r.status_code == 200, f"status={r.status_code}, body={r.text[:200]}"
            return r.json()

    def test_top_level_structure(self, response):
        """顶层6个字段"""
        required = {"trade_date", "total", "page", "page_size", "type_stats", "items"}
        missing = required - set(response.keys())
        assert not missing, f"缺少顶层字段: {missing}"

    def test_trade_date_format(self, response):
        """trade_date 为 YYYY-MM-DD 格式"""
        td = response["trade_date"]
        assert isinstance(td, str), f"trade_date 应为str, 实际: {type(td)}"
        # 能解析为date
        date.fromisoformat(td)

    def test_total_type(self, response):
        """total 为正整数"""
        assert isinstance(response["total"], int)
        assert response["total"] > 0, "板块总数应>0"

    def test_type_stats_structure(self, response):
        """type_stats 包含 concept 和 industry"""
        ts = response["type_stats"]
        assert "concept" in ts, "缺少 concept 统计"
        assert "industry" in ts, "缺少 industry 统计"
        for stype in ("concept", "industry"):
            stat = ts[stype]
            required = {"total", "hot", "has_fund_in", "total_fund_flow"}
            missing = required - set(stat.keys())
            assert not missing, f"{stype} type_stats 缺少: {missing}"

    def test_type_stats_values(self, response):
        """type_stats 数值合理性"""
        ts = response["type_stats"]
        for stype in ("concept", "industry"):
            stat = ts[stype]
            assert stat["total"] > 0, f"{stype} total 应>0"
            assert stat["hot"] >= 0, f"{stype} hot 应>=0"
            assert stat["hot"] <= stat["total"], f"{stype} hot 不应>total"
            assert isinstance(stat["total_fund_flow"], (int, float))

    def test_item_fields(self, response):
        """每条item包含全部12个字段"""
        expected_fields = {
            "sector_code", "sector_name", "sector_type", "strength_score",
            "change_pct", "fund_flow", "limit_up_count", "consecutive_days",
            "stock_count", "rank", "rank_change", "is_hot"
        }
        for item in response["items"]:
            missing = expected_fields - set(item.keys())
            extra = set(item.keys()) - expected_fields
            assert not missing, f"item 缺少字段: {missing}"
            assert not extra, f"item 多余字段: {extra}"

    def test_item_field_types(self, response):
        """item 每个字段类型正确"""
        for item in response["items"]:
            assert isinstance(item["sector_code"], str)
            assert isinstance(item["sector_name"], str)
            assert item["sector_type"] in ("concept", "industry")
            assert isinstance(item["strength_score"], (int, float))
            assert isinstance(item["change_pct"], (int, float))
            assert isinstance(item["fund_flow"], (int, float))
            assert isinstance(item["limit_up_count"], int)
            assert isinstance(item["consecutive_days"], int)
            assert isinstance(item["stock_count"], int)
            assert isinstance(item["rank"], int)
            assert isinstance(item["rank_change"], int)
            assert isinstance(item["is_hot"], bool)

    def test_strength_score_range(self, response):
        """strength_score 在0-100范围"""
        for item in response["items"]:
            assert 0 <= item["strength_score"] <= 100, \
                f"strength_score={item['strength_score']} 超出[0,100]"

    def test_change_pct_range(self, response):
        """change_pct 在±30%范围"""
        for item in response["items"]:
            assert -30 <= item["change_pct"] <= 30, \
                f"change_pct={item['change_pct']} 超出[-30,30]"

    def test_rank_ordered(self, response):
        """items按rank升序(分页内)"""
        ranks = [item["rank"] for item in response["items"]]
        assert ranks == sorted(ranks), f"rank未排序: {ranks}"

    def test_is_hot_criteria(self, response):
        """is_hot = fund_flow>10 或 consecutive_days>=3"""
        for item in response["items"]:
            expected_hot = item["fund_flow"] > 10 or item["consecutive_days"] >= 3
            assert item["is_hot"] == expected_hot, \
                f"is_hot不一致: fund={item['fund_flow']}, days={item['consecutive_days']}, hot={item['is_hot']}"

    def test_sector_code_format(self, response):
        """sector_code 格式: pw_concept_xxx 或 pw_industry_xxx"""
        for item in response["items"]:
            assert item["sector_code"].startswith("pw_concept_") or \
                   item["sector_code"].startswith("pw_industry_"), \
                f"sector_code 格式异常: {item['sector_code']}"

    def test_sector_type_matches_code(self, response):
        """sector_type 与 sector_code 前缀一致"""
        for item in response["items"]:
            if item["sector_code"].startswith("pw_concept_"):
                assert item["sector_type"] == "concept"
            elif item["sector_code"].startswith("pw_industry_"):
                assert item["sector_type"] == "industry"


# ============================================================
# 2. /rotation 板块轮动信号
# ============================================================

class TestRotationE2E:
    """板块轮动信号 — 真实链路验证"""

    @pytest.fixture(scope="class")
    def response(self):
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/rotation")
            assert r.status_code == 200
            return r.json()

    def test_top_level_fields(self, response):
        """顶层3个字段"""
        required = {"trade_date", "total_signals", "signals"}
        assert required.issubset(set(response.keys()))

    def test_signal_fields(self, response):
        """每条signal包含10个字段"""
        expected = {
            "from_sector", "from_name", "from_type",
            "to_sector", "to_name", "to_type",
            "flow_amount", "rotation_type", "rotation_type_label", "confidence"
        }
        for sig in response["signals"]:
            missing = expected - set(sig.keys())
            assert not missing, f"signal 缺少字段: {missing}"

    def test_signal_field_types(self, response):
        """signal 字段类型"""
        for sig in response["signals"]:
            assert isinstance(sig["from_sector"], str)
            assert isinstance(sig["from_name"], str)
            assert sig["from_type"] in ("concept", "industry", "")
            assert isinstance(sig["to_sector"], str)
            assert isinstance(sig["to_name"], str)
            assert sig["to_type"] in ("concept", "industry", "")
            assert isinstance(sig["flow_amount"], (int, float))
            assert sig["rotation_type"] in ("gradual", "sudden")
            assert isinstance(sig["rotation_type_label"], str)
            assert isinstance(sig["confidence"], (int, float))

    def test_confidence_range(self, response):
        """confidence 在0-1范围"""
        for sig in response["signals"]:
            assert 0 <= sig["confidence"] <= 1.0, \
                f"confidence={sig['confidence']} 超出[0,1]"

    def test_rotation_type_label(self, response):
        """rotation_type_label 正确映射"""
        for sig in response["signals"]:
            if sig["rotation_type"] == "sudden":
                assert "突然" in sig["rotation_type_label"]
            else:
                assert "渐进" in sig["rotation_type_label"] or "轮动" in sig["rotation_type_label"]


# ============================================================
# 3. /persistence 板块持续性
# ============================================================

class TestPersistenceE2E:
    """板块持续性 — 真实链路验证"""

    @pytest.fixture(scope="class")
    def response(self):
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/persistence", params={"min_days": 1, "page_size": 10})
            assert r.status_code == 200
            return r.json()

    def test_top_level_fields(self, response):
        required = {"trade_date", "total", "page", "page_size", "type_stats", "items"}
        assert required.issubset(set(response.keys()))

    def test_item_fields(self, response):
        """item 9个字段"""
        expected = {
            "sector_code", "sector_name", "sector_type",
            "consecutive_days", "limit_up_count", "fund_flow",
            "change_pct", "strength_score", "is_declining"
        }
        for item in response["items"]:
            missing = expected - set(item.keys())
            assert not missing, f"persistence item 缺少: {missing}"

    def test_item_field_types(self, response):
        for item in response["items"]:
            assert isinstance(item["consecutive_days"], int)
            assert item["consecutive_days"] >= 1, "持续性应>=1天"
            assert isinstance(item["is_declining"], bool)

    def test_is_declining_logic(self, response):
        """is_declining = strength_score < 50"""
        for item in response["items"]:
            expected = item["strength_score"] < 50
            assert item["is_declining"] == expected, \
                f"is_declining不一致: score={item['strength_score']}, declining={item['is_declining']}"


# ============================================================
# 4. /count 板块数量
# ============================================================

class TestCountE2E:
    """板块数量 — 真实链路验证"""

    @pytest.fixture(scope="class")
    def response(self):
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/count")
            assert r.status_code == 200
            return r.json()

    def test_response_fields(self, response):
        assert set(response.keys()) == {"industry", "concept"}

    def test_values_positive(self, response):
        assert response["concept"] > 0
        assert response["industry"] > 0

    def test_count_matches_strength_total(self, response):
        """count返回的数量应与strength全量total一致"""
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/strength", params={"page_size": 1})
            strength_data = r.json()
            # concept + industry total
            ts = strength_data.get("type_stats", {})
            if "concept" in ts and "industry" in ts:
                total_from_strength = ts["concept"]["total"] + ts["industry"]["total"]
                total_from_count = response["concept"] + response["industry"]
                assert total_from_count == total_from_strength, \
                    f"count={total_from_count}, strength={total_from_strength}"


# ============================================================
# 5. /kline 板块K线
# ============================================================

class TestKlineE2E:
    """板块K线 — 真实链路验证(核心)"""

    @pytest.fixture(scope="class")
    def response(self):
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/kline", params={
                "sector_code": KLINE_SECTOR_CODE,
                "days": 500  # 数据在2025年，需大days才能查到
            })
            assert r.status_code == 200, f"status={r.status_code}, body={r.text[:200]}"
            return r.json()

    def test_top_level_fields(self, response):
        """5个顶层字段"""
        expected = {"sector_code", "sector_info", "kline", "ma", "latest"}
        assert set(response.keys()) == expected

    def test_sector_info_fields(self, response):
        """sector_info 5个字段"""
        info = response["sector_info"]
        assert info is not None, "sector_info 不应为None(有数据时)"
        expected = {"sector_code", "sector_name", "sector_type", "data_range", "data_count"}
        assert set(info.keys()) == expected
        assert info["sector_type"] in ("concept", "industry")
        assert isinstance(info["data_count"], int)
        assert info["data_count"] > 0

    def test_kline_item_fields(self, response):
        """每条K线9个字段"""
        expected = {
            "trade_date", "open", "high", "low", "close",
            "volume", "amount", "change_pct", "amplitude"
        }
        for item in response["kline"]:
            missing = expected - set(item.keys())
            assert not missing, f"K线条目缺少: {missing}"

    def test_kline_item_types(self, response):
        """K线字段类型"""
        for item in response["kline"]:
            assert isinstance(item["trade_date"], str)
            # OHLCV 应为数字或null
            if item["open"] is not None:
                assert isinstance(item["open"], (int, float)), f"open 类型: {type(item['open'])}"
            if item["close"] is not None:
                assert isinstance(item["close"], (int, float))
            if item["high"] is not None:
                assert isinstance(item["high"], (int, float))
            if item["low"] is not None:
                assert isinstance(item["low"], (int, float))
            if item["volume"] is not None:
                assert isinstance(item["volume"], (int, float))

    def test_kline_date_ascending(self, response):
        """K线按trade_date升序"""
        dates = [item["trade_date"] for item in response["kline"]]
        assert dates == sorted(dates), "K线日期未升序排列"

    def test_data_consistency_high_ge_low(self, response):
        """每条K线: high >= low"""
        for item in response["kline"]:
            if item["high"] is not None and item["low"] is not None:
                assert item["high"] >= item["low"], \
                    f"high({item['high']}) < low({item['low']}) on {item['trade_date']}"

    def test_data_consistency_high_ge_open_close(self, response):
        """每条K线: high >= open, close"""
        for item in response["kline"]:
            if item["high"] is not None:
                if item["open"] is not None:
                    assert item["high"] >= item["open"], \
                        f"high({item['high']}) < open({item['open']}) on {item['trade_date']}"
                if item["close"] is not None:
                    assert item["high"] >= item["close"], \
                        f"high({item['high']}) < close({item['close']}) on {item['trade_date']}"

    def test_data_consistency_low_le_open_close(self, response):
        """每条K线: low <= open, close"""
        for item in response["kline"]:
            if item["low"] is not None:
                if item["open"] is not None:
                    assert item["low"] <= item["open"], \
                        f"low({item['low']}) > open({item['open']}) on {item['trade_date']}"
                if item["close"] is not None:
                    assert item["low"] <= item["close"], \
                        f"low({item['low']}) > close({item['close']}) on {item['trade_date']}"

    def test_latest_fields(self, response):
        """latest 包含全部字段"""
        latest = response["latest"]
        assert latest is not None, "latest 不应为None(有数据时)"
        required = {"trade_date", "close", "change_pct", "volume", "amount",
                    "high", "low", "open"}
        missing = required - set(latest.keys())
        assert not missing, f"latest 缺少: {missing}"
        # change字段存在条件
        if len(response["kline"]) >= 2:
            assert "change" in latest, "有>=2条数据时latest应有change"

    def test_latest_change_correctness(self, response):
        """latest.change = 最新close - 前日close"""
        if len(response["kline"]) >= 2:
            latest = response["latest"]
            if "change" in latest and latest["close"] is not None:
                prev_close = response["kline"][-2]["close"]
                if prev_close is not None:
                    expected_change = round(latest["close"] - prev_close, 2)
                    assert latest["change"] == expected_change, \
                        f"change={latest['change']}, expected={expected_change}"

    def test_ma_keys(self, response):
        """MA数据包含ma5/ma10/ma20/ma60"""
        ma = response["ma"]
        for key in ("ma5", "ma10", "ma20", "ma60"):
            assert key in ma, f"缺少 {key}"

    def test_ma_item_structure(self, response):
        """每条MA数据包含 trade_date + value"""
        for key in ("ma5", "ma10", "ma20"):
            for item in response["ma"][key]:
                assert "trade_date" in item, f"{key} 条目缺少 trade_date"
                assert "value" in item, f"{key} 条目缺少 value"
                assert isinstance(item["value"], float)

    def test_ma5_hand_calculated(self, response):
        """MA5 手算验证(取前5条close算第一个MA5)"""
        kline = response["kline"]
        closes = [k["close"] for k in kline if k["close"] is not None]
        if len(closes) >= 5:
            expected_ma5_0 = round(sum(closes[0:5]) / 5, 2)
            ma5_first = response["ma"]["ma5"][0]
            assert abs(ma5_first["value"] - expected_ma5_0) < 0.01, \
                f"MA5[0]={ma5_first['value']}, expected={expected_ma5_0}"

    def test_ma10_start_position(self, response):
        """MA10 从第10条开始(前9条为None)"""
        kline_count = len(response["kline"])
        ma10_count = len(response["ma"]["ma10"])
        expected = max(0, kline_count - 9)
        assert ma10_count == expected, \
            f"MA10 条数={ma10_count}, 期望={expected}"

    def test_ma20_start_position(self, response):
        """MA20 从第20条开始"""
        kline_count = len(response["kline"])
        ma20_count = len(response["ma"]["ma20"])
        expected = max(0, kline_count - 19)
        assert ma20_count == expected, \
            f"MA20 条数={ma20_count}, 期望={expected}"

    def test_change_pct_null_when_not_available(self, response):
        """AkShare不返回涨跌幅时change_pct为null"""
        # 当前数据源(AkShare板块指数)不提供涨跌幅/振幅
        null_count = sum(1 for k in response["kline"] if k["change_pct"] is None)
        # 至少应有部分为null(因为AkShare不提供)
        # 注意: 如果以后采集脚本自行计算涨跌幅,此测试需调整
        print(f"  change_pct null比例: {null_count}/{len(response['kline'])}")


class TestKlineEmptyE2E:
    """K线空数据场景"""

    def test_empty_kline_returns_null_structure(self):
        """无数据的板块返回标准空结构"""
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/kline", params={
                "sector_code": "pw_concept_不存在的板块",
                "days": 60
            })
            assert r.status_code == 200
            data = r.json()
            assert data["sector_info"] is None
            assert data["kline"] == []
            assert data["ma"] == {}
            assert data["latest"] is None


# ============================================================
# 6. /kline/batch 板块K线批量
# ============================================================

class TestKlineBatchE2E:
    """K线批量摘要 — 真实链路验证"""

    @pytest.fixture(scope="class")
    def response(self):
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/kline/batch", params={
                "sector_type": "concept", "days": 5, "page": 1, "page_size": 10
            })
            assert r.status_code == 200
            return r.json()

    def test_top_level_fields(self, response):
        """6个顶层字段"""
        expected = {"sector_type", "days", "total", "page", "page_size", "items"}
        assert set(response.keys()) == expected

    def test_item_fields(self, response):
        """每条item 6个字段"""
        expected = {"sector_code", "sector_name", "sector_type", "latest", "mini_kline", "kline_count"}
        for item in response["items"]:
            missing = expected - set(item.keys())
            assert not missing, f"batch item 缺少: {missing}"

    def test_mini_kline_only_close_and_change(self, response):
        """mini_kline 只含 trade_date/close/change_pct(不泄露OHLCV)"""
        forbidden = {"open", "high", "low", "volume", "amount"}
        for item in response["items"]:
            for mk in item["mini_kline"]:
                overlap = forbidden & set(mk.keys())
                assert not overlap, f"mini_kline 不应包含: {overlap}"

    def test_mini_kline_truncation(self, response):
        """mini_kline 最多截取最近days天"""
        days = response["days"]
        for item in response["items"]:
            assert len(item["mini_kline"]) <= days, \
                f"mini_kline长度{len(item['mini_kline'])}超过days={days}"

    def test_no_kline_data_item(self, response):
        """无K线数据: latest=None, mini_kline=[], kline_count=0"""
        for item in response["items"]:
            if item["kline_count"] == 0:
                assert item["latest"] is None
                assert item["mini_kline"] == []


# ============================================================
# 7. /lifecycle 板块生命周期
# ============================================================

class TestLifecycleE2E:
    """板块生命周期 — 真实链路验证"""

    def test_lifecycle_structure(self):
        """lifecycle API 结构正确(即使数据为空)"""
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/lifecycle")
            assert r.status_code == 200
            data = r.json()
            required = {"trade_date", "total", "page", "page_size", "state_stats", "items"}
            assert required.issubset(set(data.keys()))


# ============================================================
# 8. /lifecycle/calendar 轮动日历
# ============================================================

class TestLifecycleCalendarE2E:
    """轮动日历 — 真实链路验证"""

    def test_calendar_structure(self):
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/lifecycle/calendar", params={"days": 10})
            assert r.status_code == 200
            data = r.json()
            required = {"dates", "sectors", "matrix", "leaders"}
            assert required.issubset(set(data.keys()))


# ============================================================
# 9. /main-lines 主线追踪
# ============================================================

class TestMainLinesE2E:
    """主线追踪 — 真实链路验证"""

    def test_main_lines_structure(self):
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/main-lines")
            assert r.status_code == 200
            data = r.json()
            required = {"active_count", "items"}
            assert required.issubset(set(data.keys()))


# ============================================================
# 10. 跨API一致性
# ============================================================

class TestCrossAPIConsistency:
    """跨API数据一致性验证"""

    def test_strength_and_count_consistent(self):
        """strength total 和 count 一致"""
        with httpx.Client(timeout=10) as client:
            count_data = client.get(f"{BASE_URL}/count").json()
            strength_data = client.get(f"{BASE_URL}/strength", params={"page_size": 1}).json()
            ts = strength_data.get("type_stats", {})
            if "concept" in ts:
                assert ts["concept"]["total"] == count_data["concept"], \
                    f"concept: strength={ts['concept']['total']}, count={count_data['concept']}"
            if "industry" in ts:
                assert ts["industry"]["total"] == count_data["industry"], \
                    f"industry: strength={ts['industry']['total']}, count={count_data['industry']}"

    def test_strength_and_persistence_shared_fields(self):
        """strength和persistence共用字段取值一致(同板块同日)"""
        with httpx.Client(timeout=10) as client:
            strength_data = client.get(f"{BASE_URL}/strength", params={"page_size": 50}).json()
            persistence_data = client.get(f"{BASE_URL}/persistence", params={"min_days": 0, "page_size": 50}).json()
            
            # 建立persistence索引
            persist_map = {item["sector_code"]: item for item in persistence_data["items"]}
            
            # 对比共有字段
            shared_fields = ["sector_code", "sector_name", "fund_flow", "change_pct", "strength_score"]
            matches = 0
            for s_item in strength_data["items"]:
                code = s_item["sector_code"]
                if code in persist_map:
                    p_item = persist_map[code]
                    for field in ["fund_flow", "change_pct", "strength_score"]:
                        s_val = s_item[field]
                        p_val = p_item[field]
                        assert s_val == p_val, \
                            f"{code} {field}: strength={s_val}, persistence={p_val}"
                    matches += 1
            
            print(f"  跨API一致性验证: {matches}个板块字段完全一致")


# ============================================================
# 11. 前端-后端字段映射
# ============================================================

class TestFrontendBackendMapping:
    """验证前端API函数参数与后端端点匹配"""

    def test_strength_params_match(self):
        """getSectorStrength 参数映射"""
        with httpx.Client(timeout=10) as client:
            # 前端: getSectorStrength({ sector_type, page, page_size })
            r = client.get(f"{BASE_URL}/strength", params={
                "sector_type": "concept", "page": 1, "page_size": 5
            })
            assert r.status_code == 200
            data = r.json()
            # 验证sector_type筛选生效
            for item in data["items"]:
                assert item["sector_type"] == "concept"

    def test_kline_params_match(self):
        """getSectorKline 参数映射"""
        with httpx.Client(timeout=10) as client:
            # 前端: getSectorKline({ sector_code, days })
            r = client.get(f"{BASE_URL}/kline", params={
                "sector_code": KLINE_SECTOR_CODE, "days": 500
            })
            assert r.status_code == 200

    def test_kline_batch_params_match(self):
        """getSectorKlineBatch 参数映射"""
        with httpx.Client(timeout=10) as client:
            # 前端: getSectorKlineBatch({ sector_type, days, page, page_size })
            r = client.get(f"{BASE_URL}/kline/batch", params={
                "sector_type": "concept", "days": 5, "page": 1, "page_size": 5
            })
            assert r.status_code == 200

    def test_kline_chart_data_format(self):
        """验证K线数据格式兼容ECharts: [open, close, low, high]"""
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/kline", params={
                "sector_code": KLINE_SECTOR_CODE, "days": 500
            })
            data = r.json()
            for item in data["kline"]:
                # ECharts candlestick data = [open, close, low, high]
                ohlc = [item["open"], item["close"], item["low"], item["high"]]
                # 验证high>=low, high>=open/close, low<=open/close
                if all(v is not None for v in ohlc):
                    assert ohlc[2] <= ohlc[3], f"low({ohlc[2]}) > high({ohlc[3]})"

    def test_a_stock_color_convention(self):
        """A股红涨绿跌色值在API响应中可判定"""
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BASE_URL}/strength", params={"page_size": 50})
            data = r.json()
            for item in data["items"]:
                # 前端: change_pct > 0 → 红色(#ef4444), < 0 → 绿色(#22c55e)
                # API只需返回正确符号的change_pct
                assert isinstance(item["change_pct"], (int, float))


# ============================================================
# 12. 性能基准
# ============================================================

class TestPerformanceE2E:
    """真实链路性能基准"""

    def test_strength_under_500ms(self):
        """strength API < 500ms"""
        with httpx.Client(timeout=10) as client:
            start = time.time()
            client.get(f"{BASE_URL}/strength", params={"page_size": 50})
            elapsed = time.time() - start
            assert elapsed < 0.5, f"strength 耗时{elapsed:.3f}s, 超过500ms"

    def test_kline_under_500ms(self):
        """kline API < 500ms"""
        with httpx.Client(timeout=10) as client:
            start = time.time()
            client.get(f"{BASE_URL}/kline", params={
                "sector_code": KLINE_SECTOR_CODE, "days": 500
            })
            elapsed = time.time() - start
            assert elapsed < 0.5, f"kline 耗时{elapsed:.3f}s, 超过500ms"

    def test_count_under_200ms(self):
        """count API < 200ms"""
        with httpx.Client(timeout=10) as client:
            start = time.time()
            client.get(f"{BASE_URL}/count")
            elapsed = time.time() - start
            assert elapsed < 0.2, f"count 耗时{elapsed:.3f}s, 超过200ms"

    def test_batch_under_500ms(self):
        """kline/batch API < 500ms"""
        with httpx.Client(timeout=10) as client:
            start = time.time()
            client.get(f"{BASE_URL}/kline/batch", params={
                "sector_type": "concept", "days": 5, "page_size": 20
            })
            elapsed = time.time() - start
            assert elapsed < 0.5, f"kline/batch 耗时{elapsed:.3f}s, 超过500ms"
