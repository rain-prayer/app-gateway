"""路径与 SQLite 小工具。"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def project_root() -> Path:
    """莫狐根目录（含 config/ 或 data/ 的那一层）。"""
    cwd = Path.cwd()
    if (cwd / "config").is_dir() or (cwd / "data").is_dir():
        return cwd
    return Path(__file__).resolve().parents[3]


def mofox_db_path() -> Path:
    return project_root() / "data" / "MoFox.db"


def app_db_path() -> Path:
    p = project_root() / "data" / "neo_app_gateway" / "app.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_app_db() -> None:
    con = connect(app_db_path())
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                author TEXT NOT NULL,
                text TEXT NOT NULL,
                images TEXT NOT NULL DEFAULT '[]',
                ts REAL NOT NULL,
                likes INTEGER NOT NULL DEFAULT 0,
                comments INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'manual'
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS comments (
                id TEXT PRIMARY KEY,
                post_id TEXT NOT NULL,
                author TEXT NOT NULL,
                text TEXT NOT NULL,
                ts REAL NOT NULL
            )
            """
        )
        con.commit()
    finally:
        con.close()

def media_dir():
    """媒体文件目录（图片/表情/语音），上传与静态服务共用。"""
    d = project_root() / "data" / "neo_app_gateway" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d
