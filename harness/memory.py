"""记忆与任务状态。

两类"记得住"要分开存，这是赛题最容易做错的地方：
- memory   = 用户明确提出的长期偏好（"以后都按办公费记"），跨会话保留，可查看/更正/删除；
- task_state = 任务进度与未完成事项，跟着任务走，是上下文里"永不裁剪"的锚点。

两条硬规矩：
1. **只记用户明确说过的**。模型不得自行推断偏好后写入；写入前必须在对话里回显一句。
2. 查询永远带 company_id + user_id。材料要求"不混用不同用户、企业的记忆"。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from uuid import uuid4

from . import audit

PREF = "user_pref"
FACT = "company_fact"


# ---------------------------------------------------------------- 长期记忆


def add(
    conn: sqlite3.Connection,
    *,
    company_id: str,
    user_id: str,
    content: str,
    mem_type: str = PREF,
    source_ref: str | None = None,
) -> str:
    memory_id = f"M-{uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO memory (memory_id, company_id, user_id, mem_type, content, source_ref)"
        " VALUES (?,?,?,?,?,?)",
        (memory_id, company_id, user_id, mem_type, content.strip(), source_ref),
    )
    audit.log(
        conn,
        audit.MEMORY_WRITE,
        company_id=company_id,
        payload={"memory_id": memory_id, "content": content.strip(), "scope": f"{company_id}/{user_id}"},
    )
    return memory_id


def list_active(
    conn: sqlite3.Connection, *, company_id: str, user_id: str, limit: int = 20
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM memory WHERE company_id=? AND user_id=? AND status='active'"
            " ORDER BY updated_at DESC LIMIT ?",
            (company_id, user_id, limit),
        ).fetchall()
    )


def get(conn: sqlite3.Connection, memory_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM memory WHERE memory_id=?", (memory_id,)).fetchone()


def correct(conn: sqlite3.Connection, memory_id: str, content: str) -> bool:
    """更正内容。保留原记录（审计能回看改了什么），只改内容与时间。"""
    row = get(conn, memory_id)
    if row is None:
        return False
    old = row["content"]
    conn.execute(
        "UPDATE memory SET content=?, updated_at=datetime('now') WHERE memory_id=?",
        (content.strip(), memory_id),
    )
    audit.log(
        conn,
        audit.MEMORY_CORRECTED,
        company_id=row["company_id"],
        payload={"memory_id": memory_id, "old": old, "new": content.strip()},
    )
    return True


def remove(conn: sqlite3.Connection, memory_id: str) -> bool:
    """删除 = 置为 deleted（软删），因为审计要能说明"它曾经存在过"。"""
    row = get(conn, memory_id)
    if row is None:
        return False
    conn.execute(
        "UPDATE memory SET status='deleted', updated_at=datetime('now') WHERE memory_id=?",
        (memory_id,),
    )
    audit.log(
        conn,
        audit.MEMORY_DELETED,
        company_id=row["company_id"],
        payload={"memory_id": memory_id, "content": row["content"]},
    )
    return True


def render(memories: list[sqlite3.Row]) -> str:
    if not memories:
        return "（无长期偏好记录）"
    return "\n".join(f"- [{m['memory_id']}] {m['content']}" for m in memories)


# ---------------------------------------------------------------- 任务状态


def get_open_task(conn: sqlite3.Connection, *, company_id: str, user_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM task_state WHERE company_id=? AND user_id=? AND status='open'",
        (company_id, user_id),
    ).fetchone()


def ensure_task(
    conn: sqlite3.Connection, *, company_id: str, user_id: str, task_id: str | None = None
) -> sqlite3.Row:
    """取现有未完成任务，没有就建一个。

    材料规定：初赛每个用户每家企业最多保留一个未完成任务（数据库有部分唯一索引兜底）。
    """
    row = get_open_task(conn, company_id=company_id, user_id=user_id)
    if row:
        return row
    tid = task_id or f"T-{uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO task_state (task_id, company_id, user_id, status, stage)"
        " VALUES (?,?,?,'open','new')",
        (tid, company_id, user_id),
    )
    audit.log(conn, audit.STATE_CHANGED, task_id=tid, company_id=company_id,
              payload={"action": "task_opened"})
    return conn.execute("SELECT * FROM task_state WHERE task_id=?", (tid,)).fetchone()


def set_pending(conn: sqlite3.Connection, task_id: str, pending: list[str]) -> None:
    """写未完成事项。这些内容在上下文里永不裁剪（见 context.py）。"""
    conn.execute(
        "UPDATE task_state SET pending_items=?, updated_at=? WHERE task_id=?",
        (json.dumps(pending, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), task_id),
    )
    audit.log(conn, audit.STATE_CHANGED, task_id=task_id, payload={"pending": pending})


def pending_of(row: sqlite3.Row) -> list[str]:
    try:
        return json.loads(row["pending_items"] or "[]")
    except json.JSONDecodeError:
        return []
