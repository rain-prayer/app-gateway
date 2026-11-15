"""HTTP 接口。

只放「一问一答」的东西；实时消息走 WebSocket（ws.py）。
"""

from __future__ import annotations

import base64
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..protocol import PLATFORM
from ..service.bridge import get_bridge
from ..service.hub import get_hub
from .auth import require_bearer
from .storage import connect, media_dir, mofox_db_path, project_root

router = APIRouter()

_started_at = time.time()


class SendBody(BaseModel):
    text: str = ""
    client_msg_id: str | None = None
    kind: str = "text"          # text / image / sticker / voice
    media_id: str = ""          # /api/v1/upload 返回的 id


_MEDIA_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
}


class UploadBody(BaseModel):
    data: str                   # base64，可带 data: 前缀
    kind: str = "image"
    mime: str = ""


@router.post("/api/v1/upload", dependencies=[Depends(require_bearer)])
async def upload(body: UploadBody) -> dict[str, Any]:
    """上传图片/表情/语音。消息体走独立接口，不塞进 WS 通道。"""
    raw = body.data or ""
    mime = body.mime or ""
    if raw.startswith("data:"):
        head, raw = raw.split(",", 1)
        mime = mime or head[5:].split(";")[0]
    try:
        payload = base64.b64decode(raw)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "bad_base64"}
    if len(payload) > 20 * 1024 * 1024:
        return {"ok": False, "error": "too_large"}
    ext = _MEDIA_EXT.get(mime, ".jpg" if body.kind != "voice" else ".webm")
    fid = uuid.uuid4().hex + ext
    (media_dir() / fid).write_bytes(payload)
    return {"ok": True, "id": fid, "url": f"/media/{fid}", "kind": body.kind, "size": len(payload)}


@router.get("/api/v1/health")
async def health() -> dict[str, Any]:
    """无鉴权探活，App 启动时用来确认服务在不在。"""
    hub = get_hub()
    return {
        "ok": True,
        "platform": PLATFORM,
        "uptime_s": round(time.time() - _started_at, 1),
        "app_online": hub.is_online(),
        "pending": hub.pending_count(),
        "ts": time.time(),
    }


@router.get("/api/v1/me", dependencies=[Depends(require_bearer)])
async def me() -> dict[str, Any]:
    """鉴权探针：能返回说明 token 是对的。"""
    bridge = get_bridge()
    adapter = bridge._get_adapter()  # noqa: SLF001
    user_id = adapter.user_id() if adapter is not None else "100000000"
    return {
        "ok": True,
        "platform": PLATFORM,
        "user_id": user_id,
        "stream_id": bridge.stream_id(user_id),
        "app_online": get_hub().is_online(),
    }


@router.get("/api/v1/chat/history", dependencies=[Depends(require_bearer)])
async def history(limit: int = 200, before: int | None = None) -> dict[str, Any]:
    """历史消息：把 App 流和老 QQ 流合并，按时间升序。

    ``before`` 传毫秒时间戳时，只返回更早的消息（用于"加载更早"）。
    """
    from src.core.models.stream import ChatStream

    bridge = get_bridge()
    adapter = bridge._get_adapter()  # noqa: SLF001
    user_id = adapter.user_id() if adapter is not None else "100000000"
    upstream = getattr(getattr(getattr(adapter, "plugin", None), "config", None), "bridge", None)
    upstream_platform = getattr(upstream, "upstream_platform", "qq") or "qq"

    app_stream = ChatStream.generate_stream_id(platform=PLATFORM, user_id=user_id)
    old_stream = ChatStream.generate_stream_id(platform=upstream_platform, user_id=user_id)
    n = max(1, min(int(limit), 500))

    rows: list[tuple] = []
    try:
        con = connect(mofox_db_path(), readonly=True)
        try:
            sql = (
                "SELECT message_id, person_id, time, content, processed_plain_text "
                "FROM messages WHERE stream_id IN (?, ?)"
            )
            params: list[Any] = [app_stream, old_stream]
            if before is not None:
                sql += " AND time < ?"
                params.append(float(before) / 1000.0)
            sql += " ORDER BY time DESC LIMIT ?"
            params.append(n)
            rows = con.execute(sql, params).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        rows = []

    out = []
    for r in reversed(rows):
        text = r["processed_plain_text"] or r["content"] or ""
        if not isinstance(text, str):
            text = str(text)
        if not text.strip():
            continue
        out.append(
            {
                "id": r["message_id"] or "",
                "role": "bot" if (r["person_id"] or "") == "bot" else "me",
                "text": text,
                "ts": int((r["time"] or 0) * 1000),
            }
        )
    return {"ok": True, "messages": out}


@router.get("/api/v1/diary/list", dependencies=[Depends(require_bearer)])
async def diary_list() -> dict[str, Any]:
    """日记目录（按日期倒序，App 自己按月份分组）。"""
    from . import diary as diary_mod

    return {"ok": True, "entries": diary_mod.list_entries()}


class DiaryGenerateBody(BaseModel):
    date: str | None = None
    force: bool = False


@router.post("/api/v1/diary/generate", dependencies=[Depends(require_bearer)])
async def diary_generate(body: DiaryGenerateBody | None = None) -> dict[str, Any]:
    """补写某一天的日记（默认今天；force=True 覆盖已写过的）。"""
    import datetime as dt

    from . import diary as diary_mod
    from . import qzone as space

    day = dt.date.today()
    if body is not None and body.date:
        try:
            day = dt.datetime.strptime(body.date, "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "bad_date"}
    bridge = get_bridge()
    adapter = bridge._get_adapter()  # noqa: SLF001
    user_id = adapter.user_id() if adapter is not None else "100000000"
    return await diary_mod.generate(
        day,
        user_id,
        space.her_name(),
        force=bool(body is not None and body.force),
        manual=True,
    )


@router.get("/api/v1/diary/{date}", dependencies=[Depends(require_bearer)])
async def diary_get(date: str) -> dict[str, Any]:
    """读某一天的日记正文。"""
    import datetime as dt

    from . import diary as diary_mod

    try:
        day = dt.datetime.strptime(date, "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "bad_date"}
    entry = diary_mod.read_entry(day)
    if entry is None:
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "entry": entry}


@router.get("/api/v1/profile", dependencies=[Depends(require_bearer)])
async def get_profile() -> dict[str, Any]:
    from . import qzone as space

    return {"ok": True, **space.get_profile()}


class ProfileBody(BaseModel):
    her_name: str | None = None
    my_name: str | None = None


@router.post("/api/v1/profile", dependencies=[Depends(require_bearer)])
async def set_profile(body: ProfileBody) -> dict[str, Any]:
    """App 保存设置时把名字同步过来，空间里立刻改用新名字。"""
    from . import qzone as space

    return {"ok": True, **space.set_profile(her_name=body.her_name, my_name=body.my_name)}


@router.post("/api/v1/chat/send", dependencies=[Depends(require_bearer)])
async def send(body: SendBody) -> dict[str, Any]:
    """HTTP 兜底发送（WebSocket 断线时可用 curl 测）。"""
    bridge = get_bridge()
    adapter = bridge._get_adapter()
    if adapter is None:
        return {"ok": False, "error": "adapter_not_ready"}
    result = await bridge.push_incoming(
        text=body.text,
        client_msg_id=body.client_msg_id,
        user_id=adapter.user_id(),
        nickname=adapter.nickname(),
        kind=body.kind,
        media_id=body.media_id,
    )
    return {"ok": True, **result}
