"""日记：把 platform=app 流「当天真实发生过的话」写成一篇第一人称日记。

设计取舍（2026-11-14）：
- 不复用 `real_diary` 插件——它的素材来自 context_archiver / cross_stream_relay
  等插件，而这些在本实例里一个都没装，启用也是空转。
- 素材直接用莫狐数据库里 app 流的当天消息，最真实。
- 落盘同时写 `.json`（App 读的权威数据）和 `.md`（给人看的）。
"""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import Any

from .storage import connect, mofox_db_path, project_root

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 素材条数 → 目标篇幅
_LEN_RULES = (
    (30, "材料很多，写 300~500 字"),
    (10, "材料中等，写 150~300 字"),
    (0, "材料很少，100 字以内就够，写得短不算失败"),
)

_SYSTEM_PROMPT = """你在替一个人写「她今天的日记」。你就是她，用第一人称，像夜里自己回想这一天。

硬性要求：
1. 第一人称，语气自然、口语化，像自己回想，不是汇报；
2. 只写材料里出现过的事：材料没提到的人、事、地点、数字，一个字都不许编，想不起来就跳过；
3. 时间拿不准就别写具体时刻（「下午」「后来」可以，「15:23」要有依据）；
4. 不要「今天共有 N 条」「根据记录」这类元话术，不要罗列条目，不要照抄原话；
5. 没有记录的时间段整段跳过，不许用「白天一直很安静」「没什么事发生」去填补。

让它像人写的：
6. 写具体的物件和身体：天气、手里拿着什么、坐在哪儿、身上什么感觉；不要写抽象的「开心」「温暖」；
7. 允许一句没用的话：自言自语、跑题的一瞬、半句没说完就断了都可以；
8. 结尾不要升华、不要道理、不要总结，停在一个画面或一句话上就结束。

直接输出日记正文，不要标题、不要 markdown 层级。"""


def diary_dir():
    d = project_root() / "data" / "neo_app_gateway" / "diary"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stream_id(user_id: str) -> str:
    from src.core.models.stream import ChatStream

    from ..protocol import PLATFORM

    return ChatStream.generate_stream_id(platform=PLATFORM, user_id=user_id)


def collect(day: dt.date, user_id: str) -> list[dict[str, Any]]:
    """取某一天（本地时区）里 app 流的全部消息，按时间升序。"""
    start = dt.datetime.combine(day, dt.time.min).timestamp()
    end = start + 86400
    sid = _stream_id(user_id)
    out: list[dict[str, Any]] = []
    try:
        con = connect(mofox_db_path(), readonly=True)
        try:
            rows = con.execute(
                "SELECT person_id, time, content, processed_plain_text FROM messages "
                "WHERE stream_id=? AND time>=? AND time<? ORDER BY time ASC",
                (sid, start, end),
            ).fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        text = r["processed_plain_text"] or r["content"] or ""
        if not isinstance(text, str):
            text = str(text)
        text = text.strip()
        if not text:
            continue
        out.append(
            {
                "at": float(r["time"] or 0),
                "who": "她" if (r["person_id"] or "") == "bot" else "他",
                "text": text[:600],
            }
        )
    return out


def _build_user_prompt(day: dt.date, her_name: str, items: list[dict[str, Any]]) -> str:
    n = len(items)
    length_hint = next(hint for threshold, hint in _LEN_RULES if n >= threshold)
    lines = [
        f"你是{her_name}。",
        f"日期：{day.strftime('%Y年%m月%d日')} {WEEKDAYS[day.weekday()]}",
        f"材料共 {n} 条（当天聊过的话，顺序按时间，时间可能不准）：",
    ]
    for item in items:
        clock = dt.datetime.fromtimestamp(item["at"]).strftime("%H:%M")
        lines.append(f"{clock} {item['who']}：{item['text']}")
    lines.append("")
    lines.append(f"篇幅：{length_hint}。")
    return "\n".join(lines)


