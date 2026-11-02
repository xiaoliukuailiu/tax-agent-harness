"""自研中间件：把"模型外面的壳"挂到 LangChain 的 agent loop 上。

三个钩子分工（都在 langchain 1.x 的 AgentMiddleware 里）：
    wrap_model_call  调模型前 —— 注入上下文（Scope）
    wrap_tool_call   工具前后 —— 留痕（Audit）
    after_model      模型回复后 —— 守门（回执）与改语气（Style）
"""

from __future__ import annotations

import json
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, SystemMessage

from . import audit, context, db, style


def _has_receipt(messages: list[Any], lookback: int = 6) -> bool:
    """本轮是否有成功的保存回执。判定依据是工具返回，不是模型的话。"""
    for msg in reversed(messages[-lookback:]):
        content = getattr(msg, "content", "")
        if isinstance(content, list):
            content = json.dumps(content, ensure_ascii=False, default=str)
        text = str(content)
        if '"receipt_id"' in text and "R-" in text:
            return True
    return False


class ScopeMiddleware(AgentMiddleware):
    """每轮调模型前，把作用域、待办、记忆、规则装进 system prompt。

    对应官方要求 4（模型输入）与加分项 1（长对话摘要之外的另一半：装配与裁剪）。
    """

    def __init__(self, session, *, period: str | None = None):
        super().__init__()
        self.session = session
        self.period = period

    def wrap_model_call(self, request, handler):
        try:
            ctx = context.build(self.session, period=self.period)
            base = request.system_message.content if request.system_message else ""
            merged = f"{base}\n\n{ctx['text']}".strip()
            return handler(request.override(system_message=SystemMessage(content=merged)))
        except Exception:
            # 上下文装配失败不能拖垮主链路：退化为原始请求
            return handler(request)


class ToolAuditMiddleware(AgentMiddleware):
    """工具调用全量留痕，落 audit_event（运行记录的来源）。"""

    def __init__(self, session, *, conn=None):
        super().__init__()
        self.session = session
        self._conn = conn

    def wrap_tool_call(self, request, handler):
        tool_call = getattr(request, "tool_call", {}) or {}
        name = tool_call.get("name", "unknown")
        args = tool_call.get("args", {})
        try:
            result = handler(request)
        except Exception as exc:  # 工具异常也要留痕，且不谎报成功
            self._log(name, args, "failed", {"error": str(exc)})
            raise
        text = str(getattr(result, "content", result))
        status = "success" if '"failed"' not in text else "failed"
        self._log(name, args, status, None)
        return result

    def _log(self, name: str, args: Any, status: str, extra: dict | None) -> None:
        try:
            conn = self._conn or db.connect()
            audit.log(
                conn,
                audit.TOOL_CALLED,
                task_id=self.session.task_id,
                company_id=self.session.company_id,
                payload={
                    "tool": name,
                    "args": json.dumps(args, ensure_ascii=False, default=str)[:200],
                    "status": status,
                    **(extra or {}),
                },
            )
            conn.commit()
        except Exception:
            pass


class ExpressionMiddleware(AgentMiddleware):
    """模型回复之后、给你看之前：检查有没有假称"已保存"，再删套话。

    注意：改写消息必须用 model_copy 保留原 message.id，
    否则 add_messages 会把它当成新消息追加，用户会看到两遍同样的话。
    """

    def __init__(self, session, *, conn=None):
        super().__init__()
        self.session = session
        self._conn = conn

    def after_model(self, state, runtime):
        messages = state.get("messages") or []
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage):
            return None

        text = last.content if isinstance(last.content, str) else str(last.content)
        if not text.strip():
            return None

        receipt = _has_receipt(messages)
        result = style.polish(text, has_receipt=receipt)
        if result["text"] == text:
            return None

        try:
            conn = self._conn or db.connect()
            audit.log(
                conn,
                audit.GUARD_TRIGGERED,
                task_id=self.session.task_id,
                company_id=self.session.company_id,
                payload={"rules": result["applied"], "has_receipt": receipt},
            )
            conn.commit()
        except Exception:
            pass

        patched = last.model_copy(update={"content": result["text"]})
        return {"messages": [patched]}
