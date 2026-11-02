"""政策即代码：把"用途什么时候必填"这类业务参数从模型里拿出来。

为什么单独一个模块：
- 材料里 R002 有 v1.0（已停用，≥1000 必填）和 v2.0（9 月起每张必填）两个版本。
  如果让模型"理解政策"，它很可能拿旧版算新账 —— 这正是最典型的失分点。
- 所以：**选哪个版本由数据库的生效日期和状态决定，阈值由本模块的常量表决定，
  模型只负责把条款解释给用户听。**

版本选择规则（对应《03_业务规则》初始规则清单）：
    status='active'
    且 effective_from <= 查询月份第一天
    且 企业范围匹配（scope_company 为空表示两家通用）
满足条件的最高生效日版本胜出。找不到 → 该月没有现行规则（例如 2026-08：
R002 1.0 已停用、2.0 自 2026-09-01 才生效），因此 8 月不做用途检查。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class PurposePolicy:
    """一条已生效的发票用途规则。"""

    doc_id: str
    version: str
    title: str
    threshold_cents: int
    requires_all: bool
    note: str

    @property
    def citation(self) -> str:
        """用于回答末尾注明依据（材料要求"注明规则名称和版本"）。"""
        return f"《{self.title}》{self.version} 版"


# (doc_id, version) -> 结构化参数。政策文本在 rule_doc 表里，参数在这里。
# 这样改政策只需要动一处，且能被单元测试直接断言。
_POLICY_PARAMS: dict[tuple[str, str], dict] = {
    ("R002", "2.0"): dict(
        threshold_cents=0,
        requires_all=True,
        note="每张发票都必须填用途，不设金额门槛",
    ),
    ("R002", "1.0"): dict(
        threshold_cents=100_000,
        requires_all=False,
        note="≥1000.00 元必填（已停用，仅保留备查）",
    ),
    ("R003", "1.0"): dict(
        threshold_cents=100_000,
        requires_all=False,
        note="≥1000.00 元必填，等于 1000.00 元也在范围内",
    ),
}

PURPOSE_DOC_IDS = ("R002", "R003")


def period_bounds(period: str) -> tuple[str, str]:
    """'2026-09' -> ('2026-09-01', '2026-09-30')"""
    year, month = (int(part) for part in period.split("-"))
    first = date(year, month, 1)
    nxt = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last = nxt.toordinal() - 1
    return first.isoformat(), date.fromordinal(last).isoformat()


def active_purpose_rule(
    conn: sqlite3.Connection, company_id: str, period: str
) -> PurposePolicy | None:
    """取该企业该月份适用的用途规则；没有现行规则时返回 None。"""
    start, _ = period_bounds(period)
    rows = conn.execute(
        """
        SELECT doc_id, version, title, scope_company, effective_from
        FROM rule_doc
        WHERE status = 'active'
          AND doc_id IN (?, ?)
          AND effective_from <= ?
          AND (scope_company IS NULL OR scope_company = ?)
        ORDER BY (scope_company IS NOT NULL) DESC, effective_from DESC, version DESC
        """,
        (*PURPOSE_DOC_IDS, start, company_id),
    ).fetchall()

    for row in rows:
        params = _POLICY_PARAMS.get((row["doc_id"], row["version"]))
        if params:
            return PurposePolicy(
                doc_id=row["doc_id"],
                version=row["version"],
                title=row["title"],
                threshold_cents=params["threshold_cents"],
                requires_all=params["requires_all"],
                note=params["note"],
            )
    return None


def needs_purpose(amount_cents: int, policy: PurposePolicy) -> bool:
    """这张发票按现行规则是否必须填写用途。"""
    if policy.requires_all:
        return True
    return amount_cents >= policy.threshold_cents


def explain_absence(conn: sqlite3.Connection, company_id: str, period: str) -> str:
    """没有现行规则时，说清为什么 —— 这比只说"不需要"有用得多。"""
    rows = conn.execute(
        "SELECT doc_id, version, title, status, effective_from FROM rule_doc"
        " WHERE doc_id IN (?, ?) AND (scope_company IS NULL OR scope_company = ?)"
        " ORDER BY doc_id, version",
        (*PURPOSE_DOC_IDS, company_id),
    ).fetchall()
    status_zh = {"active": "启用", "deprecated": "已停用"}
    parts = [
        f"{r['title']} {r['version']} 版（{status_zh.get(r['status'], r['status'])}，"
        f"生效日 {r['effective_from']}）"
        for r in rows
    ]
    return (
        f"{period} 没有现行用途规则：" + "；".join(parts) + "。"
        if parts
        else f"{period} 没有适用于该企业的用途规则。"
    )


def yuan(cents: int) -> str:
    """整数分 -> '9622.06'。绝不经过 float，避免 9622.060000000001 这种尾差。"""
    return str((Decimal(cents) / 100).quantize(Decimal("0.01")))