async def _call_llm(system_prompt: str, user_prompt: str) -> tuple[bool, str, str]:
    """返回 (ok, text, error)。失败只记录、不抛。"""
    try:
        from src.app.plugin_system.api import llm_api
        from src.app.plugin_system.types import LLMPayload, ROLE, Text

        try:
            model_set = llm_api.get_model_set_by_task("summary")
        except Exception:  # noqa: BLE001
            model_set = None
        if not model_set:
            model_set = llm_api.get_model_set_by_task("actor")
        request = llm_api.create_llm_request(model_set, request_name="app_diary")
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))
        response = await request.send(stream=False)
        await response
        message = getattr(response, "message", None)
        if isinstance(message, str):
            text = message.strip()
        elif isinstance(message, list):
            text = "".join(getattr(i, "text", "") or (i if isinstance(i, str) else "") for i in message).strip()
        else:
            text = str(getattr(message, "text", "") or message or "").strip()
        if not text:
            return False, "", "empty response"
        return True, text, ""
    except Exception as exc:  # noqa: BLE001
        return False, "", str(exc)


def _entry_paths(day: dt.date) -> tuple[Any, Any]:
    stamp = day.strftime("%Y-%m-%d")
    return diary_dir() / f"{stamp}.json", diary_dir() / f"{stamp}.md"


def _manual_state_path():
    return diary_dir() / "_manual.json"


def _manual_used_date() -> str:
    try:
        data = json.loads(_manual_state_path().read_text(encoding="utf-8"))
        return str(data.get("date") or "")
    except Exception:  # noqa: BLE001
        return ""


def _mark_manual(day: dt.date) -> None:
    try:
        _manual_state_path().write_text(
            json.dumps({"date": day.strftime("%Y-%m-%d")}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def read_entry(day: dt.date) -> dict[str, Any] | None:
    json_path, _ = _entry_paths(day)
    if not json_path.exists():
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def list_entries() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in sorted(diary_dir().glob("*.json"), reverse=True):
        # `_manual.json` 这类内部状态文件不是日记，别让它混进列表
        if p.name.startswith("_"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        out.append(
            {
                "date": data.get("date", p.stem),
                "generated_at": data.get("generated_at", 0),
                "weekday": data.get("weekday", ""),
                "chars": len(data.get("body", "")),
            }
        )
    return out


async def generate(
    day: dt.date,
    user_id: str,
    her_name: str,
    *,
    force: bool = False,
    manual: bool = False,
) -> dict[str, Any]:
    """写某一天的日记。

    ``manual=True`` 是 App 上手动「补写今天」，**一天只允许成功一次**（防连点
    反复烧模型）；定时任务走 ``manual=False``，不受这个限制。
    """
    if manual and not force and _manual_used_date() == day.strftime("%Y-%m-%d"):
        return {"ok": False, "error": "daily_limit"}

    existing = read_entry(day)
    if existing and not force:
        return {"ok": True, "skipped": True, "entry": existing}

    items = collect(day, user_id)
    if not items:
        return {"ok": False, "error": "no_material", "count": 0}

    user_prompt = _build_user_prompt(day, her_name, items)
    ok, text, error = await _call_llm(_SYSTEM_PROMPT, user_prompt)
    if not ok:
        return {"ok": False, "error": error or "llm_failed", "count": len(items)}

    body = text.strip()
    if len(body) > 4000:
        body = body[:4000]

    stamp = day.strftime("%Y-%m-%d")
    entry = {
        "date": stamp,
        "weekday": WEEKDAYS[day.weekday()],
        "generated_at": time.time(),
        "count": len(items),
        "body": body,
        "name": her_name,
    }
    json_path, md_path = _entry_paths(day)
    json_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        f"# {stamp} {WEEKDAYS[day.weekday()]}",
        "",
        body,
        "",
        "",
        f"—— {her_name}",
        "",
    ]
    md_path.write_text("\n".join(md), encoding="utf-8")
    if manual:
        _mark_manual(day)
    return {"ok": True, "skipped": False, "entry": entry}


async def ensure_yesterday(user_id: str, her_name: str) -> dict[str, Any]:
    """启动时补昨天的日记（写过就跳过）。"""
    yesterday = dt.date.today() - dt.timedelta(days=1)
    return await generate(yesterday, user_id, her_name)
