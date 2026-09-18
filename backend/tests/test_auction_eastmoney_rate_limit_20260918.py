"""D 路由第二来源（东财）的**限频对策**回归测试。

为什么要单独锁这些
------------------
用户提出的问题：东财过去会限频，凭什么认为 09:25 的双来源实跑能成？
实测给出的部分答案（2026-09-18 04:5x，非高峰）：

  * `ulist.np/get` 一次 300 个 secid，全市场 2,994 只 → **10 个请求/轮**，
    实测 10/10 成功、约 0.9s；加 pacing 后约 1.5s；
  * 旧路径 `clist/get` 的 `pz` 硬上限 100 → 30~56 页/轮，
    且 09:15–09:25 **每分钟**跑一轮 —— 那才是过去被限频的成因
    （9/17 日志显示它其实在**第一个请求**就 `ProxyError` 挂掉，并未真正消耗额度）；
  * 腾讯自身是 100/批 = 30 个请求/轮，请求密度是东财新路径的 3 倍。

**非高峰实测不能证明高峰不限频**，所以真正的保障是代码里的对策：
把"被限频"与"网络故障"区分开、按批次重试、部分成功也保留、留诊断计数。
下面用假 client 把这些分支逐条锁死，不依赖真实网络。
"""
from __future__ import annotations

import pytest

from app.strategy.auction import (
    EASTMONEY_AUCTION_BATCH,
    EASTMONEY_AUCTION_RETRY,
    EASTMONEY_QUOTE_HOSTS,
    AuctionCollector,
    EastmoneyThrottled,
)

CODES = [f"{600000 + i:06d}" for i in range(EASTMONEY_AUCTION_BATCH * 2 + 5)]


def _row(code: str) -> dict:
    return {"f12": code, "f14": "x", "f17": 10.0, "f18": 9.9,
            "f5": 100, "f6": 100000.0, "f10": 1.2, "f124": 1789000000}


class _Resp:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeGet:
    """按调用序号编排响应；host 由 url 解析。"""

    def __init__(self, script):
        self.script = script          # list of callables(url) -> _Resp | Exception
        self.calls: list[str] = []

    async def __call__(self, url, **kwargs):
        # 真实调用把 secids 放在 params 里；这里拼回 URL，让断言和响应
        # 生成都用同一份「完整请求」，避免夹具与生产调用方式脱节。
        params = kwargs.get("params") or {}
        if params:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(params)}"
        self.calls.append(url)
        index = len(self.calls) - 1
        action = self.script[min(index, len(self.script) - 1)]
        result = action(url)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def patch_client(monkeypatch):
    """替换 httpx.AsyncClient，返回 (fake_get, set_script)。"""
    import httpx

    holder: dict = {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kwargs):
            return await holder["get"](url, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    def set_script(script):
        holder["get"] = _FakeGet(script)
        return holder["get"]

    return set_script


def _secids(url: str) -> list[str]:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)["secids"][0].split(",")


def _ok_batch(url: str) -> _Resp:
    """从 url 里抽 secids，返回这些 code 的正常响应。"""
    return _Resp({"data": {"diff": [_row(s.split(".")[1]) for s in _secids(url)]}})


def _batch_ordinal(url: str, first_code: str) -> int:
    """这条请求覆盖的是第几个原始批次 —— 用于按批次编排故障。"""
    return _secids(url).index(
        next(s for s in _secids(url) if s.endswith(first_code)))


@pytest.mark.asyncio
async def test_happy_path_uses_batched_ulist_not_paged_clist(patch_client):
    """正常路径：按 300/批 打 ulist.np/get，请求数 = ceil(n/300)。"""
    get = patch_client([_ok_batch])
    diag: dict = {}
    quotes = await AuctionCollector()._fetch_eastmoney_quotes(CODES, diagnostics=diag)

    assert len(quotes) == len(CODES)
    assert all("/api/qt/ulist.np/get" in u for u in get.calls)
    assert "clist" not in "".join(get.calls), "不得回退到分页 clist（限频成因）"
    assert len(get.calls) == 3 == diag["batches"]
    assert diag["rate_limited"] is False
    assert diag["failed_batches"] == 0
    assert diag["missing_codes"] == 0
    assert diag["host_used"] == EASTMONEY_QUOTE_HOSTS[0]


