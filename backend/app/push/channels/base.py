"""推送渠道基类"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class PushMessage:
    """推送消息"""
    title: str
    content: str
    msg_type: str = "info"        # info/warning/danger/signal
    category: str = "general"     # anomaly/review/risk/signal
    stock_code: str = ""
    stock_name: str = ""
    priority: int = 5             # 1-10, 10最高
    extra: dict = None


class PushChannel(ABC):
    """推送渠道基类"""

    channel_name: str = ""

    @abstractmethod
    async def send(self, message: PushMessage) -> bool:
        """发送消息"""
        ...

    @abstractmethod
    async def is_available(self) -> bool:
        """渠道是否可用"""
        ...
