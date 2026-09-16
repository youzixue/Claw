"""WebSocket 推送渠道 — 前端实时推送

复用已有的 WebSocket 管理
"""

from typing import Optional
from loguru import logger

from app.push.channels.base import PushChannel, PushMessage


class WebSocketChannel(PushChannel):
    """WebSocket 实时推送"""

    channel_name = "websocket"

    def __init__(self):
        self._manager = None  # 延迟初始化

    def _get_manager(self):
        """获取 WS 管理器(延迟导入)"""
        if self._manager is None:
            try:
                from app.api.v1.ws import ws_manager
                self._manager = ws_manager
            except ImportError:
                logger.warning("WebSocket 管理器未找到")
        return self._manager

    async def send(self, message: PushMessage) -> bool:
        """通过 WebSocket 广播消息"""
        manager = self._get_manager()
        if not manager:
            return False
        if not getattr(manager, "active_connections", None):
            return False

        try:
            payload = {
                "type": "push",
                "channel": message.category or "push",
                "category": message.category,
                "msg_type": message.msg_type,
                "title": message.title,
                "content": message.content,
                "stock_code": message.stock_code,
                "stock_name": message.stock_name,
                "priority": message.priority,
                "timestamp": __import__("time").time(),
            }

            await manager.broadcast(payload)
            logger.debug(f"WS推送: {message.title[:30]}")
            return True

        except Exception as e:
            logger.error(f"WS推送异常: {e}")
            return False

    async def is_available(self) -> bool:
        """检查 WS 渠道是否可用"""
        manager = self._get_manager()
        return manager is not None and bool(getattr(manager, "active_connections", None))


# 全局实例
ws_channel = WebSocketChannel()
