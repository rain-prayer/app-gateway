"""空间：动态 / 点赞 / 评论（SQLite，独立于莫狐主库）。

点赞规则：
- `post_likes(post_id, user_id)` 唯一索引 → 点一下 insert、再点一下 delete。
- **计数永远用 COUNT 查**，不做 +1/-1，从根上杜绝"越点越多"。

事件表 `space_events` 用于巡检：你（App 用户）发帖/点赞/评论时记一条，
后台轮询到未读事件才去叫她，没事件就不打扰。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .auth import require_bearer
from .storage import app_db_path, connect, init_app_db

router = APIRouter(prefix="/api/v1/qzone", tags=["qzone"])

# App 用户（你）在空间里的稳定 ID；显示名固定为「我」
ME_ID = "me"
ME_NAME = "我"

# 她在空间里的显示名，插件加载时会用配置里的 her_name 覆盖
_HER_NAME = "小鲸鱼"


def set_her_name(name: str) -> None:
    global _HER_NAME
    old = _HER_NAME
    if name and name.strip():
        _HER_NAME = name.strip()
    # 把历史数据里占位的「她」和旧名字都改成新名字，避免新旧混着显示
    try:
        con = connect(app_db_path())
        con.execute(
            "UPDATE posts SET author=? WHERE author IN ('她', ?)", (_HER_NAME, old)
        )
        con.execute(
            "UPDATE comments SET author=? WHERE author IN ('她', ?)", (_HER_NAME, old)
        )
        con.commit()
        con.close()
    except Exception:  # noqa: BLE001
        pass


def her_name() -> str:
    return _HER_NAME


# ---------------- 显示名档案（App 设置里改的名字同步到后端） ----------------

def _ensure_profile(con: Any) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS profile (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def get_profile() -> dict[str, str]:
    _ensure_schema()
    con = connect(app_db_path())
    try:
        _ensure_profile(con)
        con.commit()
        rows = con.execute("SELECT key, value FROM profile").fetchall()
        data = {r["key"]: r["value"] for r in rows}
    finally:
        con.close()
    data.setdefault("her_name", _HER_NAME)
    return data


def set_profile(*, her_name: str | None = None, my_name: str | None = None) -> dict[str, str]:
    _ensure_schema()
    con = connect(app_db_path())
    try:
        _ensure_profile(con)
        if her_name and her_name.strip():
            con.execute(
                "INSERT INTO profile (key, value) VALUES ('her_name', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (her_name.strip(),),
            )
        if my_name and my_name.strip():
            con.execute(
                "INSERT INTO profile (key, value) VALUES ('my_name', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (my_name.strip(),),
            )
        con.commit()
    finally:
        con.close()
    if her_name and her_name.strip():
        set_her_name(her_name.strip())
    return get_profile()

_SCHEMA_READY = False


def _ensure_schema() -> None:
    """建表 + 补列，幂等。"""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    init_app_db()
    con = connect(app_db_path())
    try:
        cols = [r["name"] for r in con.execute("PRAGMA table_info(comments)")]
        if "parent_id" not in cols:
            con.execute("ALTER TABLE comments ADD COLUMN parent_id TEXT NOT NULL DEFAULT ''")
        if "reply_to" not in cols:
            con.execute("ALTER TABLE comments ADD COLUMN reply_to TEXT NOT NULL DEFAULT ''")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS post_likes (
                post_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                ts REAL NOT NULL,
                PRIMARY KEY (post_id, user_id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS space_events (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                post_id TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL DEFAULT '',
                ts REAL NOT NULL,
                seen INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        con.commit()
    finally:
        con.close()
    _SCHEMA_READY = True


def _log_event(kind: str, post_id: str, actor: str, text: str) -> None:
    con = connect(app_db_path())
    try:
        con.execute(
            "INSERT INTO space_events (id, kind, post_id, actor, text, ts, seen) "
            "VALUES (?,?,?,?,?,?,0)",
            (uuid.uuid4().hex, kind, post_id, actor, text[:200], time.time()),
        )
        con.commit()
    finally:
        con.close()


def _seed_once() -> None:
    _ensure_schema()
    con = connect(app_db_path())
    try:
        n = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        if n > 0:
            return
        now = time.time()
        rows = [
            (
                uuid.uuid4().hex,
                _HER_NAME,
                "今天稻子熟了，我收了二十四株，手上全是草的味道。",
                "[]",
                now - 7200,
                0,
                0,
                "life",
            ),
            (
                uuid.uuid4().hex,
                _HER_NAME,
                "湖面开始起风了，我蹲在边上看了很久。",
                "[]",
                now - 86400,
                0,
                0,
                "life",
            ),
        ]
        con.executemany(
            "INSERT INTO posts (id, author, text, images, ts, likes, comments, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


def _likes_of(con: Any, post_id: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT user_id, name, ts FROM post_likes WHERE post_id=? ORDER BY ts ASC", (post_id,)
    ).fetchall()
    return [{"user_id": r["user_id"], "name": r["name"], "ts": r["ts"]} for r in rows]


def _comments_of(con: Any, post_id: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT id, author, text, ts, parent_id, reply_to FROM comments "
        "WHERE post_id=? ORDER BY ts ASC",
        (post_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "author": r["author"],
            "text": r["text"],
            "ts": r["ts"],
            "parent_id": r["parent_id"] or "",
            "reply_to": r["reply_to"] or "",
        }
        for r in rows
    ]


def _post_dict(con: Any, row: Any) -> dict[str, Any]:
    try:
        images = json.loads(row["images"] or "[]")
    except Exception:  # noqa: BLE001
        images = []
    likes = _likes_of(con, row["id"])
    comments = _comments_of(con, row["id"])
    return {
        "id": row["id"],
        "author": row["author"],
        "text": row["text"],
        "images": images,
        "ts": row["ts"],
        "likes": len(likes),
        "liked": any(item["user_id"] == ME_ID for item in likes),
        "like_names": [item["name"] for item in likes],
        "comments": comments,
        "comments_count": len(comments),
    }


# ---------------- 给工具用的内部接口（不经过 HTTP） ----------------

def read_posts(limit: int = 5) -> list[dict[str, Any]]:
    _seed_once()
    con = connect(app_db_path())
    try:
        rows = con.execute(
            "SELECT * FROM posts ORDER BY ts DESC LIMIT ?", (max(1, min(limit, 50)),)
        ).fetchall()
        return [_post_dict(con, r) for r in rows]
    finally:
        con.close()


def create_post_as(author: str, text: str, images: list[str] | None = None) -> dict[str, Any]:
    _seed_once()
    pid = uuid.uuid4().hex
    con = connect(app_db_path())
    try:
        con.execute(
            "INSERT INTO posts (id, author, text, images, ts, likes, comments, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                pid,
                author,
                text.strip(),
                json.dumps(images or [], ensure_ascii=False),
                time.time(),
                0,
                0,
                "auto",
            ),
        )
        con.commit()
        row = con.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
        return _post_dict(con, row)
    finally:
        con.close()


def toggle_like_as(user_id: str, name: str, post_id: str) -> dict[str, Any]:
    _seed_once()
    con = connect(app_db_path())
    try:
        exists = con.execute(
            "SELECT 1 FROM post_likes WHERE post_id=? AND user_id=?", (post_id, user_id)
        ).fetchone()
        if exists:
            con.execute(
                "DELETE FROM post_likes WHERE post_id=? AND user_id=?", (post_id, user_id)
            )
            liked = False
        else:
            con.execute(
                "INSERT INTO post_likes (post_id, user_id, name, ts) VALUES (?,?,?,?)",
                (post_id, user_id, name, time.time()),
            )
            liked = True
        con.commit()
        likes = _likes_of(con, post_id)
    finally:
        con.close()
    return {"liked": liked, "count": len(likes), "names": [i["name"] for i in likes]}


def add_comment_as(
    author: str, post_id: str, text: str, parent_id: str = ""
) -> list[dict[str, Any]]:
    _seed_once()
    reply_to = ""
    con = connect(app_db_path())
    try:
        if parent_id:
            parent = con.execute(
                "SELECT author FROM comments WHERE id=?", (parent_id,)
            ).fetchone()
            if parent is not None:
                reply_to = parent["author"]
            else:
                parent_id = ""
        con.execute(
            "INSERT INTO comments (id, post_id, author, text, ts, parent_id, reply_to) "
            "VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, post_id, author, text.strip(), time.time(), parent_id, reply_to),
        )
        con.commit()
        return _comments_of(con, post_id)
    finally:
        con.close()


def fetch_unseen_events(limit: int = 10) -> list[dict[str, Any]]:
    _ensure_schema()
    con = connect(app_db_path())
    try:
        rows = con.execute(
            "SELECT id, kind, post_id, actor, text, ts FROM space_events "
            "WHERE seen=0 ORDER BY ts ASC LIMIT ?",
            (max(1, min(limit, 50)),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def mark_events_seen(ids: list[str]) -> None:
    if not ids:
        return
    con = connect(app_db_path())
    try:
        con.executemany("UPDATE space_events SET seen=1 WHERE id=?", [(i,) for i in ids])
        con.commit()
    finally:
        con.close()


# ---------------- HTTP 路由 ----------------

@router.get("/posts")
async def list_posts(limit: int = 20) -> dict[str, Any]:
    _seed_once()
    con = connect(app_db_path())
    try:
        rows = con.execute(
            "SELECT * FROM posts ORDER BY ts DESC LIMIT ?", (max(1, min(limit, 100)),)
        ).fetchall()
        posts = [_post_dict(con, r) for r in rows]
    finally:
        con.close()
    return {"ok": True, "posts": posts}


class PostBody(BaseModel):
    text: str
    images: list[str] = []


@router.post("/posts", dependencies=[Depends(require_bearer)])
async def create_post(body: PostBody) -> dict[str, Any]:
    text = body.text.strip()
    if not text and not body.images:
        return {"ok": False, "error": "empty"}
    post = create_post_as(ME_NAME, text, body.images)
    _log_event("post", post["id"], ME_NAME, text)
    return {"ok": True, "post": post}


@router.post("/posts/{post_id}/like", dependencies=[Depends(require_bearer)])
async def toggle_like(post_id: str) -> dict[str, Any]:
    """点赞 / 取消点赞。同一个人点第二次就是取消。"""
    result = toggle_like_as(ME_ID, ME_NAME, post_id)
    if result["liked"]:
        _log_event("like", post_id, ME_NAME, "")
    return {"ok": True, **result}


class CommentBody(BaseModel):
    text: str
    parent_id: str | None = None


@router.post("/posts/{post_id}/comments", dependencies=[Depends(require_bearer)])
async def add_comment(post_id: str, body: CommentBody) -> dict[str, Any]:
    text = body.text.strip()
    if not text:
        return {"ok": False, "error": "empty"}
    comments = add_comment_as(ME_NAME, post_id, text, (body.parent_id or "").strip())
    _log_event("comment", post_id, ME_NAME, text)
    return {"ok": True, "comments": comments}


@router.get("/posts/{post_id}/comments")
async def list_comments(post_id: str) -> dict[str, Any]:
    _seed_once()
    con = connect(app_db_path())
    try:
        comments = _comments_of(con, post_id)
    finally:
        con.close()
    return {"ok": True, "comments": comments}
