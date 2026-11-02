"""去 AI 味：确定性后处理管线。

为什么不用提示词：模型对"请自然一些"的遵守率大概七成，剩下三成恰好会出现在评委看的那次演示里。
做成代码规则之后，"说话像人"就从模型能力问题变成了可测试的工程问题
—— 可以写单测断言黑名单命中数为 0，模型做不到这一点。

7 条规则对应《赛题二-完整系统设计》第 5 章。
"""

from __future__ import annotations

import re

# 规则 2：套话黑名单。
# 只清理"句首"和"句尾"的套话 —— 正文里正常出现的"首先我们确认"这类表述要保留。
_LEAD = re.compile(
    r"^(?:好的?|首先|其次|另外|总的来说|综上所述|非常抱歉|非常感谢)[，,。！!\s]*"
)
_TAIL = re.compile(
    r"(?:希望(?:对|能对)(?:您|你)有帮助"
    r"|如果(?:您|你)还有其他问题[^。！!\n]*"
    r"|作为(?:一个)?(?:AI|人工智能)[^。！!\n]*"
    r"|让我(?:来)?(?:帮|为)(?:您|你)[^。！!\n]*)"
    r"[。！!]?\s*$"
)

# 规则 4：完成语断言。没有回执时不允许出现。
COMPLETION_RE = re.compile(r"已(?:经)?(?:保存|更新|修改|写入|完成)(?:好|完毕)?")
_SAVE_NO_RECEIPT = "这一步还没有拿到保存回执，我先确认结果再回复你。"

# 规则 3：机械列点
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、)])\s+", re.MULTILINE)

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text) if s and s.strip()]


def has_completion_claim(text: str) -> bool:
    return bool(COMPLETION_RE.search(text))


def polish(text: str, *, has_receipt: bool = False, max_sentences: int = 6) -> dict:
    """返回 {'text': 处理后文本, 'applied': [触发的规则名]}。

    has_receipt 由调用方根据本轮工具回执判定，模型自己说了不算。
    """
    applied: list[str] = []
    out = (text or "").strip()

    # 规则 2：删套话（逐句清理句首/句尾，避免误伤正文）
    cleaned, changed = _strip_fillers(out)
    if changed:
        applied.append("套话清除")
        out = cleaned

    # 规则 4：无回执不得声称完成（最高优先级，命中直接改写）
    if not has_receipt and has_completion_claim(out):
        out = _SAVE_NO_RECEIPT
        applied.append("无回执禁用完成语")
        return {"text": out, "applied": applied}

    # 规则 1：结论前置 —— 首句没有数字时，把后面带数字的句子提上来
    sentences = _sentences(out)
    if len(sentences) >= 2 and not re.search(r"\d", sentences[0]):
        for idx, sentence in enumerate(sentences[1:], start=1):
            if re.search(r"\d", sentence):
                sentences.insert(0, sentences.pop(idx))
                out = "".join(sentences)
                applied.append("结论前置")
                break

    # 规则 3：列点节制 —— 超过 4 条且每条都很短，合并成一句话
    bullets = _BULLET_RE.findall(out)
    if len(bullets) > 4:
        items = [b.strip() for b in _BULLET_RE.split(out) if b.strip()]
        if all(len(i) < 14 for i in items):
            out = "、".join(items)
            applied.append("列点合并")

    # 规则 5：空转回复 —— 没有信息增量就补一个明确动作
    if len(out) < 10 and not re.search(r"\d", out):
        out = (out + "需要我继续查哪一项？").strip()
        applied.append("拒绝空转")

    # 规则 7：长度约束 —— 简单问题不写小作文
    sentences = _sentences(out)
    if len(sentences) > max_sentences:
        out = "".join(sentences[:max_sentences])
        applied.append("长度收敛")

    return {"text": out.strip(), "applied": applied}


def _strip_fillers(text: str) -> tuple[str, bool]:
    """逐句剥掉句首套话与句尾客套话。返回 (新文本, 是否改动过)。"""
    changed = False
    kept: list[str] = []
    for sentence in _sentences(text):
        current = sentence
        while True:
            trimmed = _LEAD.sub("", current).strip()
            if trimmed == current.strip():
                break
            current, changed = trimmed, True
        if _TAIL.search(current):
            current, changed = _TAIL.sub("", current).strip(), True
        if current:
            kept.append(current)
    return "".join(kept), changed


def blacklist_hits(text: str) -> list[str]:
    """给单测用：返回仍然命中套话的句子（正常输出应为空列表）。"""
    hits = [s for s in _sentences(text) if _LEAD.match(s) or _TAIL.search(s)]
    return hits
