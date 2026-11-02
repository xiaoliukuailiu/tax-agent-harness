#!/usr/bin/env python
"""打包成可分发的 zip。

做三件事：
1. 清理运行期产物（__pycache__ / .pytest_cache / .pytest_tmp / -wal / -shm）
2. 从官方 Excel 重建一份干净的 data/harness.db（避免把演示时改过的数据打进去）
3. 按白名单收集文件，打成 dist/tax-agent-harness-vX.Y.Z.zip，并输出清单与 SHA256

用法：
    python scripts/package.py              # 打包
    python scripts/package.py --no-rebuild # 不重建数据库（沿用现有库）
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "tax-agent-harness"
VERSION = "0.1.0"
TOP = f"{NAME}-v{VERSION}"

# 打进包里的顶层条目（其余一律不进包）
INCLUDE = [
    "README.md",
    "requirements.txt",
    ".env.example",
    "pytest.ini",
    ".gitignore",
    "demo.py",
    "harness",
    "tests",
    "scripts",
    "docs",
    "data",
]

# 任何层级出现这些目录都跳过（不进包）
SKIP_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".pytest_tmp",
    ".git",
    ".workbuddy",  # 项目记忆/配置，只排除，绝不删除
    ".idea",
    ".vscode",
    "__MACOSX",  # macOS 压缩包解出来的垃圾目录
}

# 只有这些目录允许在打包前物理删除（纯缓存，可随时重建）
CLEAN_DIRS = {"__pycache__", ".pytest_cache", ".pytest_tmp"}

# 任何层级出现这些文件名都跳过（密钥与 SQLite 临时文件）
SKIP_FILES = {".env", "harness.db-wal", "harness.db-shm", ".DS_Store"}


def clean_runtime_artifacts() -> tuple[int, int]:
    """删掉缓存与临时文件，返回 (删掉的目录数, 删掉的文件数)。

    只删 CLEAN_DIRS 里的纯缓存目录；.workbuddy / .git / .vscode 之类只排除不删除。
    """
    official = ROOT / "data" / "official"
    dirs = files = 0
    for path in sorted(ROOT.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if official in path.parents or path == official:
            continue  # 官方原始材料保持原样，不删也不改
        if path.is_dir() and path.name in CLEAN_DIRS:
            shutil.rmtree(path, ignore_errors=True)
            dirs += 1
        elif path.is_file() and path.suffix == ".pyc":
            path.unlink(missing_ok=True)
            files += 1
        elif path.is_file() and path.name in {"harness.db-wal", "harness.db-shm"}:
            path.unlink(missing_ok=True)
            files += 1
    return dirs, files


def rebuild_db() -> None:
    """从官方 Excel 重建数据库，保证包里的初始数据是干净的。"""
    db_path = ROOT / "data" / "harness.db"
    env = dict(os.environ, HARNESS_DB=str(db_path))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "import_seed.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        sys.exit(f"[FAIL] 重建数据库失败：\n{result.stdout}\n{result.stderr}")
    for line in result.stdout.strip().splitlines():
        if "PASS" in line or "FAIL" in line:
            print("      " + line.strip())


def collect() -> list[tuple[Path, str]]:
    """返回 [(本地路径, 包内相对路径)]，已排序。"""
    items: list[tuple[Path, str]] = []
    for entry in INCLUDE:
        base = ROOT / entry
        if not base.exists():
            print(f"      [warn] 缺少 {entry}，跳过")
            continue
        if base.is_file():
            if base.name in SKIP_FILES:
                continue
            items.append((base, entry))
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(ROOT).parts
            if any(part in SKIP_DIRS for part in rel_parts):
                continue
            if path.name in SKIP_FILES or path.suffix == ".pyc":
                continue
            items.append((path, path.relative_to(ROOT).as_posix()))
    return items


def package_info(files: list[tuple[Path, str]], total_bytes: int) -> str:
    lines = [
        f"{NAME} v{VERSION}",
        "",
        f"打包时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"文件数：{len(files)}",
        f"解压后大小：{total_bytes / 1024:.1f} KB",
        "",
        "快速开始：",
        "  pip install -r requirements.txt",
        "  python demo.py     # 一键演示，不需要模型 Key",
        "  pytest -q          # 24 条陷阱回归测试",
        "",
        "说明：",
        "  data/harness.db 已从 data/official 的官方 Excel 重建，为初始数据。",
        "  若需重新生成：python scripts/import_seed.py",
        "  本包不含 .env，接入模型请参照 .env.example 自行创建。",
    ]
    return "\n".join(lines) + "\n"


def build(items: list[tuple[Path, str]], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{TOP}.zip"
    total = sum(p.stat().st_size for p, _ in items)
    info = package_info(items, total).encode("utf-8")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, rel in items:
            zf.write(path, arcname=f"{TOP}/{rel}")
        zf.writestr(f"{TOP}/PACKAGE_INFO.txt", info)
    return zip_path


def describe(items: list[tuple[Path, str]], zip_path: Path) -> None:
    total = sum(p.stat().st_size for p, _ in items)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()

    buckets: dict[str, list[int]] = {}
    for path, rel in items:
        buckets.setdefault(rel.split("/")[0], []).append(path.stat().st_size)

    print("\n包内容：")
    for key in sorted(buckets):
        sizes = buckets[key]
        print(f"  {key:<14} {len(sizes):>3} 个文件  {sum(sizes) / 1024:>7.1f} KB")

    print(f"\n  合计 {len(items)} 个文件，解压后 {total / 1024:.1f} KB")
    print(f"  压缩包 {zip_path.name}  {zip_path.stat().st_size / 1024:.1f} KB")
    print(f"  SHA256 {digest}")
    print(f"  路径   {zip_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="打包财税 Agent Harness")
    parser.add_argument("--no-rebuild", action="store_true", help="不重建数据库")
    parser.add_argument("--out", default="dist", help="输出目录（默认 dist）")
    args = parser.parse_args()

    print("[1/4] 清理运行期产物")
    dirs, files = clean_runtime_artifacts()
    print(f"      删除 {dirs} 个缓存目录、{files} 个临时文件")

    print("[2/4] 重建数据库")
    if args.no_rebuild:
        print("      已跳过（--no-rebuild）")
    else:
        rebuild_db()

    print("[3/4] 收集文件")
    items = collect()
    print(f"      {len(items)} 个文件")

    print("[4/4] 生成压缩包")
    zip_path = build(items, ROOT / args.out)
    describe(items, zip_path)


if __name__ == "__main__":
    main()
