"""HTTP / WebSocket 服务端（插件自己起的独立 uvicorn）。

为什么不用 BaseRouter：
核心的 HTTP 服务绑在 127.0.0.1:8000，BaseRouter 只能挂到那上面，手机连不进来。
所以这里自己起一个 0.0.0.0:8100，只对本插件负责，核心 WebUI 仍保持只听本机。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import api, qzone, ws
from .auth import set_token
from .storage import project_root


def build_app(*, token: str, allow_origins: list[str] | None = None) -> FastAPI:
    set_token(token)
    app = FastAPI(
        title="Neo App Gateway",
        version="0.2.0",
        docs_url="/docs",
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api.router)
    app.include_router(qzone.router)
    app.include_router(ws.router)

    # 限流：整体 120 次/分钟，发消息 30 条/分钟（token 泄露时不至于被人刷爆）
    from fastapi.responses import JSONResponse

    from .ratelimit import RateLimiter

    general = RateLimiter(120, 60.0)
    sending = RateLimiter(30, 60.0)

    @app.middleware("http")
    async def _rate_limit(request, call_next):
        path = request.url.path
        if path.startswith(("/media", "/app", "/api/v1/health", "/ws", "/docs", "/openapi")):
            return await call_next(request)
        key = request.client.host if request.client else "unknown"
        if not general.allow(key):
            return JSONResponse(
                {"ok": False, "error": "rate_limited", "message": "请求太频繁，歇一下"},
                status_code=429,
            )
        if path.endswith("/chat/send") and not sending.allow(key):
            return JSONResponse(
                {"ok": False, "error": "too_fast", "message": "发得太快了，缓一缓"},
                status_code=429,
            )
        return await call_next(request)

    # 媒体文件（图片 / 表情 / 语音）静态服务，前端直接用 <服务器>/media/<id>
    media = project_root() / "data" / "neo_app_gateway" / "media"
    media.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media)), name="media")

    # 顺手把 Flutter Web 版挂上：手机打开 <隧道地址>/app/ 就能用，不用装 APK
    webapp = project_root() / "data" / "neo_app_gateway" / "webapp"
    if webapp.is_dir():
        app.mount("/app", StaticFiles(directory=str(webapp), html=True), name="webapp")

    return app
