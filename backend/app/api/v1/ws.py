"""WebSocket 实时推送"""

import json
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

router = APIRouter()


class ConnectionManager:
    """WebSocket连接管理器"""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket连接: 当前{len(self.active_connections)}个")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        logger.info(f"WebSocket断开: 当前{len(self.active_connections)}个")

    async def broadcast(self, message: dict):
        """广播消息"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)

    async def push_signal(self, signal_data: dict):
        """推送信号"""
        await self.broadcast({
            "channel": "signal",
            "data": signal_data,
            "timestamp": datetime.now().isoformat(),
        })

    async def push_anomaly(self, anomaly_data: dict):
        """推送异动"""
        await self.broadcast({
            "channel": "anomaly",
            "data": anomaly_data,
            "timestamp": datetime.now().isoformat(),
        })

    async def push_sentiment(self, sentiment_data: dict):
        """推送情绪变更"""
        await self.broadcast({
            "channel": "sentiment",
            "data": sentiment_data,
            "timestamp": datetime.now().isoformat(),
        })


# 全局单例
ws_manager = ConnectionManager()


@router.websocket("")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket端点"""
    await ws_manager.connect(websocket)
    try:
        while True:
            # 接收客户端消息(心跳等)
            data = await websocket.receive_text()
            msg = json.loads(data) if data else {}
            if msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
