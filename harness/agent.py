"""Agent 组装：模型 + 工具 + 中间件 + 检查点。

这里只做"接线"，不做业务逻辑 —— 业务全在 tools.py，规则在 policy.py，
上下文在 context.py。换模型、换界面都不用改业务。
"""

from __future__ import annotations

import sqlite3

from . import config, context, db, tools
from .middlewares import ExpressionMiddleware, ScopeMiddleware, ToolAuditMiddleware


def _checkpointer(conn: sqlite3.Connection):
    """有 langgraph-checkpoint-sqlite 就用，没有就退化为无检查点。

    检查点负责"重启后接着上次"（加分项 3）。没装依赖时不能因此跑不起来。
    """
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        return SqliteSaver(conn)
    except Exception:
        return None


def build_agent(session, *, period: str | None = None, conn: sqlite3.Connection | None = None):
    """组装一个可对话的 Agent。需要 .env 里配好模型。"""
    if not config.has_model():
        raise RuntimeError(
            "未配置模型。请在 .env 里设置 HARNESS_BASE_URL / HARNESS_API_KEY / HARNESS_MODEL，"
            "例如：\n"
            "  HARNESS_BASE_URL=https://api.deepseek.com\n"
            "  HARNESS_API_KEY=sk-xxx\n"
            "  HARNESS_MODEL=deepseek-chat\n"
            "没有 Key 也可以先用 `python demo.py` 演示完整主链路。"
        )

    # Key 会进 HTTP 头，必须是纯 ASCII。占位值里的中文会在 httpx 编码请求头时才炸，
    # 报的是 UnicodeEncodeError，很难定位，所以在这里先拦一道。
    key = config.api_key() or ""
    if not key.isascii():
        raise RuntimeError(
            "HARNESS_API_KEY 含非 ASCII 字符，多半还是 .env 里的占位值。"
            "请换成平台生成的真实 Key（sk- 开头的纯英文数字）。"
        )

    from langchain.agents import create_agent
    from langchain_openai import ChatOpenAI

    conn = conn or db.connect()
    model = ChatOpenAI(
        model=config.model_name(),
        base_url=config.base_url(),
        api_key=config.api_key(),
        temperature=0,
    )

    return create_agent(
        model=model,
        tools=tools.build_tools(session),
        system_prompt=context.SYSTEM_PROMPT,
        middleware=[
            ScopeMiddleware(session, period=period),   # 先装上下文
            ToolAuditMiddleware(session, conn=conn),   # 再留痕
            ExpressionMiddleware(session, conn=conn),  # 最后守门与改语气
        ],
        checkpointer=_checkpointer(conn),
    )


def chat(session, message: str, *, period: str | None = None, thread_id: str | None = None) -> str:
    """一问一答。thread_id 相同即同一任务，可跨进程恢复。"""
    agent = build_agent(session, period=period)
    cfg = {"configurable": {"thread_id": thread_id or session.task_id}}
    result = agent.invoke({"messages": [{"role": "user", "content": message}]}, config=cfg)
    messages = result.get("messages") or []
    return str(messages[-1].content) if messages else ""
