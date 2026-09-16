"""股票标记系统测试"""

import pytest

from app.core.stock_tagger import stock_tagger, TAG_TRADEABLE, TAG_OBSERVE_ONLY, TAG_BLOCKED


class TestStockTagger:
    """股票分级标记测试"""

    def test_main_sh_tradeable(self):
        """主板沪市可交易"""
        assert stock_tagger.get_board_type("600001") == "main_sh"
        assert stock_tagger.get_board_tag("main_sh") == TAG_TRADEABLE

    def test_main_sz_tradeable(self):
        """主板深市可交易"""
        assert stock_tagger.get_board_type("000001") == "main_sz"
        assert stock_tagger.get_board_tag("main_sz") == TAG_TRADEABLE

    def test_sme_tradeable(self):
        """中小板可交易"""
        assert stock_tagger.get_board_type("002001") == "sme"
        assert stock_tagger.get_board_tag("sme") == TAG_TRADEABLE

    def test_gem_observe(self):
        """创业板仅观察"""
        assert stock_tagger.get_board_type("300001") == "gem"
        assert stock_tagger.get_board_tag("gem") == TAG_OBSERVE_ONLY

    def test_star_observe(self):
        """科创板仅观察"""
        assert stock_tagger.get_board_type("688001") == "star"
        assert stock_tagger.get_board_tag("star") == TAG_OBSERVE_ONLY

    def test_bse_observe(self):
        """北交所仅观察"""
        assert stock_tagger.get_board_type("830001") == "bse"
        assert stock_tagger.get_board_tag("bse") == TAG_OBSERVE_ONLY

    def test_bse_920_prefix(self):
        """北交所920新代码"""
        assert stock_tagger.get_board_type("920001") == "bse"

    def test_unknown_code(self):
        """未知代码"""
        assert stock_tagger.get_board_type("999999") == "unknown"

    def test_get_board_type_various(self):
        """板块类型判断覆盖"""
        # 主板
        assert stock_tagger.get_board_type("600001") == "main_sh"
        assert stock_tagger.get_board_type("000001") == "main_sz"
        # 中小板
        assert stock_tagger.get_board_type("002001") == "sme"
        # 创业板
        assert stock_tagger.get_board_type("300001") == "gem"
        # 科创板
        assert stock_tagger.get_board_type("688001") == "star"
        # 北交所
        assert stock_tagger.get_board_type("830001") == "bse"

    def test_code_prefix_length_priority(self):
        """长前缀优先匹配(920>92>9)"""
        assert stock_tagger.get_board_type("920001") == "bse"  # 920新代码

    def test_filter_tradeable(self):
        """可交易股票过滤"""
        codes = ["600001", "300001", "000001", "688001"]
        tradeable = stock_tagger.filter_tradeable(codes)
        assert "600001" in tradeable
        assert "000001" in tradeable
        assert "300001" not in tradeable  # 创业板
        assert "688001" not in tradeable  # 科创板

    def test_tag_label_map(self):
        """标签映射完整"""
        from app.core.stock_tagger import TAG_LABEL_MAP
        assert TAG_TRADEABLE in TAG_LABEL_MAP
        assert TAG_OBSERVE_ONLY in TAG_LABEL_MAP
        assert TAG_BLOCKED in TAG_LABEL_MAP

    def test_is_tradeable(self):
        """可交易判断"""
        assert stock_tagger.is_tradeable("600001") is True
        assert stock_tagger.is_tradeable("300001") is False  # 创业板
        assert stock_tagger.is_tradeable("688001") is False  # 科创板
