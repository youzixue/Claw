"""因子引擎测试 — 48因子注册+计算正确性"""

import numpy as np
import pandas as pd
import pytest

from app.factors.base import FactorRegistry, FactorEngine, FactorCategory, FactorResult


class TestFactorRegistry:
    """因子注册表测试"""

    def test_registry_not_empty(self):
        """注册表不为空(因子模块导入后应有48个)"""
        assert FactorRegistry.count() > 0

    def test_registry_has_all_categories(self):
        """10个分类都有因子"""
        summary = FactorRegistry.summary()
        for cat in FactorCategory:
            assert cat.value in summary, f"缺少分类: {cat.value}"
            assert summary[cat.value] > 0, f"分类 {cat.value} 无因子"

    def test_get_factor_by_name(self):
        """按名获取因子"""
        factor = FactorRegistry.get("ma5_bias")
        assert factor is not None
        assert factor.factor_name == "ma5_bias"

    def test_get_nonexistent_factor(self):
        """获取不存在的因子返回None"""
        assert FactorRegistry.get("nonexistent_factor") is None

    def test_get_by_category(self):
        """按分类获取因子"""
        tech_factors = FactorRegistry.get_by_category(FactorCategory.TECHNICAL)
        assert len(tech_factors) >= 8  # 至少8个技术因子

    def test_all_factors_have_required_attrs(self):
        """所有因子都有必要属性"""
        for name, factor in FactorRegistry.all_factors().items():
            assert factor.factor_name, f"因子缺少factor_name: {name}"
            assert isinstance(factor.category, FactorCategory), f"因子category类型错误: {name}"
            assert factor.direction in (1, -1), f"因子direction错误: {name}"
            assert factor.description, f"因子缺少description: {name}"


class TestTechnicalFactors:
    """技术因子计算测试"""

    @pytest.fixture
    def sample_df(self):
        """模拟20日行情数据"""
        np.random.seed(42)
        n = 30
        dates = pd.date_range("2026-03-01", periods=n, freq="B")
        base = 100
        returns = np.random.normal(0.002, 0.02, n)
        close = base * np.cumprod(1 + returns)

        high = close * (1 + np.abs(np.random.normal(0, 0.01, n)))
        low = close * (1 - np.abs(np.random.normal(0, 0.01, n)))
        volume = np.random.randint(1_000_000, 10_000_000, n)
        turnover = np.random.uniform(1, 10, n)

        return pd.DataFrame({
            "close": close,
            "high": high,
            "low": low,
            "volume": volume.astype(float),
            "turnover": turnover,
        }, index=dates)

    def test_ma5_bias(self, sample_df):
        """5日均线偏离度"""
        factor = FactorRegistry.get("ma5_bias")
        result = factor.calculate(sample_df)
        assert result.factor_name == "ma5_bias"
        assert result.value is not None
        assert not np.isnan(result.value)
        # 偏离度一般在 -10% ~ +10%
        assert -15 < result.value < 15

    def test_ma5_bias_insufficient_data(self):
        """数据不足返回低置信度"""
        factor = FactorRegistry.get("ma5_bias")
        short_df = pd.DataFrame({"close": [10, 10, 10]})
        result = factor.calculate(short_df)
        assert result.confidence == 0.0

    def test_ma20_bias(self, sample_df):
        """20日均线偏离度"""
        factor = FactorRegistry.get("ma20_bias")
        result = factor.calculate(sample_df)
        assert result.value is not None
        assert not np.isnan(result.value)

    def test_macd_signal(self, sample_df):
        """MACD信号"""
        factor = FactorRegistry.get("macd_signal")
        result = factor.calculate(sample_df)
        assert result.factor_name == "macd_signal"
        # meta应含cross/dif/dea
        assert "cross" in result.meta
        assert result.meta["cross"] in (-1, 0, 1)

    def test_rsi_14(self, sample_df):
        """RSI因子"""
        factor = FactorRegistry.get("rsi_14")
        result = factor.calculate(sample_df)
        assert result.value is not None
        # RSI在0-100
        assert 0 <= result.value <= 100

    def test_bollinger_position(self, sample_df):
        """布林带位置"""
        factor = FactorRegistry.get("boll_position")
        result = factor.calculate(sample_df)
        # 位置一般在0附近，偶尔超出
        assert result.value is not None

    def test_volume_ratio(self, sample_df):
        """量比"""
        factor = FactorRegistry.get("volume_ratio")
        result = factor.calculate(sample_df)
        assert result.value is not None
        assert result.value > 0  # 量比必为正

    def test_turnover_rate(self, sample_df):
        """换手率"""
        factor = FactorRegistry.get("turnover_rate")
        result = factor.calculate(sample_df)
        assert result.value is not None
        assert result.value > 0


class TestFactorEngine:
    """因子引擎测试"""

    @pytest.fixture
    def sample_df(self):
        """30日行情"""
        np.random.seed(42)
        n = 30
        close = 100 * np.cumprod(1 + np.random.normal(0.001, 0.02, n))
        return pd.DataFrame({
            "close": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "volume": np.random.uniform(1e6, 1e7, n),
            "turnover": np.random.uniform(1, 10, n),
        })

    @pytest.mark.asyncio
    async def test_compute_single(self, sample_df):
        """单股计算"""
        engine = FactorEngine()
        results = await engine.compute_single("000001", pd.Timestamp.now().date(), sample_df)
        assert len(results) > 0
        for name, result in results.items():
            assert isinstance(result, FactorResult)
            assert result.factor_name == name

    def test_get_factor_summary(self):
        """因子概览"""
        engine = FactorEngine()
        summary = engine.get_factor_summary()
        assert "total" in summary
        assert summary["total"] > 0
        assert "by_category" in summary
        assert "factors" in summary
