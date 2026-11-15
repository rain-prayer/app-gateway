"""连接中枢：持有 App 的 WebSocket 连接，负责推送与离线排队。

支持**多条连接同时在线**（手机 + 电脑 + 多标签页）：
以前是"新连接踢掉旧连接"，结果两个页面会互相踢、各自重连，表现为"经常重连"。
现在改成广播：每条消息推给所有在线连接，谁在线谁收。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("neo_app_gateway")

# 离线队列上限。她主动说话频率不高，500 条足够覆盖一天。
MAX_PENDING = 500


class Connection:
    """一条已鉴权的 App 连接。

    同一时刻只允许一条协程写 WS，否则帧会交错。
    """

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._lock = asyncio.Lock()
        self.closed = False

    async def send(self, frame: dict[str, Any]) -> None:
        async with self._lock:
            await self.ws.send_json(frame)

    async def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.ws.close(code=code)
        except Exception:
            pass


class ConnectionHub:
    """多连接中枢。

    设计取舍：App 不在线时**不报错**，把消息塞进 pending 队列并返回「已入队」。
    这样适配器永远返回 success=True，KFC 的 context_snapshot 与真实行为保持一致，
    不会出现「她以为发了、实际没发」的状态分叉。
    """

    def __init__(self) -> None:
        self._conns: set[Connection] = set()
        self._primary: Connection | None = None
        self._pending: deque[dict[str, Any]] = deque(maxlen=MAX_PENDING)
        self._adapter: Any = None
        self._pushed_ok = 0

    def bind_adapter(self, adapter: Any) -> None:
        self._adapter = adapter

    def is_online(self) -> bool:
        return any(not c.closed for c in self._conns)

    def pending_count(self) -> int:
        return len(self._pending)

    def online_count(self) -> int:
        return sum(1 for c in self._conns if not c.closed)

    async def attach(self, conn: Connection) -> None:
        self._conns.add(conn)
        self._primary = conn
        logger.info(f"App 已连接（当前 {self.online_count()} 个连接）")
        self.flush_pending_nowait()

    def detach(self, conn: Connection) -> None:
        self._conns.discard(conn)
        if self._primary is conn:
            alive = [c for c in self._conns if not c.closed]
            self._primary = alive[-1] if alive else None
        logger.info(f"App 连接断开（剩余 {self.online_count()} 个）")

    async def push(self, frame: dict[str, Any]) -> bool:
        """推一帧给所有在线连接。返回 True=至少送达一个，False=全部入离线队列。"""
        sent = 0
        for conn in list(self._conns):
            if conn.closed:
                self._conns.discard(conn)
                continue
            try:
                await conn.send(frame)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"推送失败，移除该连接: {exc}")
                conn.closed = True
                self._conns.discard(conn)

        if sent:
            self._pushed_ok += 1
            return True

        if len(self._pending) == self._pending.maxlen:
            logger.warning("离线队列已满，丢弃最旧的一条")
        self._pending.append(frame)
        return False

    def flush_pending_nowait(self) -> None:
        """连接恢复后把离线消息补推给最新那条连接（不阻塞 attach 流程）。"""
        if not self._pending:
            return
        asyncio.create_task(self._flush())

    async def _flush(self) -> int:
        sent = 0
        while self._pending:
            conn = self._primary
            if conn is None or conn.closed:
                break
            frame = self._pending[0]
            try:
                await conn.send(frame)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"补推中断，剩余 {len(self._pending)} 条: {exc}")
                conn.closed = True
                self._conns.discard(conn)
                break
            self._pending.popleft()
            sent += 1
        if sent:
            logger.info(f"已补推 {sent} 条离线消息")
        return sent

    def make_bot_frame(self, text: str, message_id: str | None = None) -> dict[str, Any]:
        return {
            "type": "msg",
            "role": "bot",
            "id": message_id or "",
            "text": text,
            "ts": time.time(),
        }


_hub: ConnectionHub | None = None


def get_hub() -> ConnectionHub:
    global _hub
    if _hub is None:
        _hub = ConnectionHub()
    return _hub
