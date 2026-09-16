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
  避免在本层固化易变的上游字段名；但**值的形状**必须还原成旧 pywencai 的
  字符串契约（见 `normalize_list_columns`），否则下游 `str(list)` 会静默写脏数据。
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
# 多值列连接符：必须与旧 pywencai 契约逐字一致（下游按 `;` 切分概念）。
_LIST_SEPARATOR = ";"
# 同花顺三级行业用 `-` 连接（`医药生物-中药-中药Ⅲ`），与 SectorInfo 口径一致。
_INDUSTRY_SEPARATOR = "-"
_INDUSTRY_COLUMN = "所属同花顺行业"
_CONCEPT_COLUMN = "所属概念"
# 上游/历史数据里的占位值，一律不得成为板块名（库内曾有 `pw_concept_None`）。
_PLACEHOLDER_VALUES = frozenset({"", "none", "nan", "null", "nat", "n/a", "-"})


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


def _clean_part(item: Any) -> str:
    """清洗多值字段的单个元素，剔除上游占位值。

    历史教训：旧路径用 ``str(row.get("所属概念"))`` 直接落库，把 Python ``None``
    写成了概念名 ``None``（``stock_sector_mapping`` 内实存 16 条
    ``pw_concept_None``）。这类占位值必须在此拦截，不能进下游板块池。
    """
    if item is None:
        return ""
    if isinstance(item, float) and pd.isna(item):
        return ""
    text = str(item).strip()
    return "" if text.lower() in _PLACEHOLDER_VALUES else text


def _join_multi(value: Any, separator: str = _LIST_SEPARATOR) -> Any:
    """把上游数组型多值字段连接成字符串；非数组原样返回。"""
    if isinstance(value, (list, tuple, set)):
        return separator.join(part for part in map(_clean_part, value) if part)
    return value


def normalize_list_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """把上游的数组型多值列还原成**旧 pywencai 的字符串契约**。

    为什么必须还原（2026-09-17 实测，见 tests/test_wencai_field_contract.py）：
      * 上游把 `所属同花顺行业` / `所属概念` 作为 JSON 数组返回，经 pandas 变成
        Python ``list``；旧 pywencai 返回的是**字符串**。
      * 下游 scheduler 用 ``str(row.get("所属同花顺行业"))`` 直接拼
        ``f"pw_industry_{...}"`` 当作 ``sector_code``。拿到 list 时会得到
        ``pw_industry_['机械设备', '专用设备', '能源及重型设备']`` ——
        **每股一个唯一 sector_code**，把 264 个行业炸成 5574 个假板块，
        并同时污染 ``stock_sector_mapping`` 与 ``SectorInfo``。
      * 概念同理：旧契约以 ``;`` 分隔、下游 ``split(";")``，list 不会被切分。

    行业：``['机械设备','专用设备','能源及重型设备']`` →
    ``机械设备-专用设备-能源及重型设备``
    概念：剔除上游**掺入的行业三级名**后以 ``;`` 连接。剔除依据：7.5 万条历史
    映射实测旧契约「概念集 ∩ 行业集 = 0」（行业 L3 257 个同样 0 交集）。
    """
    if frame.empty:
        return frame
    industry_drop: list[set[str]] = []
    if _INDUSTRY_COLUMN in frame.columns:
        for value in frame[_INDUSTRY_COLUMN]:
            industry_drop.append(
                {_clean_part(part) for part in value} - {""}
                if isinstance(value, (list, tuple, set))
                else set()
            )
        frame[_INDUSTRY_COLUMN] = pd.Series(
            [_join_multi(v, _INDUSTRY_SEPARATOR) for v in frame[_INDUSTRY_COLUMN]],
            index=frame.index,
        )
    if _CONCEPT_COLUMN in frame.columns:
        cleaned = []
        for position, value in enumerate(frame[_CONCEPT_COLUMN]):
            if not isinstance(value, (list, tuple, set)):
                cleaned.append(value)
                continue
            drop = industry_drop[position] if position < len(industry_drop) else set()
            cleaned.append(
                _join_multi([c for c in value if _clean_part(c) not in drop])
            )
        frame[_CONCEPT_COLUMN] = pd.Series(cleaned, index=frame.index)
    # 兜底：上游新增的其它数组型列，同样连接成字符串，避免下游拿到 list 的 repr。
    for column in frame.columns:
        if column in (_INDUSTRY_COLUMN, _CONCEPT_COLUMN):
            continue
        values = frame[column]
        if any(isinstance(v, (list, tuple, set)) for v in values):
            frame[column] = pd.Series(
                [_join_multi(v) for v in values], index=frame.index
            )
    return frame


