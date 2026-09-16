"""龙头股识别 — 辨识度、可交易性与看A做B分离。

龙头可以先于板块梯队出现，因此板块宽度只加分、不作为单股龙头的硬否决。
一字板同样可以是高辨识度龙头，但可交易性必须显著降级。板块确认A后，再从
低位、未涨停且量价开始转强的成分股中选择B，避免把普通同概念股包装成补涨。
"""

from copy import deepcopy
from dataclasses import dataclass, field

from app.data.fund_flow_clock import main_fund_values_valid


def _main_fund_amount(item: dict) -> float | None:
    """Numeric model inputs remain supported; explicit rejected evidence cannot score."""
    if item.get("main_fund_status", "ok") != "ok":
        return None
    value = item.get("main_net_inflow")
    return float(value) if main_fund_values_valid(value, 0) else None


# 只有产业链因果关系明确的主题，才允许跨同花顺一级行业映射 B。
# 政策标签、人口主题、地域主题和泛消费概念不在此列。
CROSS_INDUSTRY_CAUSAL_THEME_TOKENS = (
    "黄金",
    "金属铜",
    "有色金属",
    "稀土",
    "小金属",
    "钨",
    "锗",
    "锂矿",
    "钴",
    "磷化工",
    "氟化工",
    "煤炭",
    "油气",
    "天然气",
    "光模块",
    "CPO",
    "印制电路板",
    "PCB",
    "液冷服务器",
    "人形机器人",
    "减速器",
    "商业航天",
    "卫星导航",
    "固态电池",
    "储能",
    "光伏",
    "风电",
    "核电",
    "电网设备",
    "半导体",
)

THEME_REASON_FAMILIES = (
    ("黄金", "贵金属"),
    ("有色金属", "工业金属", "金属铜", "铜", "铝", "稀土", "小金属", "钨", "锗", "锂", "钴"),
    ("影视", "院线", "传媒", "IP经济", "文化"),
    ("食品", "饮料", "乳品", "白酒", "调味", "零食"),
    ("光模块", "CPO", "光通信"),
    ("数据中心", "算力", "智算", "东数西算", "算力租赁", "AI服务器", "服务器"),
    ("印制电路板", "PCB", "覆铜板"),
    ("机器人", "减速器", "自动化设备"),
    ("商业航天", "卫星", "航天军工"),
    ("固态电池", "锂电池", "储能"),
    ("光伏", "风电", "核电", "绿色电力"),
    ("电网设备", "智能电网", "输变电"),
    ("半导体", "芯片", "集成电路"),
    ("煤炭", "煤化工"),
    ("油气", "石油", "天然气"),
)


@dataclass
class DragonHeadResult:
    """龙头股识别结果。"""

    code: str
    name: str
    sector_code: str
    sector_name: str
    score: float
    rank: int
    level: str
    change_pct: float = 0
    main_net_inflow: float | None = None
    main_fund_status: str = "unknown"
    main_fund_decision_at: str | None = None
    main_fund_display: dict | None = None
    consecutive_days: int = 1
    turnover: float = 0
    seal_amount: float = 0
    reasons: list = field(default_factory=list)
    recognition_score: float = 0
    tradability_score: float = 0
    leader_type: str = ""
    link_role: str = ""
    leader_code: str = ""
    leader_name: str = ""
    leader_recognition_score: float = 0
    leader_tradability_score: float = 0
    leader_origin_type: str = ""
    linkage_score: float = 0
    business_relevance_score: float = 0
    follower_shape_score: float = 0
    theme_alignment_score: float = 0
    leader_driver_reason: str = ""
    leader_industry: str = ""
    follower_industry: str = ""
    sector_limit_up_count: int = 0
    sector_up_ratio: float = 0
    event_grade: str = ""
    event_score: float = 0
    news_evidence: dict = field(default_factory=dict)
    trend_leadership_score: float = 0
    first_seal_time: str = ""
    break_count: int = 0
    is_one_word: bool = False


