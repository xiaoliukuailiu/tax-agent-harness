"""集中配置：路径、模型接入、上下文预算。

所有值都可以用环境变量覆盖，方便评委 clone 后只改 .env 就跑起来。
   HARNESS_DB              数据库路径（默认 data/harness.db）
   HARNESS_MODEL           模型名
   HARNESS_BASE_URL        OpenAI 兼容接口地址
   HARNESS_API_KEY         API Key（不要提交到仓库）
   HARNESS_CONTEXT_BUDGET  单轮上下文 token 预算（默认 8000）
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 允许用 .env 配置模型（未安装 python-dotenv 时静默跳过，仍可用系统环境变量）
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except Exception:  # pragma: no cover
    pass

DATA_DIR = ROOT / "data"
DEFAULT_DB = DATA_DIR / "harness.db"

SEED_XLSX = (
    DATA_DIR
    / "official"
    / "赛题二：财税Harness初赛材料包-v1.1.0"
    / "02_示例数据.xlsx"
)

# 初赛只检查 2026-09 的发票用途（《03_业务规则》第 3 行）
CHECK_PERIOD = "2026-09"

CONTEXT_BUDGET = int(os.environ.get("HARNESS_CONTEXT_BUDGET", "8000"))
MAX_MEMORY_IN_CONTEXT = 5
RECENT_TURNS = 6


def db_path() -> Path:
    """每次调用都读环境变量，测试可以临时指向副本库。"""
    return Path(os.environ.get("HARNESS_DB") or DEFAULT_DB)


def model_name() -> str:
    return os.environ.get("HARNESS_MODEL", "qwen-plus")


def base_url() -> str | None:
    return os.environ.get("HARNESS_BASE_URL") or os.environ.get("OPENAI_BASE_URL")


def api_key() -> str | None:
    return os.environ.get("HARNESS_API_KEY") or os.environ.get("OPENAI_API_KEY")


def has_model() -> bool:
    return bool(base_url() and api_key())
