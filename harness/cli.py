"""命令行操作入口：python -m harness.cli [--llm]

不带 --llm：直接调用工具（确定性，用于演示与核对）。
带 --llm：把自然语言交给 create_agent，由模型决定调哪个工具（需配置 .env）。

命令：
    /user U001            切换用户
    /company C001         切换企业
    /query 2026-09        发票汇总
    /bank 2026-09         银行收支
    /missing 2026-09      缺用途清单
    /list 2026-09         发票明细
    /rules [关键词]        规则检索
    /save F002 用途文本    保存用途（会打印回执号）
    /memory list|add|fix|del
    /audit                最近运行记录
    /context              本轮上下文预算
    /quit
"""

from __future__ import annotations

import argparse
import shlex

from . import audit, config, context, db, memory, tools
from .tools import Session

HELP = __doc__ or ""


def _print(obj) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("invoices", "rules") and isinstance(value, list):
                print(f"  {key}: {len(value)} 条")
                for item in value[:10]:
                    print(f"    - {item}")
            else:
                print(f"  {key}: {value}")
    else:
        print(obj)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm", action="store_true", help="用模型理解自然语言")
    parser.add_argument("--user", default="U001")
    parser.add_argument("--company", default="C001")
    args = parser.parse_args()

    conn = db.connect()
    task = memory.ensure_task(conn, company_id=args.company, user_id=args.user)
    conn.commit()
    session = Session(user_id=args.user, company_id=args.company, task_id=task["task_id"])

    print(f"财税 Harness 命令行　用户={session.user_id} 企业={session.company_id} "
          f"任务={session.task_id}")
    print("输入 /help 看命令；直接输入中文则交给模型（需 --llm）。")

    while True:
        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        if raw in ("/quit", "/exit", "exit", "quit"):
            break

        if not raw.startswith("/"):
            if not args.llm:
                print("当前是确定性模式：请用 /help 里的命令，或加 --llm 后再用自然语言。")
                continue
            from . import agent

            try:
                print(agent.chat(session, raw, thread_id=session.task_id))
            except Exception as exc:
                print(f"调用模型失败：{exc}")
            continue

        parts = shlex.split(raw)
        cmd, *rest = parts
        arg = rest[0] if rest else None

        if cmd == "/help":
            print(HELP)
        elif cmd == "/user" and arg:
            session.user_id = arg
            print(f"已切换用户：{arg}")
        elif cmd == "/company" and arg:
            session.company_id = arg
            print(f"已切换企业：{arg}")
        elif cmd == "/query":
            _print(tools.query_invoices(session, arg))
        elif cmd == "/bank":
            _print(tools.query_bank(session, arg))
        elif cmd == "/missing":
            _print(tools.missing_purpose(session, arg))
        elif cmd == "/list":
            _print(tools.list_invoices(session, arg))
        elif cmd == "/rules":
            _print(tools.search_rules(session, " ".join(rest) or None))
        elif cmd == "/save":
            if len(rest) < 2:
                print("用法：/save F002 用途文本")
                continue
            _print(tools.save_invoice(session, rest[0], purpose=" ".join(rest[1:])))
        elif cmd == "/memory":
            sub = rest[0] if rest else "list"
            if sub == "list":
                rows = memory.list_active(conn, company_id=session.company_id, user_id=session.user_id)
                print(memory.render(rows))
            elif sub == "add" and len(rest) > 1:
                mid = memory.add(conn, company_id=session.company_id, user_id=session.user_id,
                                content=" ".join(rest[1:]))
                conn.commit()
                print(f"已记下：{mid}")
            elif sub == "fix" and len(rest) > 2:
                ok = memory.correct(conn, rest[1], " ".join(rest[2:]))
                conn.commit()
                print("已更正" if ok else "没有这条记忆")
            elif sub == "del" and len(rest) > 1:
                ok = memory.remove(conn, rest[1])
                conn.commit()
                print("已删除" if ok else "没有这条记忆")
            else:
                print("用法：/memory list | add 内容 | fix M-xxx 新内容 | del M-xxx")
        elif cmd == "/audit":
            print(audit.format_timeline(audit.timeline(conn, task_id=session.task_id, limit=20)))
        elif cmd == "/context":
            result = context.build(session, conn=conn)
            print(context.render_plan(result))
        else:
            print(f"未知命令：{cmd}，/help 查看可用命令。")

    conn.close()


if __name__ == "__main__":
    main()
