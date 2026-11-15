"""桥接层：App 消息 ↔ 框架聊天流。

这里是「不绕过 KFC」的关键：我们只把消息交给适配器，由框架自己的
CoreSink → MessageReceiver → distributor → ChatStream → tick 拉起 KFC。
绝不自己 new Chatter、也不自己调 execute()。
"""

from __future__ import annotations

import time
import uuid
from collections import deque

from src.app.plugin_system.api.log_api import get_logger

from ..protocol import ADAPTER_NAME, PLATFORM, PLUGIN_NAME

logger = get_logger("neo_app_gateway")


class AppBridge:
    """单用户桥接器。"""

    def __init__(self) -> None:
        self._adapter = None
        self._seen: deque[str] = deque(maxlen=1024)
        self._chatter_ready: set[str] = set()

    # ---------- 适配器 ----------

    def _get_adapter(self):
        if self._adapter is not None:
            return self._adapter
        try:
            from src.app.plugin_system.api import adapter_api

            self._adapter = adapter_api.get_adapter(f"{PLUGIN_NAME}:adapter:{ADAPTER_NAME}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"获取 App 适配器失败: {exc}")
        return self._adapter

    def inject_adapter(self, adapter) -> None:
        self._adapter = adapter

    # ---------- 流与 chatter ----------

    @staticmethod
    def stream_id(user_id: str) -> str:
        """与核心完全一致的 stream_id 算法：sha256("{platform}_{user_id}_private")。"""
        from src.core.models.stream import ChatStream

        return ChatStream.generate_stream_id(platform=PLATFORM, user_id=user_id)

    async def ensure_chatter(self, user_id: str) -> str:
        """确保该流已经绑定到兼容的 Chatter（正常情况下就是 KFC）。

        框架本来会自动绑定，这里显式绑定一次是为了：
        1. 启动时就能在日志里看到到底选中了谁；
        2. 万一没选中（比如 chat_type 判成群聊），能被发现而不是静默不理人。
        """
        sid = self.stream_id(user_id)
        if sid in self._chatter_ready:
            return sid
        try:
            from src.app.plugin_system.api import chat_api

            chatter = chat_api.get_or_create_chatter_for_stream(
                stream_id=sid,
                chat_type="private",
                platform=PLATFORM,
            )
            if chatter is None:
                logger.error(
                    f"未找到兼容 Chatter（stream={sid[:8]}）：消息会进流但没人回。"
                    "检查 KFC 的 chat_type 是否为 PRIVATE、信封里是否误带了 group_info"
                )
            else:
                cls_name = type(chatter).__name__
                logger.info(f"已绑定 Chatter: {cls_name} stream={sid[:8]}")
                low = cls_name.lower()
                if "kokoro" not in low and "kfc" not in low:
                    logger.warning(f"绑定的 Chatter 不是 KFC（{cls_name}），如果她人设不对请检查插件启停顺序")
                self._chatter_ready.add(sid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"显式绑定 Chatter 失败（框架仍会自动绑定）: {exc}")
        return sid

    # ---------- 入站 ----------

    async def push_incoming(
        self,
        *,
        text: str,
        client_msg_id: str | None,
        user_id: str,
        nickname: str,
        ts: float | None = None,
        kind: str = "text",
        media_id: str = "",
    ) -> dict:
        """把 App 的一条消息送进框架（文本 / 图片 / 表情 / 语音）。"""
        import base64

        text = (text or "").strip()
        if not text and not media_id:
            return {"accepted": False, "reason": "empty"}

        if client_msg_id and client_msg_id in self._seen:
            return {"accepted": False, "reason": "duplicate", "client_msg_id": client_msg_id}

        adapter = self._get_adapter()
        if adapter is None:
            raise RuntimeError("App 适配器还没就绪（插件未加载完成？）")

        media_b64 = ""
        media_mime = ""
        if media_id:
            try:
                from ..server.storage import media_dir

                path = media_dir() / media_id
                media_b64 = base64.b64encode(path.read_bytes()).decode()
                ext = path.suffix.lower()
                media_mime = {
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".png": "image/png",
                    ".gif": "image/gif",
                    ".webp": "image/webp",
                    ".webm": "audio/webm",
                    ".m4a": "audio/mp4",
                    ".mp3": "audio/mpeg",
                    ".wav": "audio/wav",
                    ".ogg": "audio/ogg",
                }.get(ext, "")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"读取媒体文件失败 {media_id}: {exc}")

        raw = {
            "message_id": client_msg_id or uuid.uuid4().hex,
            "client_msg_id": client_msg_id or "",
            "text": text,
            "time": ts if isinstance(ts, (int, float)) else time.time(),
            "kind": kind,
            "media_b64": media_b64,
            "media_mime": media_mime,
            "media_id": media_id,
        }

        # 先确保 chatter 绑定，再交给适配器（顺序不重要，但日志更好读）
        await self.ensure_chatter(user_id)

        # 唯一入口：内部会走 from_platform_message → core_sink.send
        await adapter.on_platform_message(raw)

        if client_msg_id:
            self._seen.append(client_msg_id)

        logger.info(
            f"App 入站 → stream={self.stream_id(user_id)[:8]} kind={kind} "
            f"len={len(text)}{' media' if media_b64 else ''}"
        )
        return {"accepted": True, "client_msg_id": client_msg_id or ""}


_bridge: AppBridge | None = None


def get_bridge() -> AppBridge:
    global _bridge
    if _bridge is None:
        _bridge = AppBridge()
    return _bridge
