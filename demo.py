#!/usr/bin/env python
"""一键演示：python demo.py

覆盖赛题三个场景 + 四个最容易失分的陷阱。
**不需要模型 Key** —— 本脚本用固定话术代替模型，专门演示"工具 + 规则 + 回执"这条确定性的主链路。
接上模型后走的是同一套工具，只是由 create_agent 决定调哪个（见 `python -m harness.cli --llm`）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from harness import config, context, db, memory, style, tools
from harness.policy import yuan
from harness.tools import Session

LINE = "─" * 76
ME = "助手"


def title(no: str, text: str) -> None:
    print(f"\n{LINE}\n{no} {text}\n{LINE}")


def say(who: str, text: str) -> None:
    print(f"{who}：{text}")


def reply(text: str, *, has_receipt: bool = False) -> None:
    """模型回复 + 去 AI 味管线（演示时能看到规则被触发）。"""
    result = style.polish(text, has_receipt=has_receipt)
    say(ME, result["text"])
    if result["applied"]:
        print(f"      ↳ 表达管线触发：{'、'.join(result['applied'])}")


def reset_db() -> None:
    subprocess.run(
        [sys.executable, str(config.ROOT / "scripts" / "import_seed.py")],
        check=True,
        capture_output=True,
    )
    print("数据库已按官方 Excel 重建（40 张发票 / 48 条流水 / 5 条规则）")


def scene_query(s: Session) -> None:
    title("场景一", "查询资料：发票金额、银行收支、规则依据")

    say("用户", "帮我看看 2026 年 9 月的发票和银行收支")
    inv = tools.query_invoices(s, "2026-09")
    bank = tools.query_bank(s, "2026-09")
    reply(
        f"甲企业 2026-09 有 {inv['count']} 张发票，价税合计 {inv['total']} 元；"
        f"银行支出 {bank['expense']} 元，收入 {bank['income']} 元。"
        f"依据{inv['basis']}与{bank['basis']}。"
        f"提醒一句：{bank['note']}"
    )

    say("用户", "那 7 月呢？")
    july = tools.query_invoices(s, "2026-07")
    reply(f"{july['note']}（{july['basis']}）")

    print("\n【陷阱 1】没权限 ≠ 没有记录：U002 查乙企业")
    guest = Session(user_id="U002", company_id="C002", task_id="T-DEMO")
    denied = tools.query_invoices(guest, "2026-09")
    reply(f"{denied['detail']}", has_receipt=False)
    print(f"      ↳ 工具状态：{denied['status']} / {denied['error_code']}（不能答 0.00）")


def scene_missing(s: Session) -> None:
    title("场景二·检查", "多轮检查：哪些发票缺用途，依据哪一版规则")

    say("用户", "9 月还有哪些发票没填用途？")
    miss = tools.missing_purpose(s, "2026-09")
    numbers = "、".join(miss["invoice_nos"])
    reply(
        f"{miss['period']} 有 {miss['count']} 张需要补用途：{numbers}。"
        f"依据{miss['basis']}（{miss['policy']}）。"
    )

    print("\n【陷阱 2】用错规则版本会漏报：甲企业 9 月按已停用的 1.0 版只报 1 张")
    rows = tools.list_invoices(s, "2026-09")["invoices"]
    old_rule_hits = [
        r["invoice_no"] for r in rows if not r["purpose"] and int(r["amount"].replace(".", "")) >= 100000
    ]
    print(f"      已停用 1.0 版（≥1000 必填）会报：{old_rule_hits or '无'} —— 共 {len(old_rule_hits)} 张")
    print(f"      现行 2.0 版（每张必填）应报：{miss['invoice_nos']} —— 共 {miss['count']} 张")

    say("用户", "8 月是不是也缺？")
    aug = tools.missing_purpose(s, "2026-08")
    reply(aug.get("note", "8 月不做用途检查。"))


def scene_save(s: Session) -> dict:
    title("场景三", "补充并保存：五项校验 → 回执 → 重新查询核对")

    say("用户", "把 F002 的用途补成「购买复印纸与打印服务」")
    saved = tools.save_invoice(s, "F002", purpose="购买复印纸与打印服务")
    print(f"      ↳ 工具返回：status={saved['status']} 回执={saved['receipt_id']} "
          f"幂等键={saved['idempotency_key'][:12]}…")
    reply(
        f"已保存 F002，用途现在是「{saved['saved']['purpose']}」，"
        f"金额 {saved['saved']['amount']} 元，回执 {saved['receipt_id']}。"
        f"我重新查过库，确认是这条。",
        has_receipt=True,
    )

    print("\n【陷阱 3】同一句话说了两次，不能存两条")
    again = tools.save_invoice(s, "F002", purpose="购买复印纸与打印服务")
    print(f"      ↳ 第二次返回：duplicate={again['duplicate']} 回执={again['receipt_id']}（与第一次相同）")
    print(f"      ↳ 提示：{again['note']}")

    print("\n【陷阱 4】只允许改用途和备注")
    bad = tools.save_patch(s, "F002", {"amount_cents": 50000})
    reply(f"{bad['detail']}")

    print("\n【陷阱 5】U002 对甲企业只有查看权限")
    guest = Session(user_id="U002", company_id="C001", task_id="T-DEMO-2")
    denied = tools.save_invoice(guest, "F002", purpose="测试越权")
    reply(f"{denied['detail']}")

    return saved


def scene_multiturn(s: Session, conn, saved: dict) -> None:
    title("场景四", "多轮接着办：待办、记忆、重启恢复")

    task = memory.ensure_task(conn, company_id=s.company_id, user_id=s.user_id)
    memory.set_pending(
        conn,
        task["task_id"],
        ["F003 待补用途", "F004 待补用途", "F006 待补用途", "F009 待补用途"],
    )
    conn.commit()
    print(f"未完成事项已写入任务 {task['task_id']}（上下文里永不裁剪）：")
    for item in ["F003", "F004", "F006", "F009"]:
        print(f"      - {item} 待补用途")

    say("用户", "我以后补用途就直接按票面项目描述写，别自己猜业务实质")
    memory_id = memory.add(
        conn,
        company_id=s.company_id,
        user_id=s.user_id,
        content="补用途时按票面项目描述填写，不臆测业务实质",
        source_ref="demo#turn-3",
    )
    conn.commit()
    reply(f"记下了（{memory_id}）：补用途时按票面项目描述写，不臆测业务实质。要改随时说。")

    print("\n【重启恢复】进程被杀后，先按幂等键查上次结果，再决定要不要重做")
    fresh = db.connect()  # 模拟新进程：新连接
    recovered = tools.get_save_result(s, saved["idempotency_key"], conn=fresh)
    print(f"      ↳ 查询结果：{recovered['status']} 回执={recovered['receipt_id']}")
    print(f"      ↳ 结论：{recovered['note']}")

    say("用户", "那接着把 F003 也补上吧，用途「快递费」")
    nxt = tools.save_invoice(s, "F003", purpose="快递费")
    memory.set_pending(conn, task["task_id"], ["F004 待补用途", "F006 待补用途", "F009 待补用途"])
    conn.commit()
    reply(
        f"已保存 F003，用途「{nxt['saved']['purpose']}」，回执 {nxt['receipt_id']}。"
        f"还剩 3 张没补：F004、F006、F009。",
        has_receipt=True,
    )


def show_context(s: Session, conn) -> None:
    title("附一", "本轮上下文怎么装配（含预算与永不裁剪项）")
    ctx = context.build(s, period="2026-09", conn=conn)
    print(context.render_plan(ctx))
    print("\n—— 装配后的 system prompt 片段 ——")
    preview = ctx["text"].split("## 相关记忆")[-1][:220]
    print("## 相关记忆" + preview + "…")


def show_audit(conn) -> None:
    title("附二", "运行记录（audit_event，只追加不修改）")
    from harness import audit  # noqa: F401  （保持局部导入，避免脚本顶部过重）

    print(audit.format_timeline(audit.timeline(conn, limit=18)))


def show_db(conn) -> None:
    title("附三", "库里到底存了什么（评委可现场核对）")
    rows = conn.execute(
        "SELECT invoice_no, amount_cents, purpose, remark FROM invoice"
        " WHERE company_id='C001' AND substr(issue_date,1,7)='2026-09'"
        " AND (purpose IS NOT NULL) ORDER BY invoice_no"
    ).fetchall()
    print(f"{'发票':<8}{'金额':>10}  用途 / 备注")
    for r in rows:
        print(f"{r['invoice_no']:<8}{yuan(r['amount_cents']):>10}  {r['purpose'] or '—'} / {r['remark'] or '—'}")

    dup = conn.execute(
        "SELECT count(*) c FROM tool_log WHERE status='success' AND idem_key IS NOT NULL"
    ).fetchone()["c"]
    print(f"\n成功写入的保存记录 {dup} 条；同一幂等键最多一条（数据库部分唯一索引兜底）。")


def main() -> None:
    parser = argparse.ArgumentParser(description="赛题二 Harness 演示")
    parser.add_argument("--keep", action="store_true", help="保留已有数据，不重置数据库")
    args = parser.parse_args()

    print("财税 Agent Harness · 初赛演示")
    print(f"数据库：{config.db_path()}")
    if not args.keep:
        reset_db()

    conn = db.connect()
    s = Session(user_id="U001", company_id="C001", task_id="T-DEMO")

    scene_query(s)
    scene_missing(s)
    saved = scene_save(s)
    scene_multiturn(s, conn, saved)
    show_context(s, conn)
    show_audit(conn)
    show_db(conn)

    print(f"\n{LINE}")
    print("演示结束。三件事都可以现场复核：")
    print("  1) 上面每条回复里的金额，都能在 data/harness.db 里用一条 SQL 复算出来；")
    print("  2) 每次保存都有回执号，对应 tool_log 里的一行真实记录；")
    print("  3) 重复保存不会产生第二条记录（幂等键 + 部分唯一索引双保险）。")
    print("\n接模型：在 .env 配好 HARNESS_BASE_URL / HARNESS_API_KEY / HARNESS_MODEL，")
    print("然后 `python -m harness.cli --llm`，同一套工具由模型来调。")
    print(LINE)


if __name__ == "__main__":
    main()