class DragonHeadScanner:
    """板块龙头与A→B映射扫描器。"""

    @staticmethod
    def _rank_percentile(order: list[dict], code: str) -> float:
        if not order:
            return 0.0
        if len(order) == 1:
            return 100.0
        for index, item in enumerate(order):
            if str(item.get("code") or "") == code:
                return max(0.0, 100.0 * (len(order) - 1 - index) / (len(order) - 1))
        return 0.0

    @staticmethod
    def _initiation_score(item: dict) -> float:
        seal_time = str(item.get("limit_up_time") or "").replace(":", "")
        if item.get("is_limit_up") and seal_time:
            if seal_time <= "093500":
                return 100.0
            if seal_time <= "100000":
                return 88.0
            if seal_time <= "103000":
                return 72.0
            if seal_time <= "133000":
                return 52.0
            return 35.0
        change_pct = float(item.get("change_pct") or 0)
        return 55.0 if change_pct >= 5 else 40.0 if change_pct >= 2 else 20.0

    @staticmethod
    def _base_score(item: dict) -> float:
        return_20d = float(item.get("return_20d") or 0)
        position_120 = float(item.get("position_120") or 0.5)
        if return_20d <= 15 and position_120 <= 0.45:
            return 100.0
        if return_20d <= 30 and position_120 <= 0.65:
            return 78.0
        if return_20d <= 50:
            return 52.0
        return 22.0

    @staticmethod
    def _trend_leadership_score(item: dict) -> float:
        """识别非当日涨停的趋势龙头，避免龙头榜只剩最高连板。"""
        price = float(item.get("price") or 0)
        ma5 = float(item.get("ma5") or 0)
        ma10 = float(item.get("ma10") or 0)
        ma20 = float(item.get("ma20") or 0)
        ma20_slope = float(item.get("ma20_slope_5d") or 0)
        return_5d = float(item.get("return_5d") or 0)
        return_20d = float(item.get("return_20d") or 0)
        change_pct = float(item.get("change_pct") or 0)
        volume_ratio = float(item.get("volume_ratio") or 0)
        fund_inflow = (_main_fund_amount(item) or 0)

        has_trend_evidence = bool(
            (price > 0 and ma20 > 0)
            or abs(return_5d) > 0.01
            or abs(return_20d) > 0.01
        )
        if not has_trend_evidence:
            return 0.0

        score = 0.0
        score += 18.0 if price > 0 and ma20 > 0 and price >= ma20 else 0.0
        score += 18.0 if min(ma5, ma10, ma20) > 0 and ma5 >= ma10 >= ma20 else 9.0 if ma5 > 0 and ma10 > 0 and ma5 >= ma10 else 0.0
        score += 14.0 if ma20_slope >= 0.5 else 8.0 if ma20_slope > 0 else 0.0
        score += 16.0 if 8.0 <= return_20d <= 55.0 else 9.0 if 3.0 <= return_20d < 8.0 else 4.0 if 55.0 < return_20d <= 75.0 else 0.0
        score += 12.0 if -2.5 <= return_5d <= 18.0 else 6.0 if 18.0 < return_5d <= 28.0 else 0.0
        score += 8.0 if -2.5 <= change_pct <= 6.0 else 3.0 if 6.0 < change_pct < 9.5 else 0.0
        score += 7.0 if 0.8 <= volume_ratio <= 2.8 else 3.0 if 0.5 <= volume_ratio < 0.8 else 0.0
        score += 7.0 if fund_inflow > 0 else 0.0
        return round(min(score, 100.0), 1)

    @staticmethod
    def _tradability_score(item: dict) -> float:
        score = 70.0
        turnover = float(item.get("turnover") or 0)
        break_count = int(item.get("break_count") or 0)
        return_20d = float(item.get("return_20d") or 0)
        change_pct = float(item.get("change_pct") or 0)
        if bool(item.get("is_one_word")):
            score -= 48.0
        elif 3 <= turnover <= 15:
            score += 20.0
        elif 1 <= turnover < 3 or 15 < turnover <= 22:
            score += 8.0
        else:
            score -= 15.0
        # 龙头辨识度与当下买点分离。已经封板的票即使换手健康，也不能在
        # “可交易”列给出接近满分，避免页面把身份识别误读成追板指令。
        if item.get("is_limit_up") and not bool(item.get("is_one_word")):
            score -= 20.0
        elif change_pct >= 4.0:
            score -= 8.0
        score -= max(break_count - 1, 0) * 7.0
        if return_20d > 50:
            score -= 20.0
        if return_20d > 90:
            score -= 20.0
        return round(max(0.0, min(score, 100.0)), 1)

    @staticmethod
    def _industry_parts(value: str | None) -> list[str]:
        return [part.strip() for part in str(value or "").split("-") if part.strip()]

    @classmethod
    def _business_relevance_score(
        cls,
        leader: dict,
        follower: dict,
        sector_name: str,
    ) -> float:
        """判断 B 是否属于龙头的核心业务链，而非外围概念沾边。"""
        leader_industry = cls._industry_parts(leader.get("primary_industry"))
        follower_industry = cls._industry_parts(follower.get("primary_industry"))
        causal_theme = any(
            token.lower() in str(sector_name or "").lower()
            for token in CROSS_INDUSTRY_CAUSAL_THEME_TOKENS
        )
        if leader_industry and follower_industry:
            if leader_industry[:3] == follower_industry[:3]:
                return 100.0
            if leader_industry[:2] == follower_industry[:2]:
                return 92.0
            if leader_industry[0] == follower_industry[0] and causal_theme:
                return 82.0
        if causal_theme:
            return 76.0
        return 0.0

    @staticmethod
    def _follower_shape_score(item: dict) -> float:
        """B 必须自身处于转强形态，不能只靠 A 的热度被动入池。"""
        price = float(item.get("price") or 0)
        ma5 = float(item.get("ma5") or 0)
        ma10 = float(item.get("ma10") or 0)
        ma20 = float(item.get("ma20") or 0)
        ma20_slope = float(item.get("ma20_slope_5d") or 0)
        return_5d = float(item.get("return_5d") or 0)
        position_20 = float(item.get("position_20") or 0)
        volume_ratio = float(item.get("volume_ratio") or 0)
        change_pct = float(item.get("change_pct") or 0)
        fund_inflow = (_main_fund_amount(item) or 0)
        if min(price, ma5, ma10, ma20) <= 0:
            return 0.0

        score = 0.0
        score += 18.0 if price >= ma20 * 0.995 else 0.0
        score += 14.0 if ma5 >= ma10 * 0.995 else 0.0
        score += 12.0 if ma10 >= ma20 * 0.98 else 0.0
        score += 15.0 if ma20_slope >= 0.3 else 10.0 if ma20_slope >= 0 else 0.0
        score += 12.0 if -2.0 <= return_5d <= 10.0 else 7.0 if 10.0 < return_5d <= 18.0 else 0.0
        score += 12.0 if 0.55 <= position_20 <= 0.95 else 8.0 if position_20 > 0.95 else 0.0
        score += 12.0 if 1.15 <= volume_ratio <= 2.8 else 6.0 if 1.0 <= volume_ratio < 1.15 else 0.0
        score += 8.0 if 0.5 <= change_pct <= 4.8 else 0.0
        score += 9.0 if fund_inflow > 0 else 0.0
        return round(min(score, 100.0), 1)

    @staticmethod
    def _theme_alignment_score(leader: dict, sector_name: str) -> float:
        """龙头当日涨停归因必须与用于联动的主题一致。"""
        sector = str(sector_name or "").replace("概念", "").replace("板块", "").strip()
        reason = str(leader.get("limit_up_reason") or "").strip()
        if not sector or not reason:
            return 0.0
        if sector in reason or reason in sector:
            return 100.0
        for family in THEME_REASON_FAMILIES:
            if any(token in sector for token in family) and any(token in reason for token in family):
                return 90.0
        return 0.0

    def scan_sector(
        self,
        sector_code: str,
        sector_name: str,
        stocks: list[dict],
        *,
        allow_linkage: bool = False,
    ) -> list[DragonHeadResult]:
        if not stocks:
            return []

        n = len(stocks)
        by_change = sorted(stocks, key=lambda item: float(item.get("change_pct") or 0), reverse=True)
        by_fund = sorted(
            [item for item in stocks if _main_fund_amount(item) is not None],
            key=lambda item: _main_fund_amount(item), reverse=True,
        )
        by_height = sorted(stocks, key=lambda item: int(item.get("consecutive_days") or 1), reverse=True)
        by_seal_time = sorted(
            [item for item in stocks if item.get("is_limit_up")],
            key=lambda item: str(item.get("limit_up_time") or "999999").replace(":", ""),
        )
        sector_limit_up_count = sum(1 for item in stocks if item.get("is_limit_up"))
        sector_up_ratio = sum(1 for item in stocks if float(item.get("change_pct") or 0) > 0) / max(n, 1)
        max_height = max((int(item.get("consecutive_days") or 1) for item in stocks), default=1)

        results: list[DragonHeadResult] = []
        for item in stocks:
            code = str(item.get("code") or "")
            change_rank = self._rank_percentile(by_change, code)
            fund_rank = self._rank_percentile(by_fund, code) if _main_fund_amount(item) is not None else 0
            height_rank = self._rank_percentile(by_height, code)
            strength_score = change_rank * 0.55 + height_rank * 0.45
            initiation_score = self._initiation_score(item)
            is_height_leader = bool(
                item.get("is_limit_up")
                and int(item.get("consecutive_days") or 1) >= max_height
            )
            is_first_seal = bool(by_seal_time and by_seal_time[0].get("code") == code)

            influence_score = min(sector_limit_up_count, 5) * 8.0 + sector_up_ratio * 25.0
            if is_height_leader:
                influence_score += 22.0
            if is_first_seal:
                influence_score += 13.0
            influence_score = min(influence_score, 100.0)

            event_grade = str(item.get("event_grade") or "")
            event_score = float(item.get("event_score") or 0)
            catalyst_score = event_score
            if event_grade == "hard":
                catalyst_score = max(catalyst_score, 85.0)
            elif event_grade == "medium":
                catalyst_score = max(catalyst_score, 65.0)

            seal_score = float(item.get("seal_quality_score") or 0)
            if seal_score <= 0 and item.get("is_limit_up"):
                seal_score = max(35.0, 82.0 - int(item.get("break_count") or 0) * 10.0)
            base_score = self._base_score(item)
            trend_leadership_score = self._trend_leadership_score(item)
            recognition_score = round(
                strength_score * 0.25
                + initiation_score * 0.20
                + influence_score * 0.20
                + catalyst_score * 0.15
                + seal_score * 0.10
                + base_score * 0.10,
                1,
            )
            if trend_leadership_score >= 60:
                recognition_score = min(
                    100.0,
                    round(recognition_score + min((trend_leadership_score - 60.0) * 0.25, 10.0), 1),
                )
            # 有板块点火时，允许非涨停但量价/资金持续领先的核心票成为趋势龙头。
            # 这只是辨识度，不代表当前位置可追；可交易性仍由独立分数约束。
            if (
                not item.get("is_limit_up")
                and sector_limit_up_count >= 1
                and trend_leadership_score >= 82
                and fund_rank >= 60
                and (_main_fund_amount(item) or 0) > 0
                and -2.5 <= float(item.get("change_pct") or 0) <= 6.0
                and 8.0 <= float(item.get("return_20d") or 0) <= 60.0
            ):
                recognition_score = max(
                    recognition_score,
                    round(68.0 + min((trend_leadership_score - 82.0) * 0.35, 6.0), 1),
                )
            if item.get("is_limit_up") and int(item.get("consecutive_days") or 1) >= 2:
                recognition_score = max(recognition_score, 75.0)
            tradability_score = self._tradability_score(item)
            level = "dragon" if recognition_score >= 72 else "quasi_dragon" if recognition_score >= 55 else "follower"
            if not item.get("is_limit_up") and trend_leadership_score >= 82:
                leader_type = "trend_leader"
            elif event_grade in {"hard", "medium"} and event_score >= 58 and sector_limit_up_count <= 1:
                leader_type = "independent_event"
            elif sector_limit_up_count >= 2:
                leader_type = "sector_leader"
            else:
                leader_type = "emerging_leader"

            reasons: list[str] = []
            if change_rank >= 80:
                reasons.append("涨幅领先")
            if fund_rank >= 80 and (_main_fund_amount(item) or 0) > 0:
                reasons.append("资金大幅流入")
            if int(item.get("consecutive_days") or 1) >= 3:
                reasons.append(f"{int(item.get('consecutive_days') or 1)}连板")
            if is_first_seal:
                reasons.append("最早启动")
            if event_grade in {"hard", "medium"} and event_score >= 58:
                reasons.append("公司事件催化")
            if trend_leadership_score >= 75:
                reasons.append(f"趋势龙头{trend_leadership_score:.0f}分")
            if sector_limit_up_count >= 2:
                reasons.append(f"板块{sector_limit_up_count}只涨停扩散")
            turnover = float(item.get("turnover") or 0)
            if 3 <= turnover <= 15 and not bool(item.get("is_one_word")):
                reasons.append("换手健康")
            if bool(item.get("is_one_word")):
                reasons.append("一字板仅辨识不可追")
            elif item.get("is_limit_up"):
                reasons.append("已封板仅辨识不追价")

            results.append(DragonHeadResult(
                code=code,
                name=str(item.get("name") or ""),
                sector_code=sector_code,
                sector_name=sector_name,
                score=recognition_score,
                rank=0,
                level=level,
                change_pct=float(item.get("change_pct") or 0),
                main_net_inflow=_main_fund_amount(item),
                main_fund_status=item.get("main_fund_status") or (
                    "numeric_input_only" if _main_fund_amount(item) is not None else "unknown"
                ),
                main_fund_decision_at=item.get("main_fund_decision_at"),
                main_fund_display=deepcopy(item.get("main_fund_display")),
                consecutive_days=int(item.get("consecutive_days") or 1),
                turnover=turnover,
                seal_amount=float(item.get("seal_amount") or 0),
                reasons=reasons,
                recognition_score=recognition_score,
                tradability_score=tradability_score,
                leader_type=leader_type,
                sector_limit_up_count=sector_limit_up_count,
                sector_up_ratio=round(sector_up_ratio, 3),
                event_grade=event_grade,
                event_score=round(event_score, 1),
                news_evidence=deepcopy(item.get("news_evidence")) if isinstance(item.get("news_evidence"), dict) else {},
                trend_leadership_score=trend_leadership_score,
                first_seal_time=str(item.get("limit_up_time") or ""),
                break_count=int(item.get("break_count") or 0),
                is_one_word=bool(item.get("is_one_word")),
            ))

        results.sort(key=lambda result: (result.recognition_score, result.tradability_score), reverse=True)
        leader = results[0] if results else None
        if leader and leader.recognition_score >= 68:
            leader.link_role = "leader_a"
        if leader and leader.recognition_score >= 68 and allow_linkage:
            raw_by_code = {str(item.get("code") or ""): item for item in stocks}
            leader_raw = raw_by_code.get(leader.code) or {}
            follower_candidates: list[tuple[float, DragonHeadResult]] = []
            for result in results[1:]:
                raw = raw_by_code.get(result.code) or {}
                change_pct = float(raw.get("change_pct") or 0)
                volume_ratio = float(raw.get("volume_ratio") or 1.0)
                return_20d = float(raw.get("return_20d") or 0)
                amount = float(raw.get("amount") or 0)
                price = float(raw.get("price") or 0)
                avg_price = float(raw.get("avg_price") or 0)
                fund_inflow = (_main_fund_amount(raw) or 0)
                if sector_limit_up_count < 2:
                    continue
                if raw.get("is_limit_up") or not (-0.5 <= change_pct <= 4.5):
                    continue
                if not (1.15 <= volume_ratio <= 2.8) or return_20d > 25 or amount < 8e7:
                    continue
                if avg_price > 0 and price < avg_price * 0.995:
                    continue
                if fund_inflow <= 0:
                    continue
                business_relevance_score = self._business_relevance_score(
                    leader_raw,
                    raw,
                    sector_name,
                )
                follower_shape_score = self._follower_shape_score(raw)
                theme_alignment_score = self._theme_alignment_score(leader_raw, sector_name)
                if (
                    business_relevance_score < 76
                    or follower_shape_score < 68
                    or theme_alignment_score < 75
                ):
                    continue
                linkage_score = (
                    leader.recognition_score * 0.20
                    + business_relevance_score * 0.20
                    + theme_alignment_score * 0.15
                    + follower_shape_score * 0.20
                    + min(max(change_pct, 0.0) / 4.8 * 100.0, 100.0) * 0.10
                    + min(volume_ratio / 2.0 * 100.0, 100.0) * 0.10
                    + self._base_score(raw) * 0.05
                )
                if linkage_score < 72 or result.tradability_score < 50:
                    continue
                result.link_role = "follower_b"
                result.leader_code = leader.code
                result.leader_name = leader.name
                result.leader_recognition_score = leader.recognition_score
                result.leader_tradability_score = leader.tradability_score
                result.leader_origin_type = leader.leader_type
                result.linkage_score = round(linkage_score, 1)
                result.business_relevance_score = round(business_relevance_score, 1)
                result.follower_shape_score = round(follower_shape_score, 1)
                result.theme_alignment_score = round(theme_alignment_score, 1)
                result.leader_driver_reason = str(leader_raw.get("limit_up_reason") or "")
                result.leader_industry = str(leader_raw.get("primary_industry") or "")
                result.follower_industry = str(raw.get("primary_industry") or "")
                result.level = "quasi_dragon"
                result.reasons = [
                    f"看{leader.name}做低位补涨",
                    f"联动评分{linkage_score:.0f}",
                    f"业务相关{business_relevance_score:.0f}",
                    f"归因匹配{theme_alignment_score:.0f}",
                    f"B形态{follower_shape_score:.0f}",
                    *result.reasons,
                ][:6]
                follower_candidates.append((linkage_score, result))
            keep_b = {
                item.code
                for _, item in sorted(follower_candidates, key=lambda pair: pair[0], reverse=True)[:3]
            }
            for result in results:
                if result.link_role == "follower_b" and result.code not in keep_b:
                    result.link_role = ""
                    result.leader_code = ""
                    result.leader_name = ""
                    result.leader_recognition_score = 0.0
                    result.leader_tradability_score = 0.0
                    result.leader_origin_type = ""
                    result.linkage_score = 0.0
                    result.business_relevance_score = 0.0
                    result.follower_shape_score = 0.0
                    result.theme_alignment_score = 0.0
                    result.leader_driver_reason = ""
                    result.leader_industry = ""
                    result.follower_industry = ""

        for index, result in enumerate(results):
            result.rank = index + 1
        return results

    def scan_all_sectors(
        self,
        sectors_data: dict[str, list[dict]],
    ) -> dict[str, list[DragonHeadResult]]:
        results = {}
        for sector_code, stocks in sectors_data.items():
            sector_name = stocks[0].get("sector_name", "") if stocks else ""
            results[sector_code] = self.scan_sector(sector_code, sector_name, stocks)
        return results
