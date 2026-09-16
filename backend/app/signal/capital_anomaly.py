"""资金异动监测 — 主力异动/北向异动/大单追踪 + 冲高回落检测

v2: 增加冲高回落(pump and dump)检测，为推送模板提供真实判断依据
"""

from dataclasses import dataclass, field
from typing import Optional
from loguru import logger

from app.data.fund_flow_clock import main_fund_values_valid


@dataclass
class CapitalAnomaly:
    """资金异动结果"""
    code: str
    name: str
    anomaly_type: str              # main_inflow/northbound/volume_price/consecutive/pump_dump
    level: str                     # critical/major/minor
    score: float                   # 异动评分 0-100
    detail: dict = field(default_factory=dict)
    description: str = ""
    is_pump_and_dump: bool = False  # 冲高回落标记


class CapitalAnomalyDetector:
    """资金异动检测器"""

    # 阈值(可配置)
    MAIN_INFLOW_CRITICAL = 5e8     # 主力净流入 > 5亿 = critical
    MAIN_INFLOW_MAJOR = 2e8        # > 2亿 = major
    MAIN_INFLOW_MINOR = 5e7        # > 5000万 = minor
    MAIN_INFLOW_PCT_CRITICAL = 10.0  # 东财主力净流入占比 > 10%
    MAIN_INFLOW_PCT_MAJOR = 5.0
    MAIN_INFLOW_PCT_MINOR = 2.0

    CONSECUTIVE_DAYS_MAJOR = 3     # 连续3日净流入 = major
    CONSECUTIVE_DAYS_CRITICAL = 5  # 连续5日 = critical

    VOLUME_RATIO_HIGH = 2.0        # 量比 > 2 = 异常放量
    VOLUME_RATIO_EXTREME = 3.0     # 量比 > 3 = 极端放量

    # 冲高回落检测阈值
    PUMP_DUMP_INTRADAY_PULLBACK = 0.5  # 冲高后回撤超过冲高幅度的50%
    PUMP_DUMP_TAIL_RATIO = 0.6        # 上影线占全日振幅比 > 60%

    @staticmethod
    def _fmt_net_amount_yi(value: float) -> str:
        abs_yi = abs(float(value)) / 1e8
        return f"-{abs_yi:.1f}亿" if float(value) < 0 else f"{abs_yi:.1f}亿"

    def detect_pump_and_dump(
        self,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: float = 0,
        avg_volume_20d: float = 0,
        morning_high: float = 0,       # 上午最高价
        afternoon_low: float = 0,       # 下午最低价
    ) -> dict:
        """检测冲高回落(pump and dump)

        识别逻辑:
        1. 日内冲高幅度大但收盘回落 — 上影线长
        2. 上午冲高+下午回落 — 典型诱多走势
        3. 放量冲高 — 吸引跟风
        4. 收盘接近最低价 — 全日弱势

        Args:
            open_price: 开盘价
            high_price: 最高价
            low_price: 最低价
            close_price: 收盘价
            volume: 成交量
            avg_volume_20d: 20日平均成交量
            morning_high: 上午最高价(如有分时数据)
            afternoon_low: 下午最低价(如有分时数据)

        Returns:
            {
                "is_pump_and_dump": bool,
                "confidence": float,  # 0-1
                "pump_pct": float,    # 冲高幅度%
                "pullback_pct": float, # 回落幅度%
                "upper_shadow_ratio": float, # 上影线比
                "pattern": str,       # 描述
                "warnings": list,     # 风险提示
            }
        """
        result = {
            "is_pump_and_dump": False,
            "confidence": 0,
            "pump_pct": 0,
            "pullback_pct": 0,
            "upper_shadow_ratio": 0,
            "pattern": "",
            "warnings": [],
        }

        if open_price <= 0 or high_price <= 0:
            return result

        # === 1. 上影线分析 ===
        amplitude = high_price - low_price
        if amplitude <= 0:
            return result

        upper_shadow = high_price - max(open_price, close_price)
        upper_shadow_ratio = upper_shadow / amplitude
        result["upper_shadow_ratio"] = round(upper_shadow_ratio, 4)

        # === 2. 冲高幅度与回撤 ===
        pump_pct = (high_price - open_price) / open_price * 100  # 冲高幅度
        pullback_pct = (high_price - close_price) / (high_price - open_price) * 100 if (high_price - open_price) > 0 else 0
        result["pump_pct"] = round(pump_pct, 2)
        result["pullback_pct"] = round(pullback_pct, 2)

        # === 3. 综合判断 ===
        confidence = 0

        # 条件1: 长上影线 (上影线占振幅60%以上)
        if upper_shadow_ratio >= self.PUMP_DUMP_TAIL_RATIO:
            confidence += 0.3
            result["pattern"] = "长上影线"

        # 条件2: 冲高后大幅回撤 (回撤超过冲高幅度的50%)
        if pump_pct > 2 and pullback_pct >= self.PUMP_DUMP_INTRADAY_PULLBACK * 100:
            confidence += 0.25
            if result["pattern"]:
                result["pattern"] += "+大幅回撤"
            else:
                result["pattern"] = "大幅回撤"

        # 条件3: 上午冲高+下午回落 (有分时数据时)
        if morning_high > 0 and afternoon_low > 0:
            if morning_high > open_price * 1.02 and afternoon_low < morning_high * 0.97:
                confidence += 0.3
                if result["pattern"]:
                    result["pattern"] += "+上午冲高下午回落"
                else:
                    result["pattern"] = "上午冲高下午回落"

        # 条件4: 放量冲高 (量比>2)
        volume_ratio = volume / avg_volume_20d if avg_volume_20d > 0 else 1.0
        if volume_ratio >= 2.0 and pump_pct > 3:
            confidence += 0.15
            if result["pattern"]:
                result["pattern"] += "+放量冲高"

        # 条件5: 收盘接近最低价 (弱势确认)
        close_to_low = (close_price - low_price) / amplitude if amplitude > 0 else 0
        if close_to_low < 0.2:  # 收盘在最低价20%范围内
            confidence += 0.1

        result["confidence"] = round(min(1.0, confidence), 2)

        # 判定: confidence >= 0.5 判定为冲高回落
        if confidence >= 0.5:
            result["is_pump_and_dump"] = True
            result["warnings"].append(
                f"冲高{pump_pct:.1f}%后回落{pullback_pct:.0f}%，{result['pattern']}，疑似诱多"
            )
        elif confidence >= 0.3:
            result["warnings"].append(
                f"盘中冲高{pump_pct:.1f}%，有回落实象，注意确认"
            )

        return result

    def detect(
        self,
        code: str,
        name: str,
        fund_data: dict,
        recent_funds: list[dict] = None,
        volume_ratio: float = 1.0,
        change_pct: float = 0,
        # 冲高回落检测参数
        open_price: float = 0,
        high_price: float = 0,
        low_price: float = 0,
        close_price: float = 0,
        volume: float = 0,
        avg_volume_20d: float = 0,
        morning_high: float = 0,
        afternoon_low: float = 0,
    ) -> list[CapitalAnomaly]:
        """检测个股资金异动

        Args:
            code: 股票代码
            name: 股票名称
            fund_data: 当日资金数据 {main_net_inflow, big_net_inflow, ...}
            recent_funds: 近N日资金数据列表
            volume_ratio: 量比
            change_pct: 涨跌幅%
            open/high/low/close: OHLC价格(冲高回落检测)
            volume: 成交量
            avg_volume_20d: 20日均量
            morning_high: 上午最高价
            afternoon_low: 下午最低价

        Returns:
            异动列表(可能多种异动同时触发)
        """
        anomalies = []

        main_inflow = fund_data.get("main_net_inflow", 0)
        main_inflow_pct = float(fund_data.get("main_net_inflow_pct", 0) or 0)
        super_inflow = float(fund_data.get("super_net_inflow", 0) or 0)
        super_inflow_pct = float(fund_data.get("super_net_inflow_pct", 0) or 0)
        big_inflow = float(fund_data.get("big_net_inflow", 0) or 0)
        big_inflow_pct = float(fund_data.get("big_net_inflow_pct", 0) or 0)

        # 1. 主力资金大幅流入
        amount_level = 0
        if abs(main_inflow) > self.MAIN_INFLOW_CRITICAL:
            amount_level = 3
        elif abs(main_inflow) > self.MAIN_INFLOW_MAJOR:
            amount_level = 2
        elif abs(main_inflow) > self.MAIN_INFLOW_MINOR:
            amount_level = 1

        pct_level = 0
        if abs(main_inflow_pct) >= self.MAIN_INFLOW_PCT_CRITICAL:
            pct_level = 3
        elif abs(main_inflow_pct) >= self.MAIN_INFLOW_PCT_MAJOR:
            pct_level = 2
        elif abs(main_inflow_pct) >= self.MAIN_INFLOW_PCT_MINOR:
            pct_level = 1

        large_order_alignment = (
            (main_inflow > 0 and super_inflow > 0 and big_inflow > 0)
            or (main_inflow < 0 and super_inflow < 0 and big_inflow < 0)
        )
        level_rank = max(amount_level, pct_level)
        if level_rank >= 3:
            level = "critical"
            score = 90
        elif level_rank >= 2:
            level = "major"
            score = 70
        elif level_rank >= 1:
            level = "minor"
            score = 50
        else:
            level = ""
            score = 0

        is_pump = False
        if level:
            direction = "流入" if main_inflow > 0 else "流出"
            if large_order_alignment:
                score = min(95, score + 5)

            # 冲高回落检测: 仅在资金流入时检测(流出不需要)
            if main_inflow > 0 and open_price > 0:
                pump_result = self.detect_pump_and_dump(
                    open_price=open_price,
                    high_price=high_price,
                    low_price=low_price,
                    close_price=close_price,
                    volume=volume,
                    avg_volume_20d=avg_volume_20d,
                    morning_high=morning_high,
                    afternoon_low=afternoon_low,
                )
                is_pump = pump_result["is_pump_and_dump"]
                if is_pump:
                    # 冲高回落时降级
                    level = "minor" if level == "minor" else "major"
                    score = max(score - 20, 40)

            anomalies.append(CapitalAnomaly(
                code=code,
                name=name,
                anomaly_type="main_inflow",
                level=level,
                score=score,
                detail={
                    "main_net_inflow": main_inflow,
                    "main_net_inflow_pct": main_inflow_pct,
                    "super_net_inflow": super_inflow,
                    "super_net_inflow_pct": super_inflow_pct,
                    "big_net_inflow": big_inflow,
                    "big_net_inflow_pct": big_inflow_pct,
                    "large_order_alignment": large_order_alignment,
                    "direction": direction,
                },
                description=f"资金净额 {self._fmt_net_amount_yi(main_inflow)}",
                is_pump_and_dump=is_pump,
            ))

            # 如果检测到冲高回落，额外添加一条异动记录
            if is_pump:
                pump_desc = pump_result.get("pattern", "冲高回落")
                anomalies.append(CapitalAnomaly(
                    code=code,
                    name=name,
                    anomaly_type="pump_dump",
                    level="major",
                    score=75,
                    detail=pump_result,
                    description=f"冲高回落({pump_desc})，疑似诱多",
                    is_pump_and_dump=True,
                ))

        # 2. 连续净流入/流出。Caller supplies exactly five verified completed
        # sessions, newest first; absent/invalid windows are not partial evidence.
        if (recent_funds and len(recent_funds) == 5
                and all(main_fund_values_valid(f.get("main_net_inflow"), 0) for f in recent_funds)):
            recent_values = [float(f["main_net_inflow"]) for f in recent_funds]
            consecutive_in = consecutive_out = 0
            positive_total = negative_total = 0.0
            direction = 1 if recent_values[0] > 0 else -1 if recent_values[0] < 0 else 0
            for value in recent_values:
                # Count the current uninterrupted run, not all same-sign days
                # scattered across the window; zero also breaks continuity.
                if direction == 1 and value > 0:
                    consecutive_in += 1
                    positive_total += value
                elif direction == -1 and value < 0:
                    consecutive_out += 1
                    negative_total -= value
                else:
                    break

            # Finite daily amounts can still overflow when summed. Do not emit
            # an infinite amount as supporting signal evidence.
            if not main_fund_values_valid(positive_total, negative_total):
                consecutive_in = consecutive_out = 0
                positive_total = negative_total = 0.0

            if (
                consecutive_in >= self.CONSECUTIVE_DAYS_CRITICAL
                and positive_total >= self.MAIN_INFLOW_CRITICAL * 2
            ):
                anomalies.append(CapitalAnomaly(
                    code=code, name=name,
                    anomaly_type="consecutive",
                    level="critical", score=85,
                    detail={
                        "consecutive_in_days": consecutive_in,
                        "recent_total_inflow": positive_total,
                    },
                    description=f"主力连续{consecutive_in}日净流入",
                ))
            elif (
                consecutive_in >= self.CONSECUTIVE_DAYS_MAJOR
                and positive_total >= self.MAIN_INFLOW_CRITICAL
            ):
                anomalies.append(CapitalAnomaly(
                    code=code, name=name,
                    anomaly_type="consecutive",
                    level="major", score=65,
                    detail={
                        "consecutive_in_days": consecutive_in,
                        "recent_total_inflow": positive_total,
                    },
                    description=f"主力连续{consecutive_in}日净流入",
                ))

            if (
                consecutive_out >= self.CONSECUTIVE_DAYS_CRITICAL
                and negative_total >= self.MAIN_INFLOW_CRITICAL * 2
            ):
                anomalies.append(CapitalAnomaly(
                    code=code, name=name,
                    anomaly_type="consecutive",
                    level="critical", score=80,
                    detail={
                        "consecutive_out_days": consecutive_out,
                        "recent_total_outflow": negative_total,
                    },
                    description=f"主力连续{consecutive_out}日净流出",
                ))
            elif (
                consecutive_out >= self.CONSECUTIVE_DAYS_MAJOR
                and negative_total >= self.MAIN_INFLOW_CRITICAL
            ):
                anomalies.append(CapitalAnomaly(
                    code=code, name=name,
                    anomaly_type="consecutive",
                    level="major", score=60,
                    detail={
                        "consecutive_out_days": consecutive_out,
                        "recent_total_outflow": negative_total,
                    },
                    description=f"主力连续{consecutive_out}日净流出",
                ))

        # 3. 量价配合异常
        if volume_ratio >= self.VOLUME_RATIO_EXTREME:
            if change_pct > 3:
                desc = "极端放量上涨"
                score = 85
            elif change_pct < -3:
                desc = "极端放量下跌"
                score = 80
            else:
                desc = "极端放量横盘"
                score = 60

            anomalies.append(CapitalAnomaly(
                code=code, name=name,
                anomaly_type="volume_price",
                level="critical", score=score,
                detail={"volume_ratio": volume_ratio, "change_pct": change_pct},
                description=desc,
            ))
        elif volume_ratio >= self.VOLUME_RATIO_HIGH:
            if change_pct > 2:
                desc = "放量上涨"
                score = 70
            elif change_pct < -2:
                desc = "放量下跌"
                score = 65
            else:
                desc = "放量横盘"
                score = 45

            anomalies.append(CapitalAnomaly(
                code=code, name=name,
                anomaly_type="volume_price",
                level="major", score=score,
                detail={"volume_ratio": volume_ratio, "change_pct": change_pct},
                description=desc,
            ))

        # 4. 缩量上涨(特殊信号)
        if volume_ratio < 0.5 and change_pct > 3:
            anomalies.append(CapitalAnomaly(
                code=code, name=name,
                anomaly_type="volume_price",
                level="major", score=70,
                detail={"volume_ratio": volume_ratio, "change_pct": change_pct},
                description="缩量上涨(筹码锁定)",
            ))

        return anomalies

    def scan_batch(
        self,
        stocks_data: list[dict],
    ) -> list[CapitalAnomaly]:
        """批量扫描资金异动

        Args:
            stocks_data: [{code, name, fund_data, recent_funds, volume_ratio, change_pct, ...}]

        Returns:
            所有异动结果(按score降序)
        """
        all_anomalies = []
        for s in stocks_data:
            anomalies = self.detect(
                code=s["code"],
                name=s.get("name", ""),
                fund_data=s.get("fund_data", {}),
                recent_funds=s.get("recent_funds", []),
                volume_ratio=s.get("volume_ratio", 1.0),
                change_pct=s.get("change_pct", 0),
                open_price=s.get("open_price", 0),
                high_price=s.get("high_price", 0),
                low_price=s.get("low_price", 0),
                close_price=s.get("close_price", 0),
                volume=s.get("volume", 0),
                avg_volume_20d=s.get("avg_volume_20d", 0),
                morning_high=s.get("morning_high", 0),
                afternoon_low=s.get("afternoon_low", 0),
            )
            all_anomalies.extend(anomalies)

        all_anomalies.sort(key=lambda a: a.score, reverse=True)
        return all_anomalies
