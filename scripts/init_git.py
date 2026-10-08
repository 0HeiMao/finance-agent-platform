"""把本目录初始化成 git 仓库（可重复执行，新机器上重建也用这个脚本）。

做三件事：

1. `git init -b main`（已经是仓库就跳过）；
2. 把 `core.hooksPath` 指到**版本化**的 `.githooks/`——钩子放在 `.git/hooks/` 里不进版本库，
   换台机器就静默消失；放在 `.githooks/` 里则随仓库一起走，谁 clone 下来都看得到这道闸门；
3. 体检式打印：分支、待提交文件数、`.env` 是否确实被忽略（密钥文件进库比什么 bug 都严重）。

提交前钩子跑的是 `scripts/check_all.py --quick`（编码 + 语法 + 后端用例 + 前端用例），
所以第一次 `git commit` 会多几秒，这是有意的：这四件事先后都翻过车。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main() -> int:
    inside = git("rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0:
        created = git("init", "-b", "main")
        output = (created.stdout + created.stderr).strip()
        print(output or "git init 没有输出")
        if created.returncode != 0:
            return created.returncode
    else:
        print("已经是 git 仓库，跳过 git init")

    configured = git("config", "core.hooksPath", ".githooks")
    if configured.returncode != 0:
        print("设置 core.hooksPath 失败：", configured.stderr.strip(), file=sys.stderr)
        return configured.returncode

    # 行尾统一：仓库里的 .gitattributes 要求一律 LF，本地再把 autocrlf 关掉，
    # 否则每次 add 都刷一屏「LF will be replaced by CRLF」，而 CRLF 的 shell 钩子根本跑不起来。
    if git("config", "core.autocrlf", "false").returncode != 0:
        print("设置 core.autocrlf 失败", file=sys.stderr)
        return 1

    branch = git("branch", "--show-current").stdout.strip() or "(还没有提交)"
    pending = [line for line in git("status", "--porcelain").stdout.splitlines() if line.strip()]
    print(f"分支：{branch}")
    print(f"core.hooksPath：{git('config', 'core.hooksPath').stdout.strip()}")
    print(f"待提交条目：{len(pending)} 个文件/目录")

    ignored = git("check-ignore", "-q", ".env")
    if ignored.returncode == 0:
        print("密钥文件检查：.env 已被 .gitignore 忽略，不会进库")
    else:
        print("警告：.env 没有被忽略，先改 .gitignore 再提交", file=sys.stderr)
        return 1
    print("\n下一步：git add -A && git commit -m \"初始提交\"（提交前会自动跑 scripts/check_all.py --quick）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
