"""防再犯：扫描所有 git 跟踪的文件，出现疑似真实 API Key 即失败。

起因：曾把真实 DeepSeek Key 写进 .env.example 并推送到公开仓库。
.env 被 .gitignore 忽略不会进 ls-files；这里只扫真正会进仓库的文件。
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# DeepSeek/OpenAI 的 Key 形如 sk- 后跟 30+ 位字母数字；占位符 sk-your-key-here
# 含连字符，匹配不到 {20,}，所以不会误报。
KEY_RE = re.compile(r"sk-[A-Za-z0-9]{20,}")


def test_no_real_api_key_in_tracked_files():
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.splitlines()

    offenders: list[str] = []
    for rel in tracked:
        p = ROOT / rel
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in KEY_RE.finditer(text):
            masked = f"{m.group(0)[:6]}…{m.group(0)[-4:]}"
            offenders.append(f"{rel}: {masked}")

    assert not offenders, "发现疑似真实 API Key（已打码）：\n" + "\n".join(offenders)
