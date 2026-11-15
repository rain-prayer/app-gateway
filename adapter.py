"""App 适配器。

它只做两件事：
- 入站：App 的原始消息 → MessageEnvelope（交给 CoreSink）
- 出站：核心的 MessageEnvelope → App 的 WS 帧

它**不**负责：路由、记忆、人设、聊天流程。那些全在核心和 KFC 里。
"""

from __future__ import annotations

import time
from typing import Any

import aiohttp
from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseAdapter, PlatformSendResult

from .protocol import PLATFORM, build_envelope, flatten_envelope
from .service.hub import get_hub

logger = get_logger("neo_app_gateway")


class AppAdapter(BaseAdapter):
    """platform="app" 的适配器。

    注意 platform 是出站路由的唯一键：
    核心按 message.platform 找到唯一适配器，所以 App 的回复不会跑到 QQ 上。
    """

    name = "app_adapter"
    description = "自建聊天 App 适配器（platform=app）"
    platform = PLATFORM
    adapter_version = "0.1.0"

    def __init__(self, core_sink: Any, plugin: Any = None, **kwargs: Any) -> None:
        # 不传 transport：WS 服务由插件自己的 uvicorn 起，不用 mofox_wire 那套
        super().__init__(core_sink, plugin=plugin, **kwargs)
        self._send_counter = 0

    # ---------- 配置 ----------

    def _cfg(self):
        return getattr(self.plugin, "config", None)

    def user_id(self) -> str:
        cfg = self._cfg()
        return getattr(getattr(cfg, "bridge", None), "user_id", "100000000")

    def nickname(self) -> str:
        cfg = self._cfg()
        return getattr(getattr(cfg, "bridge", None), "nickname", "淮")

    # ---------- 生命周期 ----------

    async def on_adapter_loaded(self) -> None:
        hub = get_hub()
        hub.bind_adapter(self)
        try:
            from .service.bridge import get_bridge

            get_bridge().inject_adapter(self)
        except Exception:  # noqa: BLE001
            pass
        logger.info(
            f"App 适配器已加载 platform={self.platform} "
            f"user={self.user_id()} nickname={self.nickname()}"
        )
        self._log_isolation()

    def _log_isolation(self) -> None:
        """启动时自查：有没有别的适配器也在抢 platform="app"。"""
        try:
            from src.app.plugin_system.api import adapter_api

            all_adapters = adapter_api.get_all_adapters()
            same_platform = [
                sig
                for sig, item in all_adapters.items()
                if getattr(item, "platform", None) == self.platform
            ]
            if len(same_platform) > 1:
                logger.error(f"发现多个 platform={self.platform} 的适配器，出站会重复: {same_platform}")
            else:
                logger.info(f"出站路由自查通过：platform={self.platform} 只有 1 个适配器")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"适配器自查跳过: {exc}")

    async def health_check(self) -> bool:
        """永远健康。

        App 是短连接客户端，没有「长连接是否存活」这回事。
        若返回 False，框架的健康检查循环会反复 stop/start 适配器，反而制造噪音。
        """
        return True

    # ---------- 入站 ----------

    async def from_platform_message(self, raw: Any) -> Any:
        """App 原始 dict → MessageEnvelope。"""
        if not isinstance(raw, dict):
            logger.warning(f"App 消息格式异常，已忽略: {raw!r}")
            return None
        return build_envelope(raw, user_id=self.user_id(), nickname=self.nickname())

    # ---------- 出站 ----------

    async def _send_platform_message(self, envelope: Any) -> PlatformSendResult:
        """核心要把消息发到 App 时的唯一出口。

        无论 App 在不在线都返回 success=True：
        - 在线：直接推；
        - 离线：进 hub 的 pending 队列，等 App 上线补推。
        返回失败会让 KFC 认为「这句没发出去」，导致它的 context_snapshot 与真实状态分叉。
        """
        self._send_counter += 1
        message_id = f"app-{int(time.time() * 1000)}-{self._send_counter}"
        text = flatten_envelope(envelope)

        try:
            await get_hub().push(
                {
                    "type": "msg",
                    "role": "bot",
                    "id": message_id,
                    "text": text,
                    "ts": time.time(),
                    **self._save_outbound_media(envelope),
                }
            )
            # App 不在线时顺手发一条通知（只发"她找你"，正文不进第三方）
            if not get_hub().is_online():
                await self._push_notification()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"出站推送异常（仍视为发送成功，消息已入队）: {exc}")

        return PlatformSendResult(success=True, message_id=message_id)

    @staticmethod
    def _save_outbound_media(envelope: Any) -> dict[str, str]:
        """她发来的图片/表情/语音：落盘到 media 目录，回传 URL 给 App。

        解析失败就返回空 dict（App 会按文字/占位渲染），绝不让发送链崩。
        """
        try:
            from .protocol import extract_media
            from .server.storage import media_dir

            items = extract_media(envelope)
            if not items:
                return {}
            import base64
            import uuid

            first = items[0]
            ext = {"voice": ".wav"}.get(first["kind"], ".png")
            fid = uuid.uuid4().hex + ext
            (media_dir() / fid).write_bytes(base64.b64decode(first["b64"]))
            return {"kind": first["kind"], "media_url": f"/media/{fid}"}
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"出站媒体落盘失败（按文字渲染）: {exc}")
            return {}

    async def _push_notification(self) -> None:
        """ntfy 通知：失败只记日志，绝不影响聊天。"""
        try:
            cfg = getattr(getattr(self.plugin, "config", None), "push", None)
            if cfg is None or not getattr(cfg, "ntfy_enabled", False):
                return
            topic = (getattr(cfg, "ntfy_topic", "") or "").strip()
            if not topic:
                return
            base = (getattr(cfg, "ntfy_url", "") or "https://ntfy.sh").rstrip("/")
            her = "她"
            try:
                from .server import qzone as space

                her = space.her_name() or her
            except Exception:  # noqa: BLE001
                pass
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
                await sess.post(
                    f"{base}/{topic}",
                    data=f"{her} 找你".encode("utf-8"),
                    headers={"Title": "moapp", "Tags": "speech_balloon"},
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"ntfy 推送失败（忽略）: {exc}")

    # ---------- 元信息 ----------

    async def get_bot_info(self) -> dict[str, Any]:
        return {
            "bot_id": "small_whale",
            "bot_name": self.nickname(),
            "platform": self.platform,
        }
