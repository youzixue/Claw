"""推送去重限频

规则:
1. 同股推送冷却(PUSH_STOCK_COOLDOWN)
2. 每小时推送上限(PUSH_HOURLY_LIMIT)
3. 相同标题去重(5分钟内)
"""

import time
from loguru import logger

from app.config.settings import settings
from app.push.channels.base import PushMessage


class PushThrottle:
    """推送限频器"""

    def __init__(self):
        self._stock_last_push: dict[str, float] = {}      # {code: timestamp}
        self._hourly_count: int = 0
        self._hourly_reset: float = time.time()
        self._title_last_push: dict[str, float] = {}       # {title_hash: timestamp}
        self._stock_last_context: dict[str, dict] = {}     # {code: {grade, identity}}

    @staticmethod
    def _is_anomaly_grade_upgrade(message: PushMessage, previous: dict | None) -> bool:
        """A2升级到A1时穿透一次同股冷却。

        同一信号升级时identity本就应稳定，不能把“identity必须变化”
        当作穿透条件。成功发出A1后上下文会更新为A1，后续同级不会重复穿透。
        """
        if message.category != "anomaly" or not previous:
            return False
        extra = message.extra or {}
        grade_rank = {
            "B类观察候选": 0,
            "A2 盘口确认后执行": 1,
            "A1 可直接执行": 2,
        }
        current_grade = str(extra.get("setup_grade") or "")
        previous_grade = str(previous.get("setup_grade") or "")
        return grade_rank.get(current_grade, -1) > grade_rank.get(previous_grade, -1)

    @staticmethod
    def _is_rapid_rise_strength_upgrade(
        message: PushMessage,
        previous: dict | None,
    ) -> bool:
        """60秒急拉进入strong/追赶/封板阶段时允许穿透一次。"""
        if message.category != "anomaly" or not previous:
            return False
        current_identity = str((message.extra or {}).get("signal_identity") or "")
        previous_identity = str(previous.get("signal_identity") or "")
        prefix = f"{message.stock_code}|breakthrough|positive_acceleration|"
        if not (
            current_identity.startswith(prefix)
            and previous_identity.startswith(prefix)
        ):
            return False
        phase_rank = {
            "rolling_watch": 0,
            "rolling_medium": 1,
            "regular": 1,
            "rolling_strong": 2,
            "catchup": 3,
            "limit_up": 4,
        }
        # B类观察消息会在稳定信号键后追加 ``|observation``。若只取最后
        # 一段，medium→strong升级会被解析成同一个未知阶段并遭15分钟冷却。
        def _resolve_phase(identity: str) -> str:
            return next(
                (
                    part
                    for part in reversed(identity.split("|"))
                    if part in phase_rank
                ),
                "",
            )

        current_phase = _resolve_phase(current_identity)
        previous_phase = _resolve_phase(previous_identity)
        current_rank = phase_rank.get(current_phase, -1)
        previous_rank = phase_rank.get(previous_phase, -1)
        return current_rank >= phase_rank["rolling_strong"] and current_rank > previous_rank

    def should_send(self, message: PushMessage) -> bool:
        """只判断是否可发送；成功发送后由 ``mark_sent`` 写入冷却状态。"""
        now = time.time()

        # 1. 每小时上限
        if now - self._hourly_reset > 3600:
            self._hourly_count = 0
            self._hourly_reset = now

        if self._hourly_count >= settings.PUSH_HOURLY_LIMIT:
            logger.debug(f"推送限频: 达到每小时上限({settings.PUSH_HOURLY_LIMIT})")
            return False

        # 2. 仅风险告警允许绕过同股/标题冷却，买点信号始终限频。
        if (
            message.category == "risk"
            and message.priority >= settings.URGENT_SCORE_THRESHOLD // 10
        ):
            return True

        # 3. 同股冷却
        if message.stock_code:
            last = self._stock_last_push.get(message.stock_code, 0)
            cooldown = settings.PUSH_STOCK_COOLDOWN
            if message.category == "anomaly":
                cooldown = max(cooldown, settings.ANOMALY_PUSH_STOCK_COOLDOWN)
            is_grade_upgrade = self._is_anomaly_grade_upgrade(
                message,
                self._stock_last_context.get(message.stock_code),
            )
            is_strength_upgrade = self._is_rapid_rise_strength_upgrade(
                message,
                self._stock_last_context.get(message.stock_code),
            )
            if now - last < cooldown and not (is_grade_upgrade or is_strength_upgrade):
                logger.debug(f"推送限频: {message.stock_code} 冷却中")
                return False

        # 4. 标题去重(5分钟)
        title_key = hash(message.title)
        last = self._title_last_push.get(title_key, 0)
        if now - last < 300:  # 5分钟
            logger.debug(f"推送限频: 标题重复 {message.title[:20]}")
            return False

        return True

    def mark_sent(self, message: PushMessage) -> None:
        """仅在至少一个渠道发送成功后占用限频额度。"""
        now = time.time()
        if now - self._hourly_reset > 3600:
            self._hourly_count = 0
            self._hourly_reset = now
        if message.stock_code:
            self._stock_last_push[message.stock_code] = now
            if message.category == "anomaly":
                extra = message.extra or {}
                self._stock_last_context[message.stock_code] = {
                    "setup_grade": str(extra.get("setup_grade") or ""),
                    "signal_identity": str(extra.get("signal_identity") or ""),
                }
        self._title_last_push[hash(message.title)] = now
        self._hourly_count += 1

    def get_stats(self) -> dict:
        """获取限频统计"""
        return {
            "hourly_count": self._hourly_count,
            "hourly_limit": settings.PUSH_HOURLY_LIMIT,
            "tracked_stocks": len(self._stock_last_push),
            "tracked_titles": len(self._title_last_push),
        }


# 全局限频器
push_throttle = PushThrottle()
