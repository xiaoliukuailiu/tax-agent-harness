"""审计事件流：只追加、不修改。

对应官方基础要求 6（运行记录）：能查本轮模型输入、选用的记忆与规则、工具调用及结果。
注意：**绝不记录模型密钥**（材料明确要求）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

TOOL_CALLED = "TOOL_CALLED"
RULE_CITED = "RULE_CITED"
MEMORY_READ = "MEMORY_READ"
MEMORY_WRITE = "MEMORY_WRITE"
MEMORY_CORRECTED = "MEMORY_CORRECTED"
MEMORY_DELETED = "MEMORY_DELETED"
ASK_USER = "ASK_USER"
STATE_CHANGED = "STATE_CHANGED"
LLM_CALLED = "LLM_CALLED"
GUARD_TRIGGERED = "GUARD_TRIGGERED"


def log(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    task_id: str | None = None,
    company_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO audit_event (task_id, company_id, event_type, payload)"
        " VALUES (?,?,?,?)",
        (
            task_id,
            company_id,
            event_type,
            json.dumps(payload or {}, ensure_ascii=False, default=str),
        ),
    )
    return int(cur.lastrowid or 0)


def timeline(
    conn: sqlite3.Connection, *, task_id: str | None = None, limit: int = 40
) -> list[sqlite3.Row]:
    if task_id:
        rows = conn.execute(
            "SELECT * FROM audit_event WHERE task_id=? ORDER BY event_id DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_event ORDER BY event_id DESC LIMIT ?", (limit,)
        ).fetchall()
    return list(reversed(rows))


def format_timeline(rows: list[sqlite3.Row]) -> str:
    """把事件流渲染成演示时能念出来的时间线。"""
    lines: list[str] = []
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        detail = ", ".join(f"{k}={v}" for k, v in payload.items() if v is not None)
        if len(detail) > 90:
            detail = detail[:87] + "..."
        lines.append(f"  [{row['event_id']:>3}] {row['event_type']:<16} {detail}")
    return "\n".join(lines)
