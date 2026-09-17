"""股票标记系统 — 用户核心需求"""

from datetime import date, datetime
import re
from typing import Optional
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock import StockTag, StockBlacklist, StockSpot
from app.config.settings import settings


# 板块类型枚举
BOARD_TYPE_MAIN_SH = "main_sh"      # 沪市主板 600/601/603/605
BOARD_TYPE_MAIN_SZ = "main_sz"      # 深市主板 000/001
BOARD_TYPE_SME = "sme"              # 中小板 002
BOARD_TYPE_GEM = "gem"              # 创业板 300/301
BOARD_TYPE_STAR = "star"            # 科创板 688
BOARD_TYPE_BSE = "bse"              # 北交所 8开头

# 板块标记枚举
TAG_TRADEABLE = "tradeable"         # ✅ 可交易
TAG_OBSERVE_ONLY = "observe_only"   # 👁️ 仅观察
TAG_BLOCKED = "blocked"             # 🚫 不推
TAG_SUSPENDED = "suspended"         # ⏸️ 停牌

# 代码前缀→板块类型映射
CODE_PREFIX_MAP = {
    "600": BOARD_TYPE_MAIN_SH,
    "601": BOARD_TYPE_MAIN_SH,
    "603": BOARD_TYPE_MAIN_SH,
    "605": BOARD_TYPE_MAIN_SH,
    "000": BOARD_TYPE_MAIN_SZ,
    "001": BOARD_TYPE_MAIN_SZ,
    "002": BOARD_TYPE_SME,
    "003": BOARD_TYPE_MAIN_SZ,  # 深市主板新代码段
    "300": BOARD_TYPE_GEM,
    "301": BOARD_TYPE_GEM,
    "302": BOARD_TYPE_MAIN_SZ,   # 深市主板新代码段（与 price_limit_rules 的默认 main 一致）
    "688": BOARD_TYPE_STAR,
    "689": BOARD_TYPE_STAR,      # 科创板 CDR（存托凭证）；price_limit_rules 亦按 688/689 同规则
    "83":  BOARD_TYPE_BSE,
    "87":  BOARD_TYPE_BSE,
    "43":  BOARD_TYPE_BSE,
    "92":  BOARD_TYPE_BSE,   # 北交所920xxx新代码
    "920": BOARD_TYPE_BSE,  # 优先匹配3位
}

# 板块类型→标记映射
BOARD_TAG_MAP = {
    BOARD_TYPE_MAIN_SH: TAG_TRADEABLE,
    BOARD_TYPE_MAIN_SZ: TAG_TRADEABLE,
    BOARD_TYPE_SME: TAG_TRADEABLE,
    BOARD_TYPE_GEM: TAG_OBSERVE_ONLY,
    BOARD_TYPE_STAR: TAG_OBSERVE_ONLY,
    BOARD_TYPE_BSE: TAG_OBSERVE_ONLY,
}

# 标记→显示标签
TAG_LABEL_MAP = {
    TAG_TRADEABLE: "✅ 主板",
    TAG_OBSERVE_ONLY: "👁️ 仅观察",
    TAG_BLOCKED: "🚫 不推",
    TAG_SUSPENDED: "⏸️ 停牌",
}

# 板块类型→详细标签
BOARD_LABEL_MAP = {
    BOARD_TYPE_MAIN_SH: "✅ 沪主板",
    BOARD_TYPE_MAIN_SZ: "✅ 深主板",
    BOARD_TYPE_SME: "✅ 中小板",
    BOARD_TYPE_GEM: "👁️ 创业板",
    BOARD_TYPE_STAR: "👁️ 科创板",
    BOARD_TYPE_BSE: "👁️ 北交所",
}


