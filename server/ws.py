"""WebSocket 长连接。

协议（JSON 文本帧）：
    客户端 → 服务端
        {"type":"auth","token":"..."}                 连接后 5 秒内必须发
        {"type":"send","client_msg_id":"...","text":"..."}
        {"type":"pong"}
    服务端 → 客户端
        {"type":"ready","platform":"app","pending":0}
        {"type":"ack","client_msg_id":"..."}
        {"type":"msg","role":"bot","id":"...","text":"...","ts":123}
        {"type":"ping"}
        {"type":"error","message":"..."}
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.app.plugin_system.api.log_api import get_logger

from ..protocol import PLATFORM
from ..service.bridge import get_bridge
from ..service.hub import Connection, get_hub
from .auth import verify_token

logger = get_logger("neo_app_gateway")

router = APIRouter()

AUTH_TIMEOUT = 5.0
PING_INTERVAL = 20.0


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()

    # ---- 握手：必须在 5 秒内发来正确的 token，否则直接断开 ----
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=AUTH_TIMEOUT)
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401, reason="auth timeout")
        return

    if not isinstance(hello, dict) or hello.get("type") != "auth":
        await websocket.close(code=4401, reason="auth required")
        return
    if not verify_token(str(hello.get("token") or "")):
        await websocket.close(code=4403, reason="bad token")
        return

    hub = get_hub()
    conn = Connection(websocket)
    await hub.attach(conn)
    connected_at = time.time()
    try:
        ua = (websocket.headers.get("user-agent") or "?")[:48]
    except Exception:  # noqa: BLE001
        ua = "?"
    logger.info(f"WS 建立  ua={ua}")

    try:
        await websocket.send_json(
            {
                "type": "ready",
                "platform": PLATFORM,
                "ts": time.time(),
                "pending": hub.pending_count(),
            }
        )
    except Exception:  # noqa: BLE001
        hub.detach(conn)
        return

    ping_task = asyncio.create_task(_ping_loop(websocket))

    try:
        while True:
            frame = await websocket.receive_json()
            await _handle_frame(websocket, frame)
    except WebSocketDisconnect as exc:
        # 把关闭码记下来：1001=页面关了/离开，1006=网络断（异常关闭），
        # 4401=鉴权超时，4403=token 错 —— 这几种处理方式完全不同
        logger.info(
            f"WS 关闭  code={exc.code} reason={(exc.reason or '')[:60]!r} "
            f"时长={time.time() - connected_at:.1f}s"
        )
    except Exception as exc:  # noqa: BLE001
        # 客户端断开时 Starlette 抛的异常类型不固定，统一按断开处理
        logger.info(
            f"WS 异常结束 {type(exc).__name__}: {exc} 时长={time.time() - connected_at:.1f}s"
        )
    finally:
        ping_task.cancel()
        hub.detach(conn)


async def _ping_loop(websocket: WebSocket) -> None:
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await websocket.send_json({"type": "ping", "ts": time.time()})
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return


async def _handle_frame(websocket: WebSocket, frame: Any) -> None:
    if not isinstance(frame, dict):
        return

    ftype = frame.get("type")
    if ftype == "pong":
        return

    if ftype == "send":
        text = str(frame.get("text") or "")
        client_msg_id = frame.get("client_msg_id")
        kind = str(frame.get("kind") or "text")
        media_id = str(frame.get("media_id") or "")
        bridge = get_bridge()
        adapter = bridge._get_adapter()  # noqa: SLF001
        if adapter is None:
            await websocket.send_json({"type": "error", "message": "adapter_not_ready"})
            return
        try:
            result = await bridge.push_incoming(
                text=text,
                client_msg_id=str(client_msg_id) if client_msg_id else None,
                user_id=adapter.user_id(),
                nickname=adapter.nickname(),
                kind=kind,
                media_id=media_id,
            )
            await websocket.send_json({"type": "ack", **result})
        except Exception as exc:  # noqa: BLE001
            await websocket.send_json({"type": "error", "message": str(exc)})
        return

    await websocket.send_json({"type": "error", "message": f"unknown frame type: {ftype}"})
