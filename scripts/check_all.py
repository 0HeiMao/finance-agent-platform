"""一键自检：源文件编码/语法体检 + 后端单元测试 + 前端单元测试（可选：真机页面扫描）。

为什么是这个形态：门禁分两层 —— `.github/workflows/ci.yml` 在 push / PR 时跑「不需要演示数据、
不需要向量模型、不需要模型 Key」的那部分（编码/语法体检 + 后端 pytest + 前端 `npm ci`/`npm test`/
`npm run build`）；而这台开发机上没有 CI 服务可挂，所以同一套东西还需要一个谁都能在本机跑的入口，
就是这个脚本（`--quick` 由 `.githooks/pre-commit` 在每次提交前跑，见 `scripts/init_git.py`）。
它同时兜住一类真实事故 —— 用 PowerShell 的
`Get-Content` / `Set-Content -Encoding utf8` 往返 UTF-8 源文件，会按系统 ANSI 代码页
（本机 cp936）重写内容，结果文件开头多出 BOM、正文变成 mojibake（`涓嶄緷璧栨暟鎹簱` 这种），
python 直接 `SyntaxError: invalid non-printable character U+FEFF`。所以第一步永远是编码体检。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\check_all.py              # 体检 + 后端 + 前端（两边都含覆盖率门槛）
    .\\.venv\\Scripts\\python.exe scripts\\check_all.py --quick      # 只体检编码与语法
    .\\.venv\\Scripts\\python.exe scripts\\check_all.py --with-pages # 再加 17 页 × 3 档扫描
      （--with-pages 需要后端 127.0.0.1:8000 与前端 127.0.0.1:5173 已启动）

后端/前端两步分别委托给 `scripts/check_coverage.py` 与 `scripts/check_frontend_coverage.py`：
覆盖率只有在整套用例都跑完时才有意义，所以阈值判定不放 `pytest.ini`（理由见 README「覆盖率门槛」）。
"""
from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 体检范围：真正会被人手改的源码与文档；data/ 下的模型词表、output/ 下的截图不看。
SCAN_DIRS = ("backend", "scripts", "tests", "frontend/src", "frontend/tests", "docs")
SCAN_FILES = ("README.md", "AGENTS.md", "requirements.txt", "docker-compose.yml", "install.ps1")
TEXT_SUFFIXES = {".py", ".js", ".mjs", ".vue", ".md", ".txt", ".json", ".ps1", ".yml", ".yaml",
                 ".css", ".html", ".ini", ".cfg", ".toml", ".sh"}
BOM = b"\xef\xbb\xbf"
# U+FFFD：解码失败留下的替换字符，是 mojibake 最可靠的信号。
REPLACEMENT_CHAR = "\ufffd"


