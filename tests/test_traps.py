"""15 个必考陷阱的自动化回归测试。

每个用例对应《赛题二-资料包解析》第 7 节里的一条陷阱。
跑法： pytest -q
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from harness import config, db, memory, policy, style, tools
from harness.tools import Session

@pytest.fixture(scope="session")
def seed_db(tmp_path_factory):
    """整个会话只从 Excel 重建一次干净库，之后每个用例复制一份。

    优先从官方 Excel 重建（能验证导入逻辑本身）；若素材缺失或目录名被解压工具
    改写而读不到，则退回随包提供的 data/harness.db，保证测试照样能跑。
    """
    dst = tmp_path_factory.mktemp("seed") / "seed.db"
    env = dict(os.environ, HARNESS_DB=str(dst))
    result = subprocess.run(
        [sys.executable, str(config.ROOT / "scripts" / "import_seed.py")],
        capture_output=True,
        env=env,
    )
    if result.returncode != 0 or not dst.exists():
        shipped = config.ROOT / "data" / "harness.db"
        if not shipped.exists():
            raise RuntimeError(
                "既无法从官方 Excel 重建，也没有随包数据库 data/harness.db"
            )
        print("\n[warn] 无法从官方 Excel 重建，改用随包的 data/harness.db")
        shutil.copy(shipped, dst)
    return dst


@pytest.fixture()
def conn(tmp_path, monkeypatch, seed_db):
    """每个用例一份独立副本，互不干扰（不受 demo 改动影响）。"""
    dst = tmp_path / "harness.db"
    shutil.copy(seed_db, dst)
    monkeypatch.setenv("HARNESS_DB", str(dst))
    c = db.connect()
    yield c
    c.close()


@pytest.fixture()
def u001():
    return Session(user_id="U001", company_id="C001", task_id="T-U001")


@pytest.fixture()
def u002():
    return Session(user_id="U002", company_id="C001", task_id="T-U002")


# ---------------------------------------------------------------- 金额口径


def test_invoice_sum_matches_official(conn, u001):
    """发票金额 = 价税合计，9 月 10 张合计 9622.06。"""
    result = tools.query_invoices(u001, "2026-09", conn=conn)
    assert result["status"] == "success"
    assert result["count"] == 10
    assert result["total"] == "9622.06"


def test_bank_directions_not_netted(conn, u001):
    """收入与支出分别统计，不用收入减支出。"""
    result = tools.query_bank(u001, "2026-09", conn=conn)
    assert result["expense"] == "7180.74"
    assert result["income"] == "13000.50"


def test_empty_month_is_zero_with_note(conn, u001):
    """查询成功但无记录 → 0.00 并说明；不是错误也不是无权限。"""
    result = tools.query_invoices(u001, "2026-07", conn=conn)
    assert result["status"] == "success"
    assert result["total"] == "0.00"
    assert "没有发票记录" in result["note"]


def test_missing_period_asks_user(conn, u001):
    """没说月份必须先追问，不能默认当前月份。"""
    result = tools.query_invoices(u001, None, conn=conn)
    assert result["status"] == "need_info"
    assert "period" in result["missing"]


# ---------------------------------------------------------------- 权限


def test_no_permission_is_not_zero(conn):
    """U002 访问乙企业：无权限 ≠ 没有记录。"""
    guest = Session(user_id="U002", company_id="C002")
    result = tools.query_invoices(guest, "2026-09", conn=conn)
    assert result["status"] == "failed"
    assert result["error_code"] == "NO_PERMISSION"
    assert "0.00" not in result["detail"]


def test_view_only_cannot_save(conn, u002):
    """U002 对甲企业仅查看，不能保存。"""
    result = tools.save_invoice(u002, "F002", purpose="越权尝试", conn=conn)
    assert result["status"] == "failed"
    assert result["error_code"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------- 规则版本


def test_missing_purpose_c001_uses_v2(conn, u001):
    """甲企业 9 月按 2.0 版每张必填：缺 5 张。"""
    result = tools.missing_purpose(u001, "2026-09", conn=conn)
    assert result["status"] == "success"
    assert result["invoice_nos"] == ["F002", "F003", "F004", "F006", "F009"]
    assert "2.0 版" in result["basis"]


def test_missing_purpose_c002_threshold(conn):
    """乙企业按 ≥1000.00：F004(999.99) 不该报，F002(1000.00) 必须报。"""
    u = Session(user_id="U001", company_id="C002")
    result = tools.missing_purpose(u, "2026-09", conn=conn)
    assert result["invoice_nos"] == ["F002", "F005", "F006"]
    assert "F004" not in result["invoice_nos"]


def test_remark_does_not_replace_purpose(conn, u001):
    """C001 F006 有备注但用途为空，仍要上报。"""
    result = tools.missing_purpose(u001, "2026-09", conn=conn)
    assert "F006" in result["invoice_nos"]


def test_august_not_applicable(conn, u001):
    """8 月没有现行用途规则（旧版停用、新版 9 月才生效）。"""
    result = tools.missing_purpose(u001, "2026-08", conn=conn)
    assert result["status"] == "not_applicable"
    assert "2026-09" in result["note"]


def test_deprecated_rule_would_miss_four(conn, u001):
    """反证：按已停用的 1.0 版只报 1 张，说明版本选错会漏报 4 张。"""
    rows = tools.list_invoices(u001, "2026-09", conn=conn)["invoices"]
    old = [r["invoice_no"] for r in rows
           if not r["purpose"] and int(round(float(r["amount"]) * 100)) >= 100_000]
    new = tools.missing_purpose(u001, "2026-09", conn=conn)["invoice_nos"]
    assert len(old) == 1 and len(new) == 5


def test_policy_selection_by_effective_date(conn):
    """同一 doc_id 两个版本：9 月取 active 的 2.0，且不取已停用的 1.0。"""
    rule = policy.active_purpose_rule(conn, "C001", "2026-09")
    assert rule is not None and rule.version == "2.0"
    assert policy.active_purpose_rule(conn, "C001", "2026-08") is None


# ---------------------------------------------------------------- 写操作


def test_readonly_field_rejected(conn, u001):
    """金额等只读字段不允许改。"""
    result = tools.save_patch(u001, "F002", {"amount_cents": 50000}, conn=conn)
    assert result["status"] == "failed"
    assert result["error_code"] == "READONLY_FIELD"


def test_blank_purpose_rejected(conn, u001):
    """纯空格视为未填写，不能当作有效内容保存。"""
    result = tools.save_invoice(u001, "F002", purpose="   ", conn=conn)
    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_CONTENT"


def test_save_returns_db_value_not_patch(conn, u001):
    """保存后重新查询核对：返回库里的值，并带回执号。"""
    result = tools.save_invoice(u001, "F002", purpose="购买复印纸与打印服务", conn=conn)
    assert result["status"] == "success"
    assert result["receipt_id"].startswith("R-")
    assert result["saved"]["purpose"] == "购买复印纸与打印服务"
    assert result["verified"] is True


def test_idempotent_save_single_row(conn, u001):
    """同一修改说两次：只有一条成功记录，回执号相同。"""
    first = tools.save_invoice(u001, "F002", purpose="办公用品", conn=conn)
    second = tools.save_invoice(u001, "F002", purpose="办公用品", conn=conn)
    assert second["duplicate"] is True
    assert first["receipt_id"] == second["receipt_id"]

    rows = conn.execute(
        "SELECT count(*) c FROM tool_log WHERE status='success' AND idem_key IS NOT NULL"
        " AND tool_name='save_invoice' AND idem_key=?",
        (first["idempotency_key"],),
    ).fetchone()["c"]
    assert rows == 1


def test_restart_recovery_reads_receipt(conn, u001):
    """重启后先查幂等键：有回执就直接确认，不重复保存。"""
    saved = tools.save_invoice(u001, "F002", purpose="办公用品", conn=conn)
    fresh = db.connect()  # 模拟新进程
    recovered = tools.get_save_result(u001, saved["idempotency_key"], conn=fresh)
    assert recovered["status"] == "success"
    assert recovered["receipt_id"] == saved["receipt_id"]
    assert recovered["saved"]["purpose"] == "办公用品"
    fresh.close()


def test_same_invoice_no_across_companies(conn):
    """F001 在两家企业都有，企业 + 编号联合定位，不能串。"""
    a = tools.read_invoice(Session(user_id="U001", company_id="C001"), "F001", conn=conn)
    b = tools.read_invoice(Session(user_id="U001", company_id="C002"), "F001", conn=conn)
    assert a["amount"] != b["amount"]
    assert a["purpose"] != b["purpose"]


# ---------------------------------------------------------------- 记忆与任务


def test_one_open_task_per_user_company(conn):
    """每个用户每家企业最多一个未完成任务（部分唯一索引）。"""
    first = memory.ensure_task(conn, company_id="C001", user_id="U001")
    second = memory.ensure_task(conn, company_id="C001", user_id="U001")
    assert first["task_id"] == second["task_id"]


def test_memory_scoped_by_company(conn):
    """记忆按企业隔离，不跨企业混用。"""
    memory.add(conn, company_id="C001", user_id="U001", content="默认按办公费记")
    c001 = memory.list_active(conn, company_id="C001", user_id="U001")
    c002 = memory.list_active(conn, company_id="C002", user_id="U001")
    assert len(c001) == 1 and len(c002) == 0


def test_memory_correct_and_delete(conn):
    """记忆可更正、可删除（软删，审计留痕）。"""
    mid = memory.add(conn, company_id="C001", user_id="U001", content="默认办公费")
    assert memory.correct(conn, mid, "默认差旅费") is True
    assert memory.get(conn, mid)["content"] == "默认差旅费"
    assert memory.remove(conn, mid) is True
    assert memory.get(conn, mid)["status"] == "deleted"


# ---------------------------------------------------------------- 表达


def test_guard_blocks_fake_saved():
    """没有回执时不许说"已保存"。"""
    result = style.polish("已保存 F002，用途是办公费。", has_receipt=False)
    assert "已保存" not in result["text"]
    assert "无回执禁用完成语" in result["applied"]


def test_guard_allows_saved_with_receipt():
    """有回执时正常表达，不误伤。"""
    result = style.polish("已保存 F002，回执 R-0001。", has_receipt=True)
    assert "已保存" in result["text"]


def test_filler_removed_and_blacklist_empty():
    """套话被清掉，处理后黑名单命中数为 0。"""
    result = style.polish("好的，首先为您查询。2026-09 共 10 张发票。希望对您有帮助。")
    assert style.blacklist_hits(result["text"]) == []
    assert "希望" not in result["text"]
