"""知识库检索：只检索现行版本，回答必须带版本号。

为什么不用向量库：
- 初赛只有 5 条规则，FTS5/子串匹配就够，且结果可解释（能说清命中了哪个词）。
- 向量检索调不好会更难解释，对"运行记录要能说明答案从哪来"是负分。
- 规则上千份再换，接口（search）保持不变即可。
"""

from __future__ import annotations

import sqlite3


def search(
    conn: sqlite3.Connection,
    *,
    company_id: str,
    keyword: str | None = None,
    as_of: str | None = None,
    include_deprecated: bool = False,
) -> list[dict]:
    """按企业 + 生效日期 + 关键词检索规则。

    as_of 传 '2026-09' 这类月份，表示"截至该月已生效"的规则。
    """
    sql = [
        "SELECT doc_id, version, title, content, scope_company, effective_from, status",
        "FROM rule_doc WHERE 1=1",
    ]
    args: list[object] = []

    if not include_deprecated:
        sql.append("AND status = 'active'")
    sql.append("AND (scope_company IS NULL OR scope_company = ?)")
    args.append(company_id)

    if as_of:
        sql.append("AND effective_from <= ?")
        args.append(f"{as_of}-01" if len(as_of) == 7 else as_of)

    if keyword:
        sql.append("AND (title LIKE ? OR content LIKE ?)")
        args.extend([f"%{keyword}%", f"%{keyword}%"])

    sql.append("ORDER BY doc_id, version DESC")
    rows = conn.execute(" ".join(sql), args).fetchall()
    return [dict(row) for row in rows]


def cite(rule: dict) -> str:
    return f"《{rule['title']}》{rule['version']} 版"


def coverage_note(conn: sqlite3.Connection, company_id: str, period: str) -> list[str]:
    """生成"本次依据了哪些规则"的清单，供回答末尾和审计使用。"""
    rules = search(conn, company_id=company_id, as_of=period)
    return [cite(r) for r in rules]