class Step:
    """一个自检步骤的结果，用来最后汇总。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "FAIL"
        self.detail = ""

    def pass_(self, detail: str) -> None:
        self.status, self.detail = "PASS", detail

    def skip(self, detail: str) -> None:
        self.status, self.detail = "SKIP", detail


def _console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _iter_sources():
    for directory in SCAN_DIRS:
        root = PROJECT_ROOT / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            # node_modules 之类的第三方代码不体检（本项目其实没装，防患于未然）。
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES and "node_modules" not in path.parts:
                yield path
    for name in SCAN_FILES:
        path = PROJECT_ROOT / name
        if path.exists():
            yield path


def check_encoding() -> Step:
    """查 BOM、非 UTF-8 字节、替换字符 —— 就是那场事故的三个指纹。"""
    step = Step("源文件编码体检（BOM / UTF-8 / 替换字符）")
    problems: list[str] = []
    scanned = 0
    for path in _iter_sources():
        scanned += 1
        relative = path.relative_to(PROJECT_ROOT)
        raw = path.read_bytes()
        if raw.startswith(BOM):
            problems.append(f"{relative}: 开头有 UTF-8 BOM")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append(f"{relative}: 不是合法 UTF-8（{exc.start} 字节处：{exc.reason}）")
            continue
        if REPLACEMENT_CHAR in text:
            line = text[:text.index(REPLACEMENT_CHAR)].count("\n") + 1
            problems.append(f"{relative}:{line}: 含替换字符 U+FFFD（像是被按 ANSI 代码页重写过）")
    if problems:
        step.detail = f"{len(problems)} / {scanned} 个文件有问题\n" + "\n".join(f"    {p}" for p in problems)
        return step
    step.pass_(f"{scanned} 个文件全部是干净的 UTF-8，无 BOM、无替换字符")
    return step


def check_python_syntax() -> Step:
    step = Step("Python 语法体检（ast.parse）")
    problems: list[str] = []
    scanned = 0
    for directory in ("backend", "scripts", "tests"):
        for path in sorted((PROJECT_ROOT / directory).rglob("*.py")):
            scanned += 1
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError) as exc:
                problems.append(f"{path.relative_to(PROJECT_ROOT)}: {exc}")
    if problems:
        step.detail = f"{len(problems)} / {scanned} 个文件解析失败\n" + "\n".join(f"    {p}" for p in problems)
        return step
    step.pass_(f"{scanned} 个 .py 文件全部能解析")
    return step


def _venv_python() -> str:
    candidate = PROJECT_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python.exe"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def _tail(text: str, lines: int = 15) -> str:
    stripped = [line for line in text.strip().splitlines() if line.strip()]
    return "\n".join(f"    {line}" for line in stripped[-lines:])


def _run(command: list[str], cwd: Path) -> tuple[int, str]:
    completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _final_line(output: str, prefix: str) -> str:
    """取门槛脚本最后那行小结（`覆盖率门槛通过：语句 …`）。比数点更可读，也不怕多一层 -q。"""
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith(prefix):
            return line
    return ""


def run_pytest() -> Step:
    step = Step("后端单元测试 + 覆盖率门槛（scripts/check_coverage.py）")
    # 走门槛脚本而不是直接 `pytest -q`：覆盖率阈值只能由「跑完整套用例」的运行来判，
    # 所以这一步的入口就是那个不接受过滤器的脚本（理由见 README 的「覆盖率门槛」一节）。
    code, output = _run([_venv_python(), str(PROJECT_ROOT / "scripts" / "check_coverage.py")], PROJECT_ROOT)
    if code != 0:
        step.detail = f"退出码 {code}\n{_tail(output, 20)}"
        return step
    step.pass_(_final_line(output, "覆盖率门槛通过") or "退出码 0")
    return step


def run_frontend_tests() -> Step:
    step = Step("前端单元测试 + 覆盖率门槛（scripts/check_frontend_coverage.py）")
    if shutil.which("node") is None:
        step.skip("本机没有 node，跳过")
        return step
    code, output = _run([_venv_python(), str(PROJECT_ROOT / "scripts" / "check_frontend_coverage.py")], PROJECT_ROOT)
    if code != 0:
        step.detail = f"退出码 {code}\n{_tail(output, 20)}"
        return step
    step.pass_(_final_line(output, "前端覆盖率门槛通过") or "退出码 0")
    return step


def run_pages() -> Step:
    step = Step("真机页面扫描（17 页 × 3 档）")
    code, output = _run([_venv_python(), str(PROJECT_ROOT / "scripts" / "check_pages.py")], PROJECT_ROOT)
    if code != 0:
        step.detail = "有页面横向溢出或服务未启动\n" + _tail(output, 25)
        return step
    groups = sum(1 for line in output.splitlines() if "无横向溢出" in line)
    step.pass_(f"退出码 0（{groups} 组无横向溢出，截图在 output/charts/）")
    return step


def main() -> int:
    _console()
    parser = argparse.ArgumentParser(description="一键自检：编码/语法 + 后端测试 + 前端测试")
    parser.add_argument("--quick", action="store_true", help="只做编码与语法体检")
    parser.add_argument("--with-pages", action="store_true", help="追加真机页面扫描（需前后端已启动）")
    args = parser.parse_args()

    print("一键自检（项目根目录：%s）" % PROJECT_ROOT)
    steps = [check_encoding(), check_python_syntax()]
    if not args.quick:
        steps.append(run_pytest())
        steps.append(run_frontend_tests())
    if args.with_pages:
        steps.append(run_pages())

    print("\n结果汇总")
    for step in steps:
        print(f"[{step.status}] {step.name} —— {step.detail}")

    failed = [step for step in steps if step.status == "FAIL"]
    if failed:
        print(f"\n自检未通过：{len(failed)} 步失败（{'、'.join(step.name for step in failed)}）")
        return 1
    skipped = [step for step in steps if step.status == "SKIP"]
    print(f"\n自检通过（{len(steps) - len(skipped)} 步通过"
          + (f"，{len(skipped)} 步跳过" if skipped else "") + "）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
