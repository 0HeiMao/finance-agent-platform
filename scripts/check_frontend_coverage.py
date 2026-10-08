"""⑥ 覆盖率门槛（前端）—— 用 node 自带的覆盖率跑前端全量用例，卡住平均覆盖率。

跑法（在项目根，需要 node；不需要后端与浏览器）：
    .\\.venv\\Scripts\\python.exe scripts\\check_frontend_coverage.py
    .\\.venv\\Scripts\\python.exe scripts\\check_frontend_coverage.py --report   # 只打印，不判失败

为什么自己解析 node 的覆盖表，而不是用 `--test-coverage-lines` 之类的参数：那些阈值参数要
node 22+，而 CI 用的是 node 20（`--experimental-test-coverage` 才有）。

门槛算的是 `COVERED_MODULES` 这 12 个模块的**平均**覆盖率，而不是报告里的 `all files` 行：
node 20 会把 `tests/*.test.mjs` 也算进 `all files`，node 22+ 又把测试文件排除掉 —— 同一份代码
在两个版本上给出两个总分（本机实测 97.95% / 97.00%）。固定名单的平均值与 node 版本无关。

先卡的其实是「名单里的模块必须出现在报告里」：node 的覆盖表只列**被加载过的**文件，
删掉一个用例会让对应模块直接从表里消失，而 `all files` 的百分比反而可能升高 ——
这是覆盖率门槛最容易被骗过去的地方。模块还在、百分比掉了，说明用例没删但没跑到。

阈值（本机实测：平均语句 96.06%、平均分支 88.37%）：
    平均语句 ≥ 95.0%、平均分支 ≥ 86.0%
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = PROJECT_ROOT / "frontend"
REGISTER = "./tests/support/register.mjs"

# 门槛的固定名单：这些模块必须有用例加载过。加新模块时同步加进这里（README 的「覆盖率门槛」一节有说明）。
COVERED_MODULES = (
    "src/api/client.js",
    "src/api/download.js",
    "src/api/scope.js",
    "src/api/session.js",
    "src/components/AgentChat.vue",
    "src/components/AppSidebar.vue",
    "src/components/DataScopeNotice.vue",
    "src/components/ExportMenu.vue",
    "src/components/PagePager.vue",
    "src/components/ScopeSwitcher.vue",
    "src/utils/format.js",
    "src/utils/markdown.js",
)

LINE_THRESHOLD = 95.0
BRANCH_THRESHOLD = 86.0

# `src/api/client.js  |  71.74 |    60.71 |   60.00 | 12-16 ...`
ROW_RE = re.compile(
    r"^(?P<name>[^|]+?)\s*\|\s*(?P<line>\d+\.\d+)\s*\|\s*(?P<branch>\d+\.\d+)\s*\|\s*(?P<funcs>\d+\.\d+)\s*\|"
)
NOISE_PREFIXES = ("---", "file ", "start of coverage", "end of coverage")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def strip_marker(line: str) -> str:
    """去掉 node 的 `ℹ `（node 24）或 `# `（node 20）行首标记，保留树形缩进。"""
    for marker in ("\u2139 ", "# "):
        if line.startswith(marker):
            return line[len(marker):]
    return line


def parse_coverage_table(text: str) -> dict[str, tuple[float, float, float]]:
    """把 node 的覆盖表解析成 {路径: (语句%, 分支%, 函数%)}，`all files` 也在里面。

    兼容两种格式：node 20 每行给完整相对路径（`src/api/client.js`）；
    node 22+ 改成树形（`src` / ` api` / `   client.js` 三行，靠缩进拼路径）。
    """
    rows: dict[str, tuple[float, float, float]] = {}
    stack: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = strip_marker(raw).strip("\r\n")
        if not line.strip() or line.lstrip().startswith(NOISE_PREFIXES):
            continue
        match = ROW_RE.match(line)
        name_cell = line.split("|")[0]
        name = (match.group("name") if match else name_cell).strip()
        depth = len(name_cell) - len(name_cell.lstrip())
        if match is None:
            if name:
                while stack and stack[-1][0] >= depth:  # 树形：同级/更深的分组先弹掉
                    stack.pop()
                stack.append((depth, name))
            continue
        if "/" in name:
            path = name
        else:
            path = "/".join(part for level, part in stack if level < depth)
            path = f"{path}/{name}" if path else name
        rows[path] = (float(match.group("line")), float(match.group("branch")), float(match.group("funcs")))
    return rows


def _node() -> str | None:
    return shutil.which("node")


def test_files() -> list[str]:
    return [f"tests/{path.name}" for path in sorted((FRONTEND / "tests").glob("*.test.mjs"))]


def run_suite() -> tuple[int, str]:
    node = _node()
    assert node is not None
    command = [node, "--test", "--experimental-test-coverage", "--import", REGISTER, *test_files()]
    print("运行：" + " ".join(["node", "--test", "--experimental-test-coverage", "…", *test_files()]), flush=True)
    # node 收集覆盖率时要在临时目录里 mkdtemp；本机沙箱不给写系统 %TEMP%，会得到
    # `Warning: Code coverage could not be enabled. Error: EPERM … mkdtemp` 并以退出码 1 结束
    # （用例其实全过）。把临时目录指到工作区里，本地与 CI 用同一套路径。
    tmp_root = PROJECT_ROOT / "output" / "node-coverage-tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    for name in ("TEMP", "TMP", "TMPDIR"):
        environment[name] = str(tmp_root)
    completed = subprocess.run(command, cwd=str(FRONTEND), capture_output=True, env=environment,
                               text=True, encoding="utf-8", errors="replace")
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="前端覆盖率门槛（固定名单的平均覆盖率）")
    parser.add_argument("--report", action="store_true", help="只打印覆盖率，不用阈值判失败")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if _node() is None:
        print("[SKIP] 本机没有 node，跳过前端覆盖率门槛")
        return 0

    code, output = run_suite()
    print(output, flush=True)
    rows = parse_coverage_table(output)

    if not args.report:
        check("前端用例全部通过", code == 0, f"node --test 退出码 {code}")

    missing = [module for module in COVERED_MODULES if module not in rows]
    if not args.report:
        check("名单里的模块都出现在覆盖表里（用例没被删掉）", not missing,
              "缺：" + "、".join(missing) if missing else f"{len(COVERED_MODULES)} 个模块")

    line_average = _average([rows[module][0] for module in COVERED_MODULES if module in rows])
    branch_average = _average([rows[module][1] for module in COVERED_MODULES if module in rows])
    detail = (f"平均语句 {line_average:.2f}%、平均分支 {branch_average:.2f}%"
              f"（{len(COVERED_MODULES) - len(missing)}/{len(COVERED_MODULES)} 个模块在报告里）")
    if not args.report:
        check(f"平均语句覆盖率 ≥ {LINE_THRESHOLD:.1f}%", line_average >= LINE_THRESHOLD, detail)
        check(f"平均分支覆盖率 ≥ {BRANCH_THRESHOLD:.1f}%", branch_average >= BRANCH_THRESHOLD, detail)

    failed = [name for name, ok, _ in RESULTS if not ok]
    if args.report:
        print(f"\n前端覆盖率报告（--report，不判失败）：{detail}")
        return 0
    if failed:
        print(f"\n前端覆盖率门槛未通过：{len(failed)} 项不达标（{'、'.join(failed)}）")
        print("补救：删掉用例就补回来；确实不再维护的模块，从 COVERED_MODULES 里移除并说明原因。")
        return 1
    print(f"\n前端覆盖率门槛通过：{detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
