"""数据访问层：连接、权限查询、事务。

设计要点：
1. 每次操作独立连接，避免 Streamlit / 多线程下 "database is locked"（WAL + busy_timeout 双保险）。
2. 权限一律从 acl 表查，绝不从对话里取 —— 材料明确"不能通过自然语言更改身份或权限"。
3. 缺库时自动跑 scripts/import_seed.py，评委 clone 后无需手动初始化。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

PERM_MODIFY = "modify"
PERM_VIEW = "view"


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_file = Path(path or config.db_path())
    ensure_seed(db_file)
    # LangGraph 在线程池里执行节点、写检查点，连接必须允许跨线程复用；
    # 并发写由 WAL + busy_timeout 兜底。
    conn = sqlite3.connect(db_file, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def ensure_seed(db_file: Path | None = None) -> None:
    """库不存在时按官方 Excel 重建。"""
    db_file = Path(db_file or config.db_path())
    if db_file.exists():
        return
    script = config.ROOT / "scripts" / "import_seed.py"
    if not script.exists():
        raise RuntimeError(f"数据库不存在且找不到导入脚本：{script}")
    db_file.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(script)], check=True)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务：要么全成，要么全不成。写操作必须走这里。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def permission(conn: sqlite3.Connection, user_id: str, company_id: str) -> str | None:
    """返回 'modify' / 'view' / None(无权访问)。"""
    row = conn.execute(
        "SELECT perm FROM acl WHERE user_id=? AND company_id=?", (user_id, company_id)
    ).fetchone()
    return row["perm"] if row else None


def company_name(conn: sqlite3.Connection, company_id: str) -> str:
    row = conn.execute(
        "SELECT short_name FROM company WHERE company_id=?", (company_id,)
    ).fetchone()
    return row["short_name"] if row else company_id


def list_companies(conn: sqlite3.Connection, user_id: str | None = None) -> list[sqlite3.Row]:
    if user_id is None:
        return conn.execute("SELECT * FROM company ORDER BY company_id").fetchall()
    return conn.execute(
        "SELECT c.*, a.perm FROM company c JOIN acl a ON a.company_id=c.company_id"
        " WHERE a.user_id=? ORDER BY c.company_id",
        (user_id,),
    ).fetchall()


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM app_user ORDER BY user_id").fetchall()
