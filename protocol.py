"""App 网关的协议层：常量、信封构造、信封拍平。

注意：信封里的 role 必须是 mofox_wire 的 UserRole 枚举，
核心的 envelope_to_message 会取 ``raw_role.value``，传字符串会炸。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from mofox_wire import MessageEnvelope
from mofox_wire.types import UserRole

# 平台标识：出站路由的唯一键，决定她的话只会回到 App，不会回到 QQ。
PLATFORM = "app"

PLUGIN_NAME = "neo_app_gateway"
ADAPTER_NAME = "app_adapter"

# 文本消息的最大长度，防止 App 端塞进来一条巨长的东西把上下文撑爆。
MAX_TEXT_LEN = 8000


def build_envelope(
    raw: dict[str, Any],
    *,
    user_id: str,
    nickname: str,
) -> MessageEnvelope:
    """把 App 端的一条原始消息转成核心认识的 MessageEnvelope。

    Args:
        raw: 至少包含 ``text``；可选 ``message_id`` / ``time``。
        user_id: App 用户的稳定 ID（沿用 QQ 号以保持记忆归属）。
        nickname: 她在提示词里看到的名字。

    Returns:
        MessageEnvelope: direction=incoming、platform=app、无 group_info 的私聊信封。
    """
    text = str(raw.get("text") or "").strip()
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN]

    message_id = str(raw.get("message_id") or raw.get("client_msg_id") or uuid.uuid4().hex)
    ts = raw.get("time")
    if not isinstance(ts, (int, float)):
        ts = time.time()

    # 媒体消息：图片/表情 → image 段；语音 → voice 段（核心有 ASR 时会转文字）
    kind = str(raw.get("kind") or "text")
    media_b64 = str(raw.get("media_b64") or "")
    media_mime = str(raw.get("media_mime") or "")

    segments: list[dict[str, Any]] = []
    if kind in ("image", "sticker") and media_b64:
        segments.append({"type": "image", "data": f"base64|{media_b64}"})
    elif kind == "voice" and media_b64:
        segments.append({"type": "voice", "data": f"base64|{media_b64}"})
    if text:
        segments.append({"type": "text", "data": text})
    if not segments:
        segments.append({"type": "text", "data": ""})

    return {
        "direction": "incoming",
        "message_info": {
            "platform": PLATFORM,
            "message_id": message_id,
            # 必须是数值时间戳：融合叙事只接受 int/float，datetime 会被静默丢弃
            "time": float(ts),
            # 走标准消息分支；不带 group_info 就是私聊，KFC 的 chat_type=PRIVATE 才能匹配上
            "message_type": "private",
            "user_info": {
                "platform": PLATFORM,
                # 枚举而不是字符串：核心读 .value
                "role": UserRole.OWNER,
                "user_id": user_id,
                "user_nickname": nickname,
            },
        },
        "message_segment": segments,
        "raw_message": raw,
    }


def flatten_envelope(envelope: MessageEnvelope) -> str:
    """把出站信封拍平成一段纯文本，供 App 直接显示。

    目前只处理文本；图片/语音/表情先用占位符，等阶段三做媒体时再补。
    """
    segments = envelope.get("message_segment") or envelope.get("message_chain") or []
    if isinstance(segments, dict):
        segments = [segments]

    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type") or "")
        data = seg.get("data")
        if seg_type == "text":
            parts.append(str(data))
        elif seg_type in ("image", "emoji"):
            parts.append("[图片]")
        elif seg_type == "voice":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
        elif isinstance(data, str):
            parts.append(data)
    return "".join(parts).strip()


def extract_media(envelope: MessageEnvelope) -> list[dict[str, str]]:
    """从出站信封里抠出媒体段（图片/表情/语音）。

    返回 ``[{"kind": "image"|"sticker"|"voice", "b64": "..."}]``。
    ``data`` 支持 ``base64|...`` 前缀、纯 base64、http(s) 地址、file:// 路径；
    解析不了的段直接跳过（宁可显示占位符，也不要让发送链崩掉）。
    """
    import base64
    import pathlib
    import urllib.request

    segments = envelope.get("message_segment") or envelope.get("message_chain") or []
    if isinstance(segments, dict):
        segments = [segments]

    out: list[dict[str, str]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type") or "")
        if seg_type not in ("image", "emoji", "voice"):
            continue
        kind = "voice" if seg_type == "voice" else ("sticker" if seg_type == "emoji" else "image")
        raw = seg.get("data")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            if raw.startswith("base64|"):
                payload = base64.b64decode(raw[7:])
            elif raw.startswith(("http://", "https://")):
                with urllib.request.urlopen(raw, timeout=10) as resp:  # noqa: S310
                    payload = resp.read()
            elif raw.startswith("file://"):
                payload = pathlib.Path(raw[7:]).read_bytes()
            elif pathlib.Path(raw).is_file():
                payload = pathlib.Path(raw).read_bytes()
            else:
                payload = base64.b64decode(raw, validate=False)
        except Exception:  # noqa: BLE001
            continue
        if payload:
            out.append({"kind": kind, "b64": base64.b64encode(payload).decode()})
    return out
