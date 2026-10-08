"""⑥ 覆盖率门槛 —— 跑后端全量用例，卡住语句覆盖率与分支覆盖率。

跑法（需要真 PostgreSQL，同 `tests/conftest.py`；需要 `.venv` 里的 pytest-cov）：
    .\\.venv\\Scripts\\python.exe scripts\\check_coverage.py
    .\\.venv\\Scripts\\python.exe scripts\\check_coverage.py --report   # 只打印缺口，不判失败

为什么门槛不写在 `pytest.ini` 里：`--cov-fail-under` 一旦进了 `addopts`，
`pytest -k rollup` 这种「只跑一小部分用例定位问题」的运行会因为覆盖率天然很低而失败 ——
一个必然失败的过滤器等于没有过滤器。覆盖率只有在整套用例都跑完时才有意义，
所以这里是唯一的门槛入口，而且它不接受任何用例过滤器。

阈值取实测水位的下方一点（README 的表里有同一组数字）：
    语句 87.93% → 门槛 87.0%；分支 87.44% → 门槛 86.0%
没被覆盖的主要是「要真模型 / 真 API Key」的路径（`ai_service.py`、`llm.py`、`rag/answer.py`、
`rag/rerank.py`）与 `--cov=backend` 下从未被导入的文件。跑完的覆盖表会直接列出缺口行号
（`--cov-report=term-missing:skip-covered`，满覆盖的文件不打印）。

除了百分比，还卡两条「数字别是假的」：用例数必须 > 0，且**跳过数必须是 0** ——
`tests/conftest.py` 连不上库就 `pytest.skip`，那时覆盖率会掉下来；但更阴的是有人给一批用例
加上 skip 标记、总覆盖率仍然达标，这条会拦住「用跳过冒充通过」。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 实测水位：语句 87.93%、分支 87.44%（后端 173 个用例）。
STATEMENT_THRESHOLD = 87.0
BRANCH_THRESHOLD = 86.0

# `TOTAL   3628  438  900  113  86%`（带 --cov-branch 时是五列）
TOTAL_RE = re.compile(r"^TOTAL\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%", re.MULTILINE)
COUNT_RE = re.compile(r"(\d+) (passed|failed|skipped|error|errors|deselected)")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def _venv_python() -> str:
    candidate = PROJECT_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python.exe"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def run_suite() -> tuple[int, str]:
    """跑整套后端用例并采集覆盖率。这里刻意不支持任何过滤参数。

    注意别再补一个 `-q`：`pytest.ini` 的 addopts 里已经有一个，两个叠成 `-qq` 会把
    「173 passed in …」这行统计也一起吞掉，而这个脚本正要读它。
    """
    command = [
        _venv_python(), "-m", "pytest", "--color=no",
        "--cov=backend", "--cov-branch", "--cov-report=term-missing:skip-covered",
    ]
    print("运行：" + " ".join(command[1:]), flush=True)
    completed = subprocess.run(command, cwd=str(PROJECT_ROOT), capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def parse_total(output: str) -> tuple[int, int, int, int, int] | None:
    """从覆盖表里取 TOTAL 行：(语句数, 未覆盖语句, 分支数, 半覆盖分支, 报告里的合并百分比)。"""
    match = TOTAL_RE.search(output)
    if match is None:
        return None
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def parse_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in output.splitlines():
        if " passed" not in line and " failed" not in line and " skipped" not in line:
            continue
        for number, kind in COUNT_RE.findall(line):
            key = "error" if kind.startswith("error") else kind
            counts[key] = max(counts.get(key, 0), int(number))
    return counts


def _percent(covered: int, total: int) -> float:
    return 100.0 * covered / total if total else 100.0


def main() -> int:
    parser = argparse.ArgumentParser(description="后端覆盖率门槛（语句 + 分支）")
    parser.add_argument("--report", action="store_true", help="只打印覆盖率缺口，不用阈值判失败")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    code, output = run_suite()
    print(output, flush=True)

    if not args.report:
        check("后端用例全部通过", code == 0, f"pytest 退出码 {code}")

    counts = parse_counts(output)
    passed = counts.get("passed", 0)
    skipped = counts.get("skipped", 0)
    if not args.report:
        check("用例数不为 0（覆盖率不是空跑出来的）", passed > 0, f"{passed} passed")
        check("没有跳过用例（跳过会让覆盖率失真）", skipped == 0, f"{skipped} skipped")

    total = parse_total(output)
    if total is None:
        check("覆盖表里有 TOTAL 行", False, "没解析到；确认 pytest-cov 已装且 --cov=backend 生效")
        _print_summary(code, passed, skipped, None)
        return 1 if not args.report else 0

    statements, missed, branches, partial, reported = total
    statement_percent = _percent(statements - missed, statements)
    branch_percent = _percent(branches - partial, branches)
    detail = (f"语句 {statement_percent:.2f}%（{statements - missed}/{statements}）、"
              f"分支 {branch_percent:.2f}%（{branches - partial}/{branches}）、报告合并 {reported}%")
    if not args.report:
        check(f"语句覆盖率 ≥ {STATEMENT_THRESHOLD:.1f}%", statement_percent >= STATEMENT_THRESHOLD, detail)
        check(f"分支覆盖率 ≥ {BRANCH_THRESHOLD:.1f}%", branch_percent >= BRANCH_THRESHOLD, detail)

    failed = [name for name, ok, _ in RESULTS if not ok]
    if args.report:
        print(f"\n覆盖率报告（--report，不判失败）：{detail}")
        return 0
    if failed:
        print(f"\n覆盖率门槛未通过：{len(failed)} 项不达标（{'、'.join(failed)}）")
        print("补救：给新增/改动的代码补用例，或在 README 的「覆盖率门槛」一节里说明为什么该降低阈值。")
        return 1
    print(f"\n覆盖率门槛通过：{detail}、用例 {passed} 通过 / {skipped} 跳过")
    return 0


def _print_summary(code: int, passed: int, skipped: int, percent: str | None) -> None:
    print(f"\n覆盖率门槛未通过：pytest 退出码 {code}、{passed} passed / {skipped} skipped、{percent or '无覆盖率数据'}")


if __name__ == "__main__":
    raise SystemExit(main())
