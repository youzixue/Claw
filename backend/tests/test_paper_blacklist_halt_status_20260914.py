"""Pure current-status projections, without the execution-risk fixture."""
import pytest
from datetime import datetime
from app.core.stock_tagger import stock_tagger
from app.models.stock import StockBlacklist

AT = datetime(2026, 9, 9, 10)

@pytest.mark.parametrize("reason,active", [
    ("suspended", True), ("suspended", False),
    ("st", True), ("delisting", True), ("manual", True),
])
def test_blacklist_halt_projection_uses_only_active_explicit_reason(reason, active):
    from datetime import timedelta
    from app.models.stock import StockTag
    tag = StockTag(code="600001", name="轮次测试", board_type="main_sh", board_tag="tradeable",
        is_st=False, is_suspended=False, is_delisting=False)
    blacklist = StockBlacklist(code=tag.code, reason=reason,
        start_date=AT.date()-timedelta(days=2), end_date=None if active else AT.date()-timedelta(days=1))
    status = stock_tagger.resolve_status(tag.code, tag, blacklist=blacklist, at=AT)
    assert status["is_suspended"] is (active and reason=="suspended")
    assert status["board_tag"] == ("suspended" if active and reason=="suspended" else
                                   "blocked" if active else "tradeable")
