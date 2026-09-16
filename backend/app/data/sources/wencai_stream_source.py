"""问财（同花顺）流式选股源 —— 替代已失效的 pywencai。

为什么需要这个
--------------
pywencai 0.13.1（已是 PyPI 最新版）自 2026-08 下旬起完全不可用。
根因**不是**库版本旧，而是上游把取数方式换了：

* 旧：`POST http://www.iwencai.com/customized/chart/get-robot-data`，返回 JSON
* 新：`POST https://www.iwencai.com/gateway/aime/stream-query`，返回 **SSE 流**

pywencai 的模型是「一次 POST → 解析 JSON → DataFrame」，因此**无论怎么升级都无法工作**，
除非整个重写。实测证据（2026-09-16，`data_source_health`）：
`limit_up` / `stock_mapping` / `sector_list` 的最后一次成功均停在 2026-08-21~24，
此后全部转 down；这也解释了 `stock_tags` 为何冻结在 2026-08-31。

协议（已由 Playwright 抓包逐字验证，非推断）
-------------------------------------------
请求：`POST /gateway/aime/stream-query`
  headers: ``accept: text/event-stream`` / ``x-source: ths_iwencai_pc_xuangu``
           / ``hexin-v``（反爬令牌，复用 pywencai 的 node 生成器）/ ``cookie``
  body   : ``{"question": ..., "agent_tools":[{"tool_id":"FinQuery",
             "tool_param":{"domain":"stock","perpage":50}}], "version":"3.4.1", ...}``

响应：SSE，逐行 ``data:{...}``。真实数据在**最后一个**
  ``section.result_page.components[].data`` 里：
  ``{"code_count": 91, "row_count": 91, "columns":[...7...], "datas":[...50...]}``

鉴权：问财要求登录会话。未登录时同接口返回 401，或数据层返回 ``code_count=0``。
  故 cookie 从 ``settings.PYWENCAI_COOKIE``（即 ``.env``）读取。

设计取舍
--------
* **失败即抛错，绝不返回空 DataFrame**：空结果与"取不到"必须可区分，
  否则调用方会把故障当成"今天没有涨停股"，与
  `_update_stock_status` 既有的「列表缺席不是风险解除证据」语义一致。
* 只读：本模块不写任何业务表、不下单、不调用交易接口。
* 列名保持**原始中文 key**（含日期后缀列），由上层解析器决定如何映射，
  避免在本层固化易变的上游字段名。
"""
from __future__ import annotations

import asyncio
import json
import re
import time as _time
from typing import Any

import pandas as pd
import requests
from loguru import logger

from app.config.settings import settings

STREAM_URL = "https://www.iwencai.com/gateway/aime/stream-query"
PAGE_REFERER = "https://www.iwencai.com/screener/result?querytype=stock"
# 上游前端硬编码的协议版本与 agent；上游改版时需同步更新（故配套告警）。
DIALOG_VERSION = "3.4.1"
AGENT_ID = "MaSzyUwyyl"
AGENT_NAME = ""
DEFAULT_PERPAGE = 50
# 形如 `涨停原因[20260916]` —— 列名带交易日后缀，逐日变化
_DATED_COLUMN = re.compile(r"\[(\d{8})\]$")


class WencaiStreamError(RuntimeError):
    """问财流式接口不可用（区别于「查得到但没有结果」）。"""


