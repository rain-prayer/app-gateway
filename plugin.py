"""插件入口：注册适配器，并在插件加载时起一个独立的 HTTP/WS 服务。

Token 解析优先级：
1. config.toml 里 ``[server].token``（非空且不是 change-me）；
2. ``data/neo_app_gateway/token.txt``（首次自动生成并持久化，重启不变）。
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from pathlib import Path

import uvicorn

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BasePlugin
from src.core.components.loader import register_plugin
from src.kernel.concurrency import get_task_manager

from .adapter import AppAdapter
from .config import AppGatewayConfig
from .server.app import build_app
from .service.bridge import get_bridge
from .tools import QzoneCommentTool, QzoneLikeTool, QzonePostTool, QzoneReadTool

logger = get_logger("neo_app_gateway")


class _NoSignalServer(uvicorn.Server):
    """不接管进程信号的 uvicorn。

    插件跑在莫狐自己的事件循环里。如果让 uvicorn 装 SIGINT/SIGTERM 处理器，
    Ctrl+C 会变成「只关网关」，莫狐自己就收不到退出信号了。
    """

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


def _project_root() -> Path:
    """定位莫狐根目录（含 config/ 或 data/ 的那一层）。"""
    cwd = Path.cwd()
    if (cwd / "config").is_dir() or (cwd / "data").is_dir():
        return cwd
    return Path(__file__).resolve().parents[2]


def _resolve_token(cfg: AppGatewayConfig) -> str:
    token = (cfg.server.token or "").strip()
    if token and token != "change-me":
        return token

    path = _project_root() / "data" / "neo_app_gateway" / "token.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved

    new_token = secrets.token_urlsafe(24)
    path.write_text(new_token, encoding="utf-8")
    logger.warning(f"未配置 server.token，已自动生成并保存到 {path}")
    return new_token


@register_plugin
class AppGatewayPlugin(BasePlugin):
    """自建 App 网关插件。"""

    plugin_name: str = "neo_app_gateway"
    configs: list[type] = [AppGatewayConfig]
    dependent_components: list[str] = []

    _uvicorn: uvicorn.Server | None = None
    _serve_task = None
    _space_task = None
    _diary_task = None

    def get_components(self) -> list[type]:
        return [
            AppAdapter,
            QzoneReadTool,
            QzonePostTool,
            QzoneLikeTool,
            QzoneCommentTool,
        ]

    async def on_plugin_loaded(self) -> None:
        cfg = self.config
        if not isinstance(cfg, AppGatewayConfig):
            logger.error("配置未加载，网关不启动")
            return
        if not cfg.plugin.enabled:
            logger.info("网关已在配置中禁用")
            return

        token = _resolve_token(cfg)

        # 她在空间里显示的名字（配置可改）
        try:
            from .server import qzone as _space

            _space.set_her_name(cfg.bridge.her_name)
            # App 设置里改过的名字优先级更高（用户在手机上输的那个）
            _prof = _space.get_profile()
            if _prof.get("her_name"):
                _space.set_her_name(_prof["her_name"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"设置空间显示名失败: {exc}")

        app = build_app(token=token, allow_origins=list(cfg.server.allow_origins or ["*"]))
        server = _NoSignalServer(
            uvicorn.Config(
                app,
                host=cfg.server.host,
                port=cfg.server.port,
                log_level="warning",
                # 手机切后台/息屏时，浏览器会暂停响应 ping。
                # 默认 20s 超时会把她踢掉，表现为「经常重连」；放宽到 10 分钟。
                ws_ping_interval=30,
                ws_ping_timeout=600,
            )
        )
        self._uvicorn = server

        try:
            self._serve_task = get_task_manager().create_task(
                server.serve(), name="neo_app_gateway_http", daemon=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"启动网关失败: {exc}")
            self._uvicorn = None
            return

        for _ in range(50):
            if server.started:
                break
            await asyncio.sleep(0.1)

        if server.started:
            logger.info(f"网关已启动: http://{cfg.server.host}:{cfg.server.port} （WebSocket: /ws）")
            logger.info(f"访问令牌: {_project_root() / 'data' / 'neo_app_gateway' / 'token.txt'}")
        else:
            logger.error(
                f"网关启动失败：端口 {cfg.server.port} 可能被占用，或 {cfg.server.host} 不可绑"
            )
            await self._shutdown()
            return

        # 空间巡检：有新事件才叫她，没有就什么都不做
        try:
            self._space_task = get_task_manager().create_task(
                self._space_loop(), name="neo_app_gateway_space", daemon=True
            )
            logger.info("空间巡检已启动（有新动态/点赞/评论才会打扰她）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"启动空间巡检失败: {exc}")

        # 日记：启动时补昨天，之后每天 02:00 检查
        try:
            self._diary_task = get_task_manager().create_task(
                self._diary_loop(), name="neo_app_gateway_diary", daemon=True
            )
            logger.info("日记任务已启动（每天 02:00 写昨天的）")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"启动日记任务失败: {exc}")

    async def _diary_loop(self) -> None:
        import datetime as dt

        from .server import diary as diary_mod
        from .server import qzone as space

        await asyncio.sleep(20)
        while True:
            try:
                bridge = get_bridge()
                adapter = bridge._get_adapter()  # noqa: SLF001
                if adapter is not None:
                    result = await diary_mod.ensure_yesterday(
                        adapter.user_id(), space.her_name()
                    )
                    if result.get("ok") and not result.get("skipped"):
                        logger.info(f"日记已补写：{result.get('entry', {}).get('date')}")
                    elif not result.get("ok"):
                        logger.info(f"日记这次没写成：{result.get('error')}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"日记任务异常: {exc}")

            # 睡到下一个 02:00
            now = dt.datetime.now()
            target = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if target <= now:
                target += dt.timedelta(days=1)
            await asyncio.sleep(max(60.0, (target - now).total_seconds()))

    async def _space_loop(self) -> None:
        from .server import qzone as space

        interval = 600  # 10 分钟一次
        await asyncio.sleep(90)
        while True:
            try:
                events = space.fetch_unseen_events(8)
                if events:
                    prompt = self._format_events(events)
                    if await self._wake_her(prompt):
                        space.mark_events_seen([e["id"] for e in events])
                        logger.info(f"空间有新事件，已叫她（{len(events)} 条）")
                    else:
                        logger.info("空间有新事件，但她当前不在 Wait（下轮再试）")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"空间巡检异常: {exc}")
            await asyncio.sleep(interval)

    @staticmethod
    def _format_events(events: list[dict]) -> str:
        lines = ["空间里有新动静："]
        for e in events:
            kind = e.get("kind")
            actor = e.get("actor") or "有人"
            text = e.get("text") or ""
            if kind == "post":
                lines.append(f"- {actor} 发了新动态：{text}")
            elif kind == "like":
                lines.append(f"- {actor} 给你的动态点了赞")
            elif kind == "comment":
                lines.append(f"- {actor} 评论了：{text}")
        lines.append("想回应就去看看（qzone_read 看详情，qzone_like / qzone_comment 互动）。")
        return "\n".join(lines)

    async def _wake_her(self, prompt: str) -> bool:
        try:
            from src.core.managers.chatter_manager import get_chatter_manager
            from src.core.transport.distribution.stream_loop_manager import (
                get_stream_loop_manager,
            )

            from .service.bridge import get_bridge

            bridge = get_bridge()
            adapter = bridge._get_adapter()  # noqa: SLF001
            if adapter is None:
                return False
            sid = bridge.stream_id(adapter.user_id())
            await get_stream_loop_manager().start_stream_loop(sid)
            return await get_chatter_manager().resume_chatter(
                sid, source="space", extra={"resume_prompt": prompt}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"唤醒她失败: {exc}")
            return False

    async def on_plugin_unloaded(self) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        server = self._uvicorn
        if server is None:
            return
        self._uvicorn = None
        server.should_exit = True
        await asyncio.sleep(0.5)
        logger.info("网关已停止")