def _to_frame(data: dict[str, Any]) -> pd.DataFrame:
    rows = data.get("datas") or []
    frame = pd.DataFrame(rows)
    # 上游按问句返回的列集合并不固定：有的问句给 `股票代码`，有的只给 `code`。
    # 旧 pywencai 契约恒有 `股票代码`，下游解析器（scheduler 盘前/盘后/股票状态）
    # 读的也是它；这里补一列以维持同一契约，避免每个调用方各自适配。
    if "股票代码" not in frame.columns and "code" in frame.columns:
        frame["股票代码"] = frame["code"]
    # 值的形状也必须还原成旧契约（上游多值字段是数组）——否则下游静默写脏数据。
    frame = normalize_list_columns(frame)
    frame.attrs["code_count"] = int(data.get("code_count") or 0)
    frame.attrs["row_count"] = int(data.get("row_count") or len(rows))
    frame.attrs["chunks_info"] = data.get("chunks_info") or ""
    return frame


class WencaiStreamSource:
    """问财流式选股源。同步实现；异步调用方用 `query_async`。"""

    source_name = "wencai_stream"

    # 同进程内的按问句缓存。
    # 为什么需要：全 A 股映射（5574 行）实测耗时 9~14 秒，而**盘前与盘后各拉
    # 一次完全相同的映射**；行业/概念归属变化很慢（周级），重复拉取纯属浪费。
    # 实测 perpage 调优**无效**（5574/6000/10000 -> 11.2/9.1/9.3 秒，属噪声）：
    # 这段时间花在服务端聚合全 A 股，客户端改不了。
    _cache: dict[str, tuple[float, pd.DataFrame]] = {}

    def __init__(self, *, timeout: float = 40.0) -> None:
        self.timeout = float(timeout)

    # ---- 同步核心 ----

    def query(
        self, question: str, *, perpage: int = DEFAULT_PERPAGE, ttl_sec: float = 0.0,
    ) -> pd.DataFrame:
        """按问句取选股结果。失败抛 `WencaiStreamError`，不返回空表。

        `ttl_sec > 0` 时启用同进程缓存；命中直接返回**副本**，避免调用方
        改动缓存内容。缓存只在成功时写入 —— 失败绝不落缓存。
        """
        cache_key = f"{question}\x00{int(perpage)}"
        if ttl_sec > 0:
            hit = self._cache.get(cache_key)
            if hit is not None and (_time.monotonic() - hit[0]) < ttl_sec:
                frame = hit[1].copy()
                frame.attrs.update(hit[1].attrs)
                frame.attrs["cache_hit"] = True
                logger.info("[wencai] {} -> 命中缓存 {} 行", question, len(frame))
                return frame
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
        if ttl_sec > 0:
            # 只在成功时写缓存；失败已在上方抛错，故不会污染缓存。
            self._cache[cache_key] = (_time.monotonic(), frame.copy())
        frame.attrs["cache_hit"] = False
        logger.info(
            "[wencai] {} -> {} 行 / code_count={} ({:.1f}s)",
            question, len(frame), frame.attrs.get("code_count"),
            _time.monotonic() - started,
        )
        return frame

    async def query_async(
        self, question: str, *, perpage: int = DEFAULT_PERPAGE, ttl_sec: float = 0.0,
    ) -> pd.DataFrame:
        """异步包装：阻塞 IO 放到线程池，避免堵塞事件循环（与项目既有用法一致）。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self.query(question, perpage=perpage, ttl_sec=ttl_sec)
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