class StockTagger:
    """股票分级标记系统"""

    def get_board_type(self, code: str) -> str:
        """根据代码判断板块类型"""
        if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
            return "unknown"
        for prefix, board_type in sorted(CODE_PREFIX_MAP.items(), key=lambda x: -len(x[0])):
            if code.startswith(prefix):
                return board_type
        return "unknown"

    def get_board_tag(self, board_type: str) -> str:
        """板块类型→标记"""
        return BOARD_TAG_MAP.get(board_type, TAG_OBSERVE_ONLY)

    def is_tradeable(self, code: str) -> bool:
        """仅判断代码板块准入，不是身份、当前风险或下单许可。"""
        board_type = self.get_board_type(code)
        return self.get_board_tag(board_type) == TAG_TRADEABLE

    def filter_tradeable(self, codes: list[str]) -> list[str]:
        """过滤出可交易股票"""
        return [c for c in codes if self.is_tradeable(c)]

    def get_tag_label(self, code: str) -> str:
        """获取显示标签"""
        board_type = self.get_board_type(code)
        return BOARD_LABEL_MAP.get(board_type, "❓ 未知")

    @staticmethod
    def clean_name(name) -> str:
        if not isinstance(name, str):
            return ""
        value = "".join(name.split())
        return "" if value.lower() in {"", "nan", "none", "null", "--", "未知"} else value

    def name_risks(self, name) -> tuple[bool, bool]:
        value = self.clean_name(name).upper()
        return (
            bool(re.match(r"^(?:S\*?ST|\*?ST)", value)),
            bool(value and (value.startswith("退") or value.endswith("退") or "退市" in value)),
        )

    def resolve_status(self, code, tag=None, *, name="", quote_name="", blacklist=None, at=None) -> dict:
        """当前风险投影，不证明历史PIT/官方证券身份，也不是完整买入许可。"""
        board_type = self.get_board_type(code)
        stored_name = self.clean_name(getattr(tag, "name", None))
        supplied_name = self.clean_name(name)
        current_quote_name = self.clean_name(quote_name)
        reasons = []
        if board_type == "unknown":
            reasons.append("invalid_or_unknown_code")
        if tag is None:
            reasons.append("missing_stock_tag")
        else:
            if getattr(tag, "code", None) != code:
                reasons.append("tag_code_mismatch")
            if not stored_name:
                reasons.append("missing_stock_name")
            if getattr(tag, "board_type", None) != board_type:
                reasons.append("board_type_conflict")
            if any(value and stored_name and value != stored_name
                   for value in (supplied_name, current_quote_name)):
                reasons.append("name_conflict")
            if getattr(tag, "board_tag", None) not in TAG_LABEL_MAP:
                reasons.append("unknown_board_tag")
        name_st, name_delisting = self.name_risks(stored_name)
        other_st, other_delisting = self.name_risks(supplied_name)
        quote_st, quote_delisting = self.name_risks(current_quote_name)
        is_st = bool(getattr(tag, "is_st", False) or name_st or other_st or quote_st)
        is_delisting = bool(getattr(tag, "is_delisting", False) or name_delisting or other_delisting or quote_delisting)
        is_suspended = bool(getattr(tag, "is_suspended", False))
        today = (at or datetime.now()).date()
        active_blacklist = bool(
            blacklist is not None
            and getattr(blacklist, "code", None) == code
            and (blacklist.start_date is None or blacklist.start_date <= today)
            and (blacklist.end_date is None or blacklist.end_date >= today)
        )
        # Explicit active halt evidence applies to exits as well as entries.
        # ST/delisting/manual entry bans are NOT inferred to be trading halts.
        is_suspended = bool(is_suspended or (
            active_blacklist and str(blacklist.reason or "") == "suspended"))
        if is_st:
            reasons.append("st_risk")
        if is_delisting:
            reasons.append("delisting_risk")
        if is_suspended:
            reasons.append("suspended")
        if active_blacklist:
            reasons.append("active_blacklist")
        board_tag = self.get_board_tag(board_type)
        stored_board_tag = getattr(tag, "board_tag", None)
        # 正向风险、人工限制、板块限制取并集；错误tradeable字段不能覆盖风险。
        if reasons or stored_board_tag == TAG_OBSERVE_ONLY:
            board_tag = TAG_OBSERVE_ONLY
        if is_st or is_delisting or active_blacklist or stored_board_tag == TAG_BLOCKED:
            board_tag = TAG_BLOCKED
        if is_suspended or stored_board_tag == TAG_SUSPENDED:
            board_tag = TAG_SUSPENDED
        identity_issues = [r for r in reasons if r not in {
            "st_risk", "delisting_risk", "suspended", "active_blacklist",
        }]
        return {
            "contract_version": "stock_status_projection_v1_20260914",
            "basis": "current_projection_not_historical_pit",
            "identity_status": "unknown_or_conflicting" if identity_issues else "known_projection",
            "identity_issues": identity_issues,
            "board_type": board_type, "board_tag": board_tag,
            "is_st": is_st, "is_delisting": is_delisting,
            "is_suspended": is_suspended,
            "is_ipo_recent": bool(getattr(tag, "is_ipo_recent", False)),
            "is_limit_up": bool(getattr(tag, "is_limit_up", False)),
            "is_limit_down": bool(getattr(tag, "is_limit_down", False)),
            "is_tradeable": board_tag == TAG_TRADEABLE and not identity_issues,
            "active_blacklist": active_blacklist,
            "blacklist_reason": str(blacklist.reason or "") if active_blacklist else None,
            "risk_reasons": reasons,
            "execution_authorized": False,
        }

    async def load_status(self, session: AsyncSession, code: str, *,
                          fresh: bool = False, at: datetime | None = None) -> dict:
        # 行情名称只增加风险/冲突，不能补齐缺失身份。
        # Locked execution explicitly reloads even caller-retained ORM projections.
        at = at or datetime.now()
        if self.get_board_type(code) == "unknown":
            return self.resolve_status(code, at=at)
        with session.no_autoflush:
            tag = await session.get(StockTag, code, populate_existing=True) if fresh else await session.get(StockTag, code)
            blacklist = await session.get(StockBlacklist, code, populate_existing=True) if fresh else await session.get(StockBlacklist, code)
            name = await session.scalar(select(StockSpot.name).where(StockSpot.code == code))
        return self.resolve_status(code, tag, quote_name=name, blacklist=blacklist, at=at)

    async def tag_stock(self, session: AsyncSession, code: str, name: str = "",
                        is_st: Optional[bool] = None, is_suspended: Optional[bool] = None,
                        is_limit_up: Optional[bool] = None, is_limit_down: Optional[bool] = None,
                        ipo_date: Optional[date] = None) -> StockTag:
        """增量标记；映射缺失/False不构成摘帽或复牌证据，不清除既有风险。"""
        board_type = self.get_board_type(code)
        if board_type == "unknown":
            # 2026-09-18：原实现只抛裸字符串，导致 data_source_health 里
            # pywencai/stock_mapping 报 invalid_or_unknown_stock_code 时
            # **无法知道是哪个代码**，只能靠猜。现把代码与已登记前缀一并带出。
            # 注意：调用方以 `match="invalid_or_unknown_stock_code"` 匹配前缀，
            # 追加后缀不影响既有测试。
            raise ValueError(
                f"invalid_or_unknown_stock_code: code={code!r} "
                f"prefix={str(code)[:3]!r} "
                f"（请在 stock_tagger.CODE_PREFIX_MAP 登记该号段）"
            )
        tag = await session.get(StockTag, code)
        if tag is None:
            tag = StockTag(code=code, board_type=board_type,
                           board_tag=self.get_board_tag(board_type))
            session.add(tag)
        old_name_st, old_name_delisting = self.name_risks(tag.name)
        if self.clean_name(name):
            tag.name = self.clean_name(name)
        name_st, name_delisting = self.name_risks(tag.name)
        tag.is_st = bool(tag.is_st or is_st is True or old_name_st or name_st)
        tag.is_delisting = bool(tag.is_delisting or old_name_delisting or name_delisting)
        tag.is_suspended = bool(tag.is_suspended or is_suspended is True)
        tag.board_type = board_type
        if tag.is_suspended or tag.board_tag == TAG_SUSPENDED:
            tag.board_tag = TAG_SUSPENDED
        elif tag.is_st or tag.is_delisting or tag.board_tag == TAG_BLOCKED:
            tag.board_tag = TAG_BLOCKED
        elif tag.board_tag != TAG_OBSERVE_ONLY:
            tag.board_tag = self.get_board_tag(board_type)
        for field, value in (("is_limit_up", is_limit_up), ("is_limit_down", is_limit_down)):
            if type(value) is bool:
                setattr(tag, field, value)
        if ipo_date is not None:
            tag.ipo_date = ipo_date
        if tag.ipo_date:
            tag.is_ipo_recent = (date.today() - tag.ipo_date).days < settings.IPO_RECENT_DAYS
        tag.updated_at = datetime.now()
        await session.commit()
        return tag

    async def batch_tag(self, session: AsyncSession,
                        stocks: list[dict]) -> int:
        """批量标记股票列表；**单个未登记号段不得中断整批**。

        stocks: [{"code": "600xxx", "name": "XX", "is_st": False, ...}]

        2026-09-17：原先逐个 `tag_stock`，而 `tag_stock` 对
        `get_board_type(code) == "unknown"` 直接抛 `ValueError` —— 于是
        列表中任意一个新号段就会让**整批标记全部丢失**（且已 add 的 tag 未提交）。
        实测当日盘后：映射写入 81,187 条成功，紧随其后的 `股票标记写入`
        整行缺失（对照 8/17、8/18、8/24 均写入 3,400~4,962 只），
        原因就是 `689009`（科创板 CDR）与 `302132`（深主板新号段）两个号段
        当时尚未登记。

        改为**逐项失败隔离**：未登记号段跳过并汇总告警，其余照常标记。
        跳过的股票不会失去既有 `StockTag`（`tag_stock` 是增量标记，且映射缺失
        不构成摘帽或复牌证据），只是本轮不刷新。
        """
        count = 0
        skipped: list[str] = []
        for s in stocks:
            code = str(s.get("code") or "")
            if self.get_board_type(code) == "unknown":
                skipped.append(code)
                continue
            await self.tag_stock(
                session,
                code=code,
                name=s.get("name", ""),
                is_st=s.get("is_st"),
                is_suspended=s.get("is_suspended"),
                is_limit_up=s.get("is_limit_up"),
                is_limit_down=s.get("is_limit_down"),
                ipo_date=s.get("ipo_date"),
            )
            count += 1
        if skipped:
            logger.warning(
                f"批量标记跳过 {len(skipped)} 只未登记号段的股票（不中断整批）: "
                f"{skipped[:10]}{'...' if len(skipped) > 10 else ''}；"
                "请在 CODE_PREFIX_MAP 登记号段后重跑"
            )
            # 2026-09-18：跳过只写日志等于**静默丢标记** —— 数据源健康里看不到，
            # 汇总告警也不会亮，只能靠人工翻日志。改成落一条可审计的健康记录：
            # 首次出现记为 degraded，持续出现升级为 down，与其它采集失败同一口径。
            from app.core.data_quality import data_quality_guard

            await data_quality_guard.record_failure(
                session,
                "stock_tagger",
                "unregistered_code_segment",
                "invalid_or_unknown_stock_code: "
                f"count={len(skipped)} "
                f"prefixes={sorted({code[:3] for code in skipped})} "
                f"codes={skipped[:20]}{'...' if len(skipped) > 20 else ''}",
            )
        logger.info(f"批量标记完成: {count}只股票")
        return count

    async def get_signal_filter(self, session: AsyncSession,
                                 include_observe: bool = True,
                                 include_limit_up: bool = True) -> dict:
        """获取信号过滤条件
        
        返回: {
            "blocked_codes": [...],     # 完全不推(ST/退市)
            "suspended_codes": [...],   # 停牌(不参与评分)
            "observe_codes": [...],     # 仅观察(创业板/科创板/北交所)
            "limit_up_codes": [...],    # 已涨停(推但标注无法买入)
        }
        """
        at = datetime.now()
        tags, blacklists, quote_names = await self._load_status_rows(session)
        statuses = {
            code: self.resolve_status(code, tags.get(code), blacklist=blacklists.get(code),
                                      quote_name=quote_names.get(code), at=at)
            for code in tags.keys() | blacklists.keys() | quote_names.keys()
        }
        return {
            "blocked_codes": [c for c, s in statuses.items() if s["board_tag"] == TAG_BLOCKED],
            "suspended_codes": [c for c, s in statuses.items() if s["board_tag"] == TAG_SUSPENDED],
            "observe_codes": [c for c, s in statuses.items() if s["board_tag"] == TAG_OBSERVE_ONLY] if include_observe else [],
            "limit_up_codes": [c for c, s in statuses.items() if s["is_limit_up"]] if include_limit_up else [],
        }

    @staticmethod
    async def _load_status_rows(session):
        with session.no_autoflush:
            tags = (await session.scalars(select(StockTag))).all()
            blacklists = (await session.scalars(select(StockBlacklist))).all()
            quote_names = (await session.execute(select(StockSpot.code, StockSpot.name))).all()
        return {t.code: t for t in tags}, {b.code: b for b in blacklists}, dict(quote_names)

    async def filter_signals(self, session: AsyncSession, items: list[dict],
                              code_field: str = "code",
                              exclude_suspended: bool = True,
                              exclude_blocked: bool = True,
                              mark_observe: bool = True) -> list[dict]:
        """过滤信号列表 — 移除停牌/退市/ST，标记观察标的
        
        items: 信号列表，每个dict必须包含 code_field 指定的字段
        返回: 过滤后的列表(不修改原列表)
        
        对于 blocked/suspended 的股票：直接移除
        对于 observe_only 的股票：保留但加 tag="👁️ 仅观察" 标记
        """
        at = datetime.now()
        tags, blacklists, quote_names = await self._load_status_rows(session)
        result = []
        for item in items:
            code = item.get(code_field)
            # 不猜代码、不让缺失身份成为默认可交易，也不修改原候选证据。
            lookup_code = code if isinstance(code, str) else ""
            status = self.resolve_status(
                code, tags.get(lookup_code), name=item.get("name"),
                blacklist=blacklists.get(lookup_code),
                quote_name=quote_names.get(lookup_code), at=at,
            )
            if exclude_suspended and status["board_tag"] == TAG_SUSPENDED:
                continue
            if exclude_blocked and status["board_tag"] == TAG_BLOCKED:
                continue
            new_item = dict(item)
            new_item["is_tradeable"] = status["is_tradeable"]
            new_item["stock_status"] = status
            if not status["is_tradeable"]:
                if "buy_allowed" in new_item:
                    new_item["buy_allowed"] = False
                if mark_observe:
                    new_item["tag"] = TAG_LABEL_MAP.get(status["board_tag"], "❓ 身份待核验")
            result.append(new_item)
        return result


# 全局单例
stock_tagger = StockTagger()