@pytest.mark.asyncio
async def test_429_is_classified_as_rate_limit_and_switches_host(patch_client):
    """429 必须被判成"限频"（而不是普通网络故障）并换主机。"""
    def script(url):
        if EASTMONEY_QUOTE_HOSTS[0] in url:
            return _Resp({}, status_code=429)
        return _ok_batch(url)

    get = patch_client([script])
    diag: dict = {}
    quotes = await AuctionCollector()._fetch_eastmoney_quotes(CODES, diagnostics=diag)

    assert diag["rate_limited"] is True, "429 必须置 rate_limited"
    assert diag["throttled_batches"] == 1
    # 主主机限频后换到备用主机并补齐
    assert diag["host_used"] == EASTMONEY_QUOTE_HOSTS[1]
    assert len(quotes) == len(CODES)
    assert any(EASTMONEY_QUOTE_HOSTS[1] in u for u in get.calls)


@pytest.mark.asyncio
async def test_throttle_does_not_restart_from_batch_zero_on_next_host(patch_client):
    """旧缺陷：任一批次失败 → 换主机从第 0 批重来，请求量翻倍。

    现在要求：限频主机上失败的那个批次之前的**已取批次不重取**，
    备用主机只补缺口，总请求数受控。
    """
    def script(url):
        host = EASTMONEY_QUOTE_HOSTS[0] if EASTMONEY_QUOTE_HOSTS[0] in url \
            else EASTMONEY_QUOTE_HOSTS[1]
        script.seen.setdefault(host, []).append(url)
        # 只有**主主机**限频：第 1 个批次成功，其余 429；
        # 备用主机必须正常服务，否则测不出"换主机补缺口"。
        if host == EASTMONEY_QUOTE_HOSTS[0]:
            return _ok_batch(url) if "600000" in url else _Resp({}, status_code=429)
        return _ok_batch(url)

    script.seen = {}
    get = patch_client([script])
    diag: dict = {}
    quotes = await AuctionCollector()._fetch_eastmoney_quotes(CODES, diagnostics=diag)

    assert len(quotes) == len(CODES), "备用主机必须补齐缺口"
    # 备用主机只补 2 个缺失批次，而不是把 3 个批次全部重取
    assert len(script.seen[EASTMONEY_QUOTE_HOSTS[1]]) == 2, script.seen
    assert diag["batches_by_host"][EASTMONEY_QUOTE_HOSTS[1]] == 2
    assert diag["rate_limited"] is True


@pytest.mark.asyncio
async def test_transient_network_error_skips_only_that_batch(patch_client):
    """单批网络故障：重试后跳过该批，**保留其余批次**（部分成功优于全丢）。"""
    def script(url):
        script.seen.append(url)
        # 第 2 个批次（含 600300）在**两个主机上都**失败 → 该批次最终缺失，
        # 但第 1、3 批必须保留下来（部分成功优于全丢）。
        if "600300" in url:
            raise ConnectionError("boom")
        return _ok_batch(url)

    script.seen = []
    get = patch_client([script])
    diag: dict = {}
    quotes = await AuctionCollector()._fetch_eastmoney_quotes(CODES, diagnostics=diag)

    # 第 1 批拿到 300 只；第 2 批重试耗尽后跳过；第 3 批拿到 5 只
    assert len(quotes) == EASTMONEY_AUCTION_BATCH + 5, len(quotes)
    assert diag["rate_limited"] is False, "网络故障不得误报为限频"
    # 两个主机各失败一次该批次；缺失量只记一份（按 code 去重后的缺口）
    assert diag["failed_batches"] == 2, diag
    assert diag["missing_codes"] == EASTMONEY_AUCTION_BATCH
    assert diag["retries"] >= 1


@pytest.mark.asyncio
async def test_html_error_page_is_treated_as_throttle(patch_client):
    """返回非 JSON 对象（反爬错误页）按限频处理，不能当成功解析。"""
    def script(url):
        if EASTMONEY_QUOTE_HOSTS[0] in url:
            return _Resp("<html>forbidden</html>")
        return _ok_batch(url)

    patch_client([script])
    diag: dict = {}
    quotes = await AuctionCollector()._fetch_eastmoney_quotes(CODES, diagnostics=diag)

    assert diag["rate_limited"] is True
    assert len(quotes) == len(CODES)
    assert diag["host_used"] == EASTMONEY_QUOTE_HOSTS[1]


