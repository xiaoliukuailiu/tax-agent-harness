"""工具层：所有读写都从这里走，模型没有第二个入口。

这个文件是整套系统的"边界"。四条设计原则：

1. **权限每次查表**，不从对话里取（材料：不能通过自然语言更改身份或权限）。
2. **写前五项校验**（企业 / 对象 / 内容 / 字段白名单 / 重复），任何一项不过就返回结构化错误。
3. **三态回执**：success / processing / failed。"已保存"三个字只能由 success + 回执号支撑。
4. **保存后重新查询核对**，返回的是库里的真实值，不是调用方传来的 patch。

金额一律用整数「分」传递，格式化在展示层做 —— 材料要求两位小数且不得有浮点尾差。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from . import audit, config, db, memory, policy
from .policy import yuan

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# 只允许修改这两个字段（材料：只允许修改发票用途和备注）
WRITABLE_FIELDS = {"purpose", "remark"}
MAX_TEXT_LEN = 200


@dataclass
class Session:
    """一次会话的作用域。所有工具调用都必须带着它。"""

    user_id: str
    company_id: str
    task_id: str = "T-DEMO"
    stage: str = "new"
    notes: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------ 响应构造


def _fail(code: str, detail: str, *, retryable: bool = False, **extra: Any) -> dict:
    return {
        "status": "failed",
        "error_code": code,
        "detail": detail,
        "retryable": retryable,
        **extra,
    }


def _need(missing: list[str], question: str) -> dict:
    return {"status": "need_info", "missing": missing, "question": question}


def _check_access(session: Session, conn: sqlite3.Connection, *, need_modify: bool = False) -> dict | None:
    perm = db.permission(conn, session.user_id, session.company_id)
    name = db.company_name(conn, session.company_id)
    if perm is None:
        return _fail(
            "NO_PERMISSION",
            f"{session.user_id} 无权访问{name}（{session.company_id}）。"
            "这属于权限不足，不是「没有记录」。",
        )
    if need_modify and perm != db.PERM_MODIFY:
        return _fail(
            "PERMISSION_DENIED",
            f"{session.user_id} 对{name}只有查看权限，不能修改发票用途或备注。",
            perm=perm,
        )
    return None


def _log_call(
    conn: sqlite3.Connection,
    session: Session,
    tool: str,
    args: dict,
    result: dict,
    *,
    latency_ms: int | None = None,
    idem: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO tool_log (task_id, company_id, tool_name, args, result, status, idem_key,"
        " receipt_id, latency_ms) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            session.task_id,
            session.company_id,
            tool,
            json.dumps(args, ensure_ascii=False, default=str),
            json.dumps(result, ensure_ascii=False, default=str),
            result.get("status", "failed"),
            idem,
            None,
            latency_ms,
        ),
    )
    return int(cur.lastrowid or 0)


def _track(
    conn: sqlite3.Connection, session: Session, tool: str, args: dict, result: dict, *, idem: str | None = None
) -> dict:
    """记录审计事件（运行记录的来源）。

    写完立即提交：SQLite 是单写者模型，留着未提交事务会把后面的写入锁死。
    """
    _log_call(conn, session, tool, args, result, idem=idem)
    audit.log(
        conn,
        audit.TOOL_CALLED,
        task_id=session.task_id,
        company_id=session.company_id,
        payload={
            "tool": tool,
            "args": json.dumps(args, ensure_ascii=False, default=str),
            "status": result.get("status"),
            "receipt_id": result.get("receipt_id"),
        },
    )
    conn.commit()
    return result


def _check_period(period: str | None) -> tuple[str | None, dict | None]:
    if not period:
        return None, _need(["period"], "要查哪个月份？请给出年月，例如 2026-09。")
    if not PERIOD_RE.match(period):
        return None, _fail("INVALID_PERIOD", f"月份格式不对：{period}，应为 yyyy-mm，例如 2026-09。")
    return period, None


# ------------------------------------------------------------------ 读工具


def query_invoices(
    session: Session, period: str | None = None, *, conn: sqlite3.Connection | None = None
) -> dict:
    """查询某企业某月的发票张数与价税合计。"""
    conn = conn or db.connect()
    args = {"period": period}
    if (err := _check_access(session, conn)) is not None:
        return _track(conn, session, "query_invoices", args, err)

    period, err = _check_period(period)
    if err:
        return _track(conn, session, "query_invoices", args, err)

    start, end = policy.period_bounds(period)
    row = conn.execute(
        "SELECT count(*) AS cnt, coalesce(sum(amount_cents),0) AS total FROM invoice"
        " WHERE company_id=? AND issue_date BETWEEN ? AND ?",
        (session.company_id, start, end),
    ).fetchone()

    result = {
        "status": "success",
        "tool": "query_invoices",
        "company": db.company_name(conn, session.company_id),
        "period": period,
        "count": row["cnt"],
        "total_cents": row["total"],
        "total": yuan(row["total"]),
        "basis": "《金额统计说明》1.0 版",
    }
    if row["cnt"] == 0:
        result["note"] = f"{period} 查询成功但没有发票记录，合计为 0.00 元。"
    return _track(conn, session, "query_invoices", args, result)


def query_bank(
    session: Session, period: str | None = None, *, conn: sqlite3.Connection | None = None
) -> dict:
    """查询某企业某月的银行支出与收入（分别统计，不互相抵销）。"""
    conn = conn or db.connect()
    args = {"period": period}
    if (err := _check_access(session, conn)) is not None:
        return _track(conn, session, "query_bank", args, err)

    period, err = _check_period(period)
    if err:
        return _track(conn, session, "query_bank", args, err)

    start, end = policy.period_bounds(period)
    rows = conn.execute(
        "SELECT direction, coalesce(sum(amount_cents),0) AS total, count(*) AS cnt FROM bank_txn"
        " WHERE company_id=? AND txn_date BETWEEN ? AND ? GROUP BY direction",
        (session.company_id, start, end),
    ).fetchall()
    by_dir = {r["direction"]: r for r in rows}
    expense = by_dir.get("支出")
    income = by_dir.get("收入")

    result = {
        "status": "success",
        "tool": "query_bank",
        "company": db.company_name(conn, session.company_id),
        "period": period,
        "count": (expense["cnt"] if expense else 0) + (income["cnt"] if income else 0),
        "expense_cents": expense["total"] if expense else 0,
        "income_cents": income["total"] if income else 0,
        "expense": yuan(expense["total"] if expense else 0),
        "income": yuan(income["total"] if income else 0),
        "note": "收入与支出分别统计、不互相抵销；与发票金额不要求相等，不能据此判断欠款或已付款。",
        "basis": "《银行资料说明》1.0 版",
    }
    return _track(conn, session, "query_bank", args, result)


def list_invoices(
    session: Session, period: str | None = None, *, conn: sqlite3.Connection | None = None
) -> dict:
    """列出某月发票明细（含用途、备注），用于检查与展示。"""
    conn = conn or db.connect()
    args = {"period": period}
    if (err := _check_access(session, conn)) is not None:
        return _track(conn, session, "list_invoices", args, err)
    period, err = _check_period(period)
    if err:
        return _track(conn, session, "list_invoices", args, err)

    start, end = policy.period_bounds(period)
    rows = conn.execute(
        "SELECT invoice_no, issue_date, seller, item_name, amount_cents, purpose, remark"
        " FROM invoice WHERE company_id=? AND issue_date BETWEEN ? AND ? ORDER BY invoice_no",
        (session.company_id, start, end),
    ).fetchall()
    items = [
        {
            "invoice_no": r["invoice_no"],
            "date": r["issue_date"],
            "seller": r["seller"],
            "item": r["item_name"],
            "amount": yuan(r["amount_cents"]),
            "purpose": r["purpose"],
            "remark": r["remark"],
        }
        for r in rows
    ]
    return _track(
        conn,
        session,
        "list_invoices",
        args,
        {"status": "success", "period": period, "count": len(items), "invoices": items},
    )


def read_invoice(
    session: Session, invoice_no: str, *, conn: sqlite3.Connection | None = None
) -> dict:
    """按"企业 + 发票编号"读取一张发票（同号可能属于不同企业，必须联合定位）。"""
    conn = conn or db.connect()
    args = {"invoice_no": invoice_no}
    if (err := _check_access(session, conn)) is not None:
        return _track(conn, session, "read_invoice", args, err)

    row = conn.execute(
        "SELECT * FROM invoice WHERE company_id=? AND invoice_no=?",
        (session.company_id, invoice_no),
    ).fetchone()
    if row is None:
        return _track(
            conn,
            session,
            "read_invoice",
            args,
            _fail("TARGET_NOT_FOUND", f"{session.company_id} 下没有发票 {invoice_no}。"),
        )
    result = {
        "status": "success",
        "invoice_no": row["invoice_no"],
        "amount": yuan(row["amount_cents"]),
        "purpose": row["purpose"],
        "remark": row["remark"],
        "seller": row["seller"],
        "item": row["item_name"],
        "date": row["issue_date"],
    }
    return _track(conn, session, "read_invoice", args, result)


def missing_purpose(
    session: Session, period: str | None = None, *, conn: sqlite3.Connection | None = None
) -> dict:
    """列出未填用途的发票，并说明依据的规则版本。"""
    conn = conn or db.connect()
    args = {"period": period}
    if (err := _check_access(session, conn)) is not None:
        return _track(conn, session, "missing_purpose", args, err)

    period, err = _check_period(period)
    if err:
        return _track(conn, session, "missing_purpose", args, err)

    active = policy.active_purpose_rule(conn, session.company_id, period)
    if active is None:
        result = {
            "status": "not_applicable",
            "tool": "missing_purpose",
            "period": period,
            "note": policy.explain_absence(conn, session.company_id, period)
            + "初赛仅检查 2026-09 的发票用途，8 月数据用于金额查询。",
        }
        return _track(conn, session, "missing_purpose", args, result)

    start, end = policy.period_bounds(period)
    rows = conn.execute(
        "SELECT invoice_no, amount_cents, purpose FROM invoice"
        " WHERE company_id=? AND issue_date BETWEEN ? AND ? ORDER BY invoice_no",
        (session.company_id, start, end),
    ).fetchall()

    missing = []
    for r in rows:
        filled = bool((r["purpose"] or "").strip())  # 纯空格视为未填写
        if not filled and policy.needs_purpose(r["amount_cents"], active):
            missing.append({"invoice_no": r["invoice_no"], "amount": yuan(r["amount_cents"])})

    audit.log(
        conn,
        audit.RULE_CITED,
        task_id=session.task_id,
        company_id=session.company_id,
        payload={"doc": active.doc_id, "version": active.version, "rule": active.citation},
    )

    result = {
        "status": "success",
        "tool": "missing_purpose",
        "period": period,
        "checked": len(rows),
        "count": len(missing),
        "invoice_nos": [m["invoice_no"] for m in missing],
        "invoices": missing,
        "basis": active.citation,
        "policy": active.note,
    }
    if not missing:
        result["note"] = f"{period} 所有需要填用途的发票都已填写（依据 {active.citation}）。"
    return _track(conn, session, "missing_purpose", args, result)


def search_rules(
    session: Session,
    keyword: str | None = None,
    period: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """检索规则。只返回现行版本，并带上名称与版本号。"""
    conn = conn or db.connect()
    args = {"keyword": keyword, "period": period}
    found = policy.search(conn, company_id=session.company_id, keyword=keyword, as_of=period)
    result = {
        "status": "success",
        "tool": "search_rules",
        "count": len(found),
        "rules": [
            {
                "doc_id": r["doc_id"],
                "version": r["version"],
                "title": r["title"],
                "citation": policy.cite(r),
                "effective_from": r["effective_from"],
                "content": r["content"],
            }
            for r in found
        ],
    }
    if not found:
        result["status"] = "not_found"
        result["note"] = "没有匹配的现行规则，不能凭印象作答。"
    return _track(conn, session, "search_rules", args, result)


# ------------------------------------------------------------------ 写工具


def _idem_key(company_id: str, invoice_no: str, patch: dict) -> str:
    raw = f"{company_id}|{invoice_no}|{json.dumps(patch, ensure_ascii=False, sort_keys=True)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _find_receipt(conn: sqlite3.Connection, company_id: str, idem: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT receipt_id, result, created_at FROM tool_log"
        " WHERE company_id=? AND idem_key=? AND status='success'",
        (company_id, idem),
    ).fetchone()


def save_invoice(
    session: Session,
    invoice_no: str,
    *,
    purpose: str | None = None,
    remark: str | None = None,
    idempotency_key: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """保存发票用途/备注（面向调用方的便捷入口）。"""
    patch = {k: v for k, v in {"purpose": purpose, "remark": remark}.items() if v is not None}
    return save_patch(session, invoice_no, patch, idempotency_key=idempotency_key, conn=conn)


def save_patch(
    session: Session,
    invoice_no: str,
    patch: dict,
    *,
    idempotency_key: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """唯一写入口：任何字段补丁都必须过五项校验。

    五项校验 → 幂等 → 事务落库 → **重新查询核对** → 三态回执。
    """
    started = time.perf_counter()
    conn = conn or db.connect()
    patch = {k: v for k, v in (patch or {}).items() if v is not None}
    args = {"invoice_no": invoice_no, "patch": patch}
    idem = idempotency_key or _idem_key(session.company_id, invoice_no, patch)

    def done(result: dict, *, record: bool = True) -> dict:
        """校验未通过/命中幂等时的提前返回。

        record=False 用于"幂等命中"：此时绝不能再用同一个幂等键写一行日志
        （唯一索引正是靠这个键拦住重复保存的）。
        """
        latency = int((time.perf_counter() - started) * 1000)
        if record:
            _log_call(conn, session, "save_invoice", args, result, latency_ms=latency, idem=idem)
        audit.log(
            conn,
            audit.TOOL_CALLED,
            task_id=session.task_id,
            company_id=session.company_id,
            payload={
                "tool": "save_invoice",
                "invoice_no": invoice_no,
                "patch": json.dumps(patch, ensure_ascii=False),
                "status": result.get("status"),
                "receipt_id": result.get("receipt_id"),
                "latency_ms": latency,
            },
        )
        conn.commit()
        return result

    # ① 权限
    if (err := _check_access(session, conn, need_modify=True)) is not None:
        return done(err)

    # ② 字段白名单（金额、日期、销售方等只读字段在这里被挡住）
    illegal = set(patch) - WRITABLE_FIELDS
    if illegal:
        return done(
            _fail(
                "READONLY_FIELD",
                f"{'、'.join(sorted(illegal))} 不可修改。只允许修改发票用途和备注。",
            )
        )
    if not patch:
        return done(_fail("INVALID_CONTENT", "没有要修改的内容：请给出用途或备注。"))

    # ③ 内容校验
    for field_name, value in patch.items():
        if not isinstance(value, str):
            return done(_fail("INVALID_CONTENT", f"{field_name} 必须是文本。"))
        if not value.strip():
            return done(_fail("INVALID_CONTENT", f"{field_name} 不能是空白字符。"))
        if len(value.strip()) > MAX_TEXT_LEN:
            return done(_fail("INVALID_CONTENT", f"{field_name} 超过 {MAX_TEXT_LEN} 字。"))

    # ④ 对象存在性（企业 + 编号联合定位）
    target = conn.execute(
        "SELECT invoice_no, amount_cents, purpose, remark FROM invoice"
        " WHERE company_id=? AND invoice_no=?",
        (session.company_id, invoice_no),
    ).fetchone()
    if target is None:
        return done(
            _fail(
                "TARGET_NOT_FOUND",
                f"{session.company_id} 下没有发票 {invoice_no}，请先确认企业和发票编号。",
            )
        )

    # ⑤ 重复：幂等键已成功过 → 直接返回上次回执，不重复落库
    existing = _find_receipt(conn, session.company_id, idem)
    if existing:
        try:
            saved = json.loads(existing["result"]).get("saved", {})
        except (json.JSONDecodeError, AttributeError):
            saved = {}
        return done(
            {
                "status": "success",
                "tool": "save_invoice",
                "duplicate": True,
                "receipt_id": existing["receipt_id"],
                "idempotency_key": idem,
                "saved_at": existing["created_at"],
                "saved": saved,
                "note": "这次修改此前已成功保存过，直接返回原回执，未重复写入。",
            },
            record=False,
        )

    # 落库：业务表 + 工具日志 + 审计在同一事务里，要么全成要么全不成
    try:
        with db.transaction(conn):
            conn.execute(
                "UPDATE invoice SET purpose=coalesce(?, purpose), remark=coalesce(?, remark),"
                " updated_at=datetime('now') WHERE company_id=? AND invoice_no=?",
                (patch.get("purpose"), patch.get("remark"), session.company_id, invoice_no),
            )
            log_id = _log_call(
                conn,
                session,
                "save_invoice",
                args,
                {"status": "success"},
                idem=idem,
            )
            receipt = f"R-{log_id:04d}"
            conn.execute("UPDATE tool_log SET receipt_id=? WHERE log_id=?", (receipt, log_id))

            # 保存后重新查询核对：返回库里真实值，而不是调用方给的 patch
            after = conn.execute(
                "SELECT purpose, remark, amount_cents FROM invoice"
                " WHERE company_id=? AND invoice_no=?",
                (session.company_id, invoice_no),
            ).fetchone()
            saved = {
                "invoice_no": invoice_no,
                "amount": yuan(after["amount_cents"]),
                "purpose": after["purpose"],
                "remark": after["remark"],
            }
            expected = patch.get("purpose", after["purpose"])
            result = {
                "status": "success",
                "tool": "save_invoice",
                "duplicate": False,
                "receipt_id": receipt,
                "idempotency_key": idem,
                "saved": saved,
                "verified": (after["purpose"] or "") == (expected or ""),
            }
            conn.execute(
                "UPDATE tool_log SET result=? WHERE log_id=?",
                (json.dumps(result, ensure_ascii=False), log_id),
            )
            audit.log(
                conn,
                audit.STATE_CHANGED,
                task_id=session.task_id,
                company_id=session.company_id,
                payload={"invoice_no": invoice_no, "receipt_id": receipt, "saved": saved},
            )
    except sqlite3.IntegrityError:
        # 并发下唯一索引兜底：说明另一路已经成功保存
        again = _find_receipt(conn, session.company_id, idem)
        if again:
            return done(
                {
                    "status": "success",
                    "tool": "save_invoice",
                    "duplicate": True,
                    "receipt_id": again["receipt_id"],
                    "idempotency_key": idem,
                    "saved": json.loads(again["result"]).get("saved", {}),
                    "note": "唯一索引拦截到重复写入，返回已有回执。",
                },
                record=False,
            )
        return done(_fail("SAVE_ERROR", "保存时触发唯一约束，请稍后重试。", retryable=True))

    # 事务已提交，补一条工具调用审计（tool_log 已在事务内写过，不重复）
    latency = int((time.perf_counter() - started) * 1000)
    audit.log(
        conn,
        audit.TOOL_CALLED,
        task_id=session.task_id,
        company_id=session.company_id,
        payload={
            "tool": "save_invoice",
            "invoice_no": invoice_no,
            "patch": json.dumps(patch, ensure_ascii=False),
            "status": result["status"],
            "receipt_id": result["receipt_id"],
            "latency_ms": latency,
        },
    )
    conn.commit()

    if not result["verified"]:
        result["status"] = "failed"
        result["error_code"] = "VERIFY_MISMATCH"
        result["detail"] = "保存后复核发现与预期不一致，请以库中值为准并人工确认。"

    return result


def get_save_result(
    session: Session, idempotency_key: str, *, conn: sqlite3.Connection | None = None
) -> dict:
    """按幂等键查上次保存结果 —— 进程重启后恢复任务时先调这个。"""
    conn = conn or db.connect()
    row = conn.execute(
        "SELECT receipt_id, result, created_at, status FROM tool_log"
        " WHERE company_id=? AND idem_key=? AND status='success'",
        (session.company_id, idempotency_key),
    ).fetchone()
    if row is None:
        return {
            "status": "not_found",
            "note": "该幂等键没有成功记录，说明上次没有保存成功，可以重新执行。",
        }
    return {
        "status": "success",
        "receipt_id": row["receipt_id"],
        "saved_at": row["created_at"],
        "saved": json.loads(row["result"]).get("saved", {}),
        "note": "上次已保存成功，请直接确认，不要重复保存。",
    }


# ------------------------------------------------------------------ 供 LLM 绑定的工具集


def build_tools(session: Session) -> list:
    """把上面的函数包装成 LangChain 工具，供 create_agent 绑定。

    每个工具的 docstring 就是给模型看的"什么时候用它"，所以写清场景，不要写参数类型。
    """
    from langchain_core.tools import tool

    # 工具名必须是 ^[a-zA-Z0-9_-]+$（OpenAI 兼容接口的硬约束，中文名会被 DeepSeek 等拒绝）；
    # 语义靠 docstring 传达，模型照样能选对工具。
    @tool("query_invoice_summary")
    def invoice_summary(period: str) -> dict:
        """查询当前企业指定月份的发票张数和价税合计金额。period 形如 2026-09。"""
        return query_invoices(session, period)

    @tool("query_bank_summary")
    def bank_summary(period: str) -> dict:
        """查询当前企业指定月份的银行支出和收入，两者分别统计。period 形如 2026-09。"""
        return query_bank(session, period)

    @tool("list_missing_purpose")
    def missing_purpose_list(period: str) -> dict:
        """按现行规则检查指定月份哪些发票没有填写用途，返回编号清单和依据的规则版本。"""
        return missing_purpose(session, period)

    @tool("list_invoice_detail")
    def invoice_detail(period: str) -> dict:
        """列出指定月份全部发票的明细，含用途与备注，用于核对。period 形如 2026-09。"""
        return list_invoices(session, period)

    @tool("query_rules")
    def rules_query(keyword: str) -> dict:
        """检索现行有效的业务规则，返回规则名称、版本和正文。keyword 可为空字符串。"""
        return search_rules(session, keyword or None)

    @tool("save_invoice")
    def invoice_save(invoice_no: str, purpose: str = "", remark: str = "") -> dict:
        """保存发票的用途和/或备注。只能在用户明确要求修改时调用，返回回执号才算保存成功。"""
        return save_invoice(
            session,
            invoice_no,
            purpose=purpose or None,
            remark=remark or None,
        )

    return [invoice_summary, bank_summary, missing_purpose_list, invoice_detail, rules_query, invoice_save]