def _headers(cookie: str) -> dict[str, str]:
    headers = {
        "accept": "text/event-stream",
        "content-type": "application/json",
        "x-source": "ths_iwencai_pc_xuangu",
        "referer": PAGE_REFERER,
        "origin": "https://www.iwencai.com",
        # 上游要求浏览器 UA；用与抓包一致的值，避免反爬差异
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    if cookie:
        headers["cookie"] = cookie
    try:                                    # 反爬令牌；生成失败不致命，交由上游判断
        from pywencai.headers import get_token

        token = get_token()
        if token:
            headers["hexin-v"] = token
    except Exception as exc:                # noqa: BLE001 — 令牌非必须
        logger.warning(f"[wencai] hexin-v 令牌生成失败，继续但不带该头: {exc}")
    return headers


def _payload(question: str, *, perpage: int = DEFAULT_PERPAGE) -> dict[str, Any]:
    return {
        "question": question,
        "default_fallback": False,
        "input_type": "click",
        "entity_info": {"device_type": "mac", "comefrom": None},
        "source": "ths_iwencai_pc_xuangu",
        "dialog_model": "CUSTOMER_AGENT",
        "version": DIALOG_VERSION,
        "agent_tools": [{"tool_id": "FinQuery",
                         "tool_param": {"domain": "stock", "perpage": int(perpage)}}],
        "events": [{"event_type": "user_input", "event_name": "normal_agent", "content": {}}],
        "add_info": {},
        "agent_id": AGENT_ID,
        "agent_name": AGENT_NAME,
    }


def extract_table(stream_text: str) -> dict[str, Any] | None:
    """从 SSE 文本里取出第一个带行数据的 result_page 组件。

    只做纯解析，便于单测直接喂字符串。返回 ``None`` 表示流里没有结果表。
    """
    for raw in stream_text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        body = line[len("data:"):].strip()
        if not body or body == "[DONE]":
            continue
        try:
            event = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        section = event.get("section")
        if not isinstance(section, dict):
            continue
        result_page = section.get("result_page")
        if not isinstance(result_page, dict):
            continue
        for component in result_page.get("components") or []:
            if not isinstance(component, dict):
                continue
            data = component.get("data")
            if isinstance(data, dict) and isinstance(data.get("datas"), list):
                return data
    return None


def _to_frame(data: dict[str, Any]) -> pd.DataFrame:
    rows = data.get("datas") or []
    frame = pd.DataFrame(rows)
    # 上游按问句返回的列集合并不固定：有的问句给 `股票代码`，有的只给 `code`。
    # 旧 pywencai 契约恒有 `股票代码`，下游解析器（scheduler 盘前/盘后/股票状态）
    # 读的也是它；这里补一列以维持同一契约，避免每个调用方各自适配。
    if "股票代码" not in frame.columns and "code" in frame.columns:
        frame["股票代码"] = frame["code"]
    frame.attrs["code_count"] = int(data.get("code_count") or 0)
    frame.attrs["row_count"] = int(data.get("row_count") or len(rows))
    frame.attrs["chunks_info"] = data.get("chunks_info") or ""
    return frame


class WencaiStreamSource:
    """问财流式选股源。同步实现；异步调用方用 `query_async`。"""

    source_name = "wencai_stream"

    def __init__(self, *, timeout: float = 40.0) -> None:
        self.timeout = float(timeout)

    # ---- 同步核心 ----

    def query(self, question: str, *, perpage: int = DEFAULT_PERPAGE) -> pd.DataFrame:
        """按问句取选股结果。失败抛 `WencaiStreamError`，不返回空表。"""
        cookie = str(getattr(settings, "PYWENCAI_COOKIE", "") or "").strip()
        if not cookie:
            raise WencaiStreamError(
                "未配置 PYWENCAI_COOKIE：问财自 2026-08 下旬起要求登录会话，"
                "请在 .env 填入登录后的 cookie"
            )
        started = _time.monotonic()
        try:
            resp = requests.post(
                STREAM_URL, headers=_headers(cookie),
                json=_payload(question, perpage=perpage),
                timeout=self.timeout, stream=True,
            )
        except requests.RequestException as exc:
            raise WencaiStreamError(f"问财流式接口请求失败: {exc}") from exc

        if resp.status_code == 401:
            raise WencaiStreamError(
                "问财返回 401：登录会话已失效，需重新登录并更新 PYWENCAI_COOKIE"
            )
        if resp.status_code != 200:
            raise WencaiStreamError(
                f"问财流式接口 HTTP {resp.status_code}: {resp.text[:200]}"
            )

        chunks: list[str] = []
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if raw:
                    chunks.append(raw)
        except requests.RequestException as exc:
            raise WencaiStreamError(f"读取问财流式响应中断: {exc}") from exc

        data = extract_table("\n".join(chunks))
        if data is None:
            raise WencaiStreamError(
                "问财流式响应中没有 result_page（可能是登录失效、"
                f"上游协议改版，或问题无法解析）；已收到 {len(chunks)} 行 SSE"
            )
        frame = _to_frame(data)
        logger.info(
            "[wencai] {} -> {} 行 / code_count={} ({:.1f}s)",
            question, len(frame), frame.attrs.get("code_count"),
            _time.monotonic() - started,
        )
        return frame

    async def query_async(self, question: str, *, perpage: int = DEFAULT_PERPAGE) -> pd.DataFrame:
        """异步包装：阻塞 IO 放到线程池，避免堵塞事件循环（与项目既有用法一致）。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self.query(question, perpage=perpage)
        )


def normalize_dated_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """把 `涨停原因[20260916]` 这类带交易日后缀的列名还原为基名。

    上游按交易日给列名加后缀，逐日变化；上层解析器需要稳定列名。
    保留原列并在 `attrs['dated_columns']` 记下后缀日期，便于审计。
    """
    rename: dict[str, str] = {}
    dated: dict[str, str] = {}
    for column in frame.columns:
        match = _DATED_COLUMN.search(str(column))
        if match:
            base = _DATED_COLUMN.sub("", str(column))
            rename[column] = base
            dated[base] = match.group(1)
    if rename:
        frame = frame.rename(columns=rename)
        frame.attrs["dated_columns"] = dated
    return frame