@pytest.mark.asyncio
async def test_all_hosts_throttled_returns_empty_and_full_gap(patch_client):
    """两个主机都被限频：返回空、缺口如实记为全量，不得伪造成成功。"""
    patch_client([lambda url: _Resp({}, status_code=429)])
    diag: dict = {}
    quotes = await AuctionCollector()._fetch_eastmoney_quotes(CODES, diagnostics=diag)

    assert quotes == {}
    assert diag["rate_limited"] is True
    assert diag["missing_codes"] == len(CODES)
    assert diag["host_used"] is None


@pytest.mark.asyncio
async def test_all_fivexx_are_throttled_not_silently_retried_forever(patch_client):
    """5xx 同 429 处理，且重试次数有界（= EASTMONEY_AUCTION_RETRY）。"""
    get = patch_client([lambda url: _Resp({}, status_code=503)])
    diag: dict = {}
    await AuctionCollector()._fetch_eastmoney_quotes(CODES[:10], diagnostics=diag)

    assert diag["rate_limited"] is True
    per_host = EASTMONEY_AUCTION_RETRY          # 每主机只打 1 个批次 × 重试上限
    assert len(get.calls) == per_host * len(EASTMONEY_QUOTE_HOSTS), get.calls


@pytest.mark.asyncio
async def test_empty_diff_is_not_treated_as_throttle(patch_client):
    """上游正常返回但 diff 为空 → 不是限频，只是没数据；不得误判并反复换主机。"""
    patch_client([lambda url: _Resp({"data": {"diff": []}})])
    diag: dict = {}
    quotes = await AuctionCollector()._fetch_eastmoney_quotes(CODES[:10], diagnostics=diag)

    assert quotes == {}
    assert diag["rate_limited"] is False
    assert diag["failed_batches"] == 0


def test_pacing_and_retry_constants_are_documented_for_the_window():
    """pacing + 重试必须仍装得进 09:25:00–09:25:30 的 30 秒证据窗。

    实测：10 个批次 × 0.08s pacing ≈ 0.8s，整轮 腾讯+东财 ≈ 2.5s；
    最坏情况（每批都重试到上限）也必须在窗口内，否则第 3 轮 09:25:25
    只剩 5 秒必然失败 —— 这正是必须把重试次数写小的原因。
    """
    from app.strategy.auction import (
        EASTMONEY_AUCTION_BACKOFF_SEC,
        EASTMONEY_AUCTION_PACE_SEC,
    )

    batches = 10
    worst = (batches * EASTMONEY_AUCTION_PACE_SEC
             + batches * sum(EASTMONEY_AUCTION_BACKOFF_SEC * i
                             for i in range(1, EASTMONEY_AUCTION_RETRY)))
    assert worst < 25, f"最坏耗时 {worst:.1f}s 会越过证据窗"
    assert EastmoneyThrottled is not None


@pytest.mark.asyncio
async def test_rate_limit_is_recorded_to_data_source_health_with_correct_types(patch_client):
    """被限频时必须真的写进 DataSourceHealth，而不是抛 TypeError。

    这是一个**只在限频路径才触发**的缺陷：`record_failure` 的签名是
    `(source, api_name, error_msg: str, latency_ms: int = 0)`，内部还会做
    `error_msg[:500]`。第一版传的是 `EastmoneyThrottled` **异常对象**、且
    `latency_ms=None` —— 两者都不合法。因为正常路径不经过这里，
    全套测试都不会发现，只有真被限频的那天才炸。
    """
    from datetime import date, datetime

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.core.data_quality import DataSourceHealth
    from app.db.session import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # 两个主机都 429 → 必然走到记账分支
        patch_client([lambda url: _Resp({}, status_code=429)])

        async with maker() as session:
            # 显式传 codes，避免依赖 StockTag 种子数据。
            at = datetime(2026, 9, 18, 9, 25, 10)
            result = await AuctionCollector().collect_eastmoney_auction_evidence(
                session, date(2026, 9, 18), now=at,
                codes=[f"60{i:04d}" for i in range(3)],
            )
            health = (await session.scalars(
                select(DataSourceHealth).where(
                    DataSourceHealth.source == "eastmoney",
                    DataSourceHealth.api_name == "auction_quote_rate_limit",
                )
            )).all()
    finally:
        await engine.dispose()

    assert result["written"] == 0
    assert result["fetch_diagnostics"]["rate_limited"] is True
    assert health, "限频必须留下 DataSourceHealth 记录"
    assert isinstance(health[0].latency_ms, int), health[0].latency_ms
    assert "限频批次" in (health[0].error_msg or "")
    assert health[0].status in {"degraded", "down"}
