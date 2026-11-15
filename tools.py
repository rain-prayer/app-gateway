"""给她的空间工具：看 / 发 / 赞 / 评。"""

from __future__ import annotations

from typing import Annotated

from src.core.components.base import BaseTool

from .server import qzone as space


class QzoneReadTool(BaseTool):
    """看空间里最近有什么。"""

    name = "qzone_read"
    description = (
        "看看空间里最近有什么动态：谁发的、内容、谁点了赞、有哪些评论。"
        "想在空间做点什么之前，先用它了解情况。"
    )
    chatter_allow = []

    async def execute(
        self,
        limit: Annotated[int, "看几条动态，默认 3"] = 3,
    ) -> tuple[bool, str]:
        posts = space.read_posts(limit)
        if not posts:
            return True, "空间里还没有动态。"
        lines = []
        for p in posts:
            likes = "、".join(p["like_names"]) if p["like_names"] else "还没有人点赞"
            cmts = "；".join(
                f"{c['author']}{' 回复 ' + c['reply_to'] if c['reply_to'] else ''}：{c['text']}"
                for c in p["comments"]
            ) or "还没有评论"
            lines.append(
                f"[{p['id']}] {p['author']}：{p['text']}\n"
                f"  点赞：{likes}\n  评论：{cmts}"
            )
        return True, "\n".join(lines)


class QzonePostTool(BaseTool):
    """自己在空间发一条动态。"""

    name = "qzone_post"
    description = "在空间发一条动态，写你此刻想说的。想发的时候才发，不用勉强。"
    chatter_allow = []

    async def execute(
        self,
        text: Annotated[str, "动态内容"],
    ) -> tuple[bool, str]:
        text = (text or "").strip()
        if not text:
            return False, "内容不能为空"
        post = space.create_post_as(space.her_name(), text)
        return True, f"已经发出去了（动态 ID {post['id']}）。"


class QzoneLikeTool(BaseTool):
    """给某条动态点赞/取消。"""

    name = "qzone_like"
    description = "给一条动态点赞；如果已经赞过，再点一次就是取消。"
    chatter_allow = []

    async def execute(
        self,
        post_id: Annotated[str, "动态 ID，可从 qzone_read 的结果里看到"],
    ) -> tuple[bool, str]:
        result = space.toggle_like_as("her", space.her_name(), post_id)
        return True, "已经点赞了。" if result["liked"] else "取消了点赞。"


class QzoneCommentTool(BaseTool):
    """评论或回复。"""

    name = "qzone_comment"
    description = "评论一条动态；如果是在回复某条评论，把那条评论的 ID 传给 parent_id。"
    chatter_allow = []

    async def execute(
        self,
        post_id: Annotated[str, "动态 ID"],
        text: Annotated[str, "评论内容"],
        parent_id: Annotated[str, "要回复的评论 ID，没有就留空"] = "",
    ) -> tuple[bool, str]:
        text = (text or "").strip()
        if not text:
            return False, "内容不能为空"
        space.add_comment_as(space.her_name(), post_id, text, (parent_id or "").strip())
        return True, "评论发出去了。"
