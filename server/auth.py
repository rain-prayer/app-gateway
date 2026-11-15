"""Bearer Token 校验。"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

_token: str = ""


def set_token(token: str) -> None:
    global _token
    _token = (token or "").strip()


def verify_token(candidate: str) -> bool:
    if not _token or not candidate:
        return False
    return secrets.compare_digest(candidate.strip(), _token)


async def require_bearer(request: Request) -> None:
    """FastAPI 依赖：要求 ``Authorization: Bearer <token>``。"""
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    if not verify_token(header.split(" ", 1)[1]):
        raise HTTPException(status_code=403, detail="Token 无效")
