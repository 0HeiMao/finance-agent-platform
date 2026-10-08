"""⑤ 持续集成 —— 工作流自检（本地与 CI 里跑的都是这一个脚本）。

跑法（不需要数据库、不需要起后端、不需要模型）：
    .\\.venv\\Scripts\\python.exe scripts\\verify_ci_workflow.py

为什么需要它：CI 配置文件本身没有测试，而它「绿」不等于它真的在测东西。
最危险的一种假绿是**测试全 skip**：`tests/conftest.py` 连不上数据库就 `pytest.skip`，
所以「忘记设 `DATABASE_URL`」会得到一条漂亮的绿色流水线，实际上一个问题都没验。
镜像 tag 漂了、脚本改名了、`continue-on-error` 加上去了，同样是「CI 绿、出事在真机上」。

这里卡住的不变量（每条都对应一种「绿得没有意义」）：

1. 工作流是合法 YAML、有 `backend` / `frontend` 两个 job，且没有 `continue-on-error` / `if: false`
   之类把失败吞掉的东西。
2. `backend` 的 postgres 镜像 tag 与 `docker-compose.yml` 一致，且带 `pg_isready` 健康检查：
   镜像换成普通 postgres 会在建 `document_chunks` 时报 `type "vector" does not exist`；
   没有健康检查则测试可能在库还没起来时开跑 —— 又是一种全 skip 的假绿。
3. `DATABASE_URL` / `AUTH_SECRET_KEY` 必须显式给，且**变量名在 `backend/config.py` 里真实存在**
   （写错一个字母就静默变成默认值）；密钥不许是仓库里的占位值。
4. 工作流引用到的脚本/文件都真实存在（否则 CI 在跑一个不存在的路径，报错要到远端才看见）。
5. 后端测试步骤必须走 `scripts/check_coverage.py`（覆盖率门槛），而这个门槛脚本内部必须是
   `python -m pytest`（`-m` 才会把项目根放进 `sys.path`，裸 `pytest` 在 runner 上会
   `ModuleNotFoundError: backend`）。
6. 前端用 `npm ci`（按 lockfile 精确还原）并跑 `npm test`、`scripts/check_frontend_coverage.py`
   （前端覆盖率门槛）与 `npm run build`。
7. 自检脚本自己必须出现在工作流里 —— 否则这份自检会悄悄变成「没人跑的死代码」。
8. 需要演示数据 / 向量模型 / 模型 Key 的真机脚本（`check_pages.py`、`ui_components_smoke.py`、
   `scripts/verify_*.py`）**不许**进流水线：它们只会长期红着，比不跑更糟。这条边界同时要求
   README 的「持续集成」一节写明（脚本顺手核对，免得文档和流水线各说各话）。
9. 两个门槛脚本里的阈值必须在 README 的「覆盖率门槛」一节里出现 —— 阈值和文档分开维护时，
   下一个人就会用「把阈值调低」代替「补用例」。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# `python scripts/xxx.py` 只会把 `scripts/` 放进 sys.path（不是当前目录），而 CI 里就是这么调用的 ——
# 少了这一行，「环境变量名与 backend/config.py 对齐」这条会在 runner 上退化成
# `ModuleNotFoundError: No module named 'backend'`（本地手跑时通常带着 PYTHONPATH，看不出来）。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
COMPOSE_PATH = PROJECT_ROOT / "docker-compose.yml"
README_PATH = PROJECT_ROOT / "README.md"

PLACEHOLDER_SECRET = "change-this-secret-in-env"
SELF_NAME = Path(__file__).name
# runner 自带、不属于 backend/config.py 的变量。写错名字的 config 变量不在这里，所以查得出来。
ALLOWED_EXTRA_ENV = {"PYTHONIOENCODING", "PYTHONUNBUFFERED", "PYTHONPATH", "GITHUB_TOKEN"}
# 有意不进 CI 的真机脚本：要演示库 / 470MB 向量模型 / 真模型 API Key。
LIVE_ONLY_SCRIPTS = ("check_pages.py", "ui_components_smoke.py")
PATH_RE = re.compile(r"(?:frontend/|scripts/)?[\w][\w./-]*\.(?:py|txt|json|ini|yml|yaml|lock)")
README_CI_HEADING = "## 持续集成"
# 两个覆盖率门槛脚本（⑤ 的 CI 与 ⑥ 的阈值）：CI 必须调用它们，README 必须写明它们的阈值。
COVERAGE_GATE = "check_coverage.py"
FRONTEND_COVERAGE_GATE = "check_frontend_coverage.py"
README_COVERAGE_HEADING = "### 覆盖率门槛"
THRESHOLD_RE = re.compile(r"^(?:STATEMENT|BRANCH|LINE)_THRESHOLD\s*=\s*([\d.]+)", re.MULTILINE)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def load_yaml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 工作流结构

def structure() -> dict:
    """工作流的骨架 + 收齐所有 `run:` 命令，后面几步都吃这一份数据。"""
    if not WORKFLOW_PATH.exists():
        check("工作流文件存在（.github/workflows/ci.yml）", False, str(WORKFLOW_PATH))
        return {}
    try:
        workflow = load_yaml(WORKFLOW_PATH)
    except Exception as exc:  # noqa: BLE001 - 解析失败要一句话说清哪儿坏
        check("工作流是合法 YAML", False, f"{type(exc).__name__}: {exc}")
        return {}
    check("工作流是合法 YAML", isinstance(workflow, dict), str(WORKFLOW_PATH.relative_to(PROJECT_ROOT)))

    jobs = workflow.get("jobs") or {}
    check("声明了 backend 与 frontend 两个 job", {"backend", "frontend"} <= set(jobs), ", ".join(jobs))
    # YAML 会把裸 `on:` 读成布尔 True，所以两种键都要认。
    triggers = workflow.get("on", workflow.get(True))
    check("有触发条件（push / pull_request）",
          triggers is not None and ("pull_request" in triggers or "push" in triggers), str(triggers))

    steps = [step for job in jobs.values() for step in (job.get("steps") or [])]
    runs = [step["run"] for step in steps if "run" in step]
    swallowed = [step for step in steps if step.get("continue-on-error") or step.get("if") == False]  # noqa: E712
    check("没有 continue-on-error / if: false（不吞失败）", not swallowed,
          "、".join(str(step.get("name") or step.get("uses")) for step in swallowed))
    check("所有 run 步骤里没有 `|| true` 之类的兜底", not any(re.search(r"\|\|\s*true", run) for run in runs))
    return {"workflow": workflow, "jobs": jobs, "steps": steps, "runs": runs,
            "backend": jobs.get("backend") or {}, "frontend": jobs.get("frontend") or {}}


def job_runs(job: dict) -> list[str]:
    return [step["run"] for step in (job.get("steps") or []) if "run" in step]


def job_env(workflow: dict, job: dict) -> dict:
    merged = dict(workflow.get("env") or {})
    merged.update(job.get("env") or {})
    for step in job.get("steps") or []:
        merged.update(step.get("env") or {})
    return {str(key): value for key, value in merged.items()}


# ------------------------------------------------------------------ 数据库与镜像

def database_service(workflow: dict, backend: dict) -> None:
    """CI 的库必须与真机同一镜像，并且要等它真的能连上再跑测试。"""
    services = (backend.get("services") or {}).get("postgres") or {}
    image = str(services.get("image") or "")
    compose = load_yaml(COMPOSE_PATH).get("services", {}).get("postgres", {})
    compose_image = str(compose.get("image") or "")
    check("CI 的 postgres 镜像与 docker-compose.yml 一致", image == compose_image and bool(image),
          f"CI={image or '（没有 services.postgres）'} / compose={compose_image}")
    check("镜像带 pgvector（普通 postgres 建不出 document_chunks）", "pgvector" in image, image)
    options = str(services.get("options") or "")
    check("服务有健康检查（pg_isready）", "pg_isready" in options, options or "（没有 options）")

    env = job_env(workflow, backend)
    database_url = str(env.get("DATABASE_URL") or "")
    check("backend job 设了 DATABASE_URL（否则测试全 skip = 假绿）", bool(database_url), database_url)
    check("DATABASE_URL 指向 runner 本机的服务端口",
          "127.0.0.1:5432" in database_url or "localhost:5432" in database_url, database_url)
    secret = str(env.get("AUTH_SECRET_KEY") or "")
    check("AUTH_SECRET_KEY 已设且不是仓库占位值", bool(secret) and secret != PLACEHOLDER_SECRET,
          "（没设的话配置层会往工作区写 .env）" if not secret else "已设")
    check("PYTHONIOENCODING=utf-8（中文输出不会炸在编码上）",
          str(env.get("PYTHONIOENCODING") or "").lower() == "utf-8", str(env.get("PYTHONIOENCODING")))


def env_names(workflow: dict, jobs: dict) -> None:
    """环境变量名必须真实存在：写错一个字母等于用一个默认值跑完整条流水线。"""
    try:
        from backend.config import Settings
    except Exception as exc:  # noqa: BLE001
        check("环境变量名与 backend/config.py 对齐", False, f"无法导入 backend.config：{exc}")
        return
    known = {name.upper() for name in Settings.model_fields} | ALLOWED_EXTRA_ENV
    used: dict[str, str] = {}
    for key, value in (workflow.get("env") or {}).items():
        used[str(key)] = str(value)
    for job_name, job in jobs.items():
        for key, value in job_env(workflow, job).items():
            used.setdefault(key, str(value))
        for step in job.get("steps") or []:
            for entry in re.findall(r"\$\{\{\s*env\.(\w+)\s*\}\}", str(step.get("run") or "")):
                used.setdefault(entry, "(只在 run 里引用)")
    unknown = sorted(name for name in used if name not in known)
    check("环境变量名与 backend/config.py 对齐", not unknown,
          f"未识别的变量：{'、'.join(unknown)}" if unknown else f"共 {len(used)} 个变量")


def referenced_paths(data: dict) -> None:
    """`run:` 里点名的文件必须存在，脚本改名时这里先红，而不是 CI 上才红。"""
    missing: list[str] = []
    seen: set[str] = set()
    for run in data["runs"]:
        # 先把 URL 抠掉：`https://download.pytorch.org/whl/cpu` 会被路径正则读成 `download.py`。
        command = re.sub(r"https?://\S+", " ", run)
        for token in PATH_RE.findall(command):
            if token in seen:
                continue
            seen.add(token)
            if not (PROJECT_ROOT / token).exists():
                missing.append(token)
    check("工作流引用到的文件都存在", not missing, "、".join(missing) or f"检查了 {len(seen)} 个路径")


# ------------------------------------------------------------------ 具体门禁命令

def commands(data: dict) -> None:
    backend_runs = job_runs(data["backend"])
    gate = next((run.strip() for run in backend_runs if "check_all.py" in run and "--quick" in run), "")
    check("后端 job 跑了结构门禁 check_all.py --quick", bool(gate), gate or "（没有这一步）")
    check("自检脚本自己也被 CI 调用", any(SELF_NAME in run for run in backend_runs))
    coverage_gate = next((run.strip() for run in backend_runs if COVERAGE_GATE in run), "")
    check("后端 job 跑了覆盖率门槛（它就是 pytest 全量）", bool(coverage_gate),
          coverage_gate or "（没有这一步：覆盖率没有门槛，只有一份没人看的报告）")
    check("CI 里没有裸 pytest 调用（裸 pytest 在 runner 上 import 不到 backend）",
          not any(re.match(r"\s*pytest\b", run) for run in backend_runs))
    gate_script = PROJECT_ROOT / "scripts" / COVERAGE_GATE
    check(f"{COVERAGE_GATE} 自己用 `python -m pytest` 跑全量",
          gate_script.exists() and re.search(r'"-m",\s*"pytest"', gate_script.read_text(encoding="utf-8")) is not None,
          "门槛脚本里应能看到 `\"-m\", \"pytest\"`")
    check("装了 CPU 版 torch（Linux 上默认会拉 2.5GB 的 CUDA 版）",
          any("download.pytorch.org/whl/cpu" in run for run in backend_runs))
    check("跑 pytest 前先装 requirements.txt",
          any("requirements.txt" in run for run in backend_runs))
    for action in ("checkout", "setup-python", "setup-node"):
        uses = [str(step.get("uses") or "") for step in data["steps"]]
        pinned = [item for item in uses if item.startswith(f"actions/{action}@")]
        check(f"actions/{action} 被固定到版本 tag（不是 @main）",
              bool(pinned) and all(re.match(r"^actions/[\w-]+@v\d+(\.\d+)*$", item) for item in pinned),
              "、".join(pinned) or "（没用到）")

    frontend_runs = job_runs(data["frontend"])
    check("前端用 npm ci（按 lockfile 精确还原）",
          any(re.match(r"\s*npm ci\b", run) for run in frontend_runs)
          and not any(re.match(r"\s*npm install\b", run) for run in frontend_runs))
    check("前端跑了 npm test 与 npm run build",
          any(re.match(r"\s*npm test\b", run) for run in frontend_runs)
          and any(re.match(r"\s*npm run build\b", run) for run in frontend_runs),
          " / ".join(run.strip() for run in frontend_runs))
    frontend_gate = next((run.strip() for run in frontend_runs if FRONTEND_COVERAGE_GATE in run), "")
    check("前端 job 跑了覆盖率门槛", bool(frontend_gate),
          frontend_gate or "（没有这一步：覆盖率没有门槛，只有一份没人看的报告）")
    check("前端 lockfile 已入库（npm ci 要求它存在）",
          (PROJECT_ROOT / "frontend" / "package-lock.json").exists())


# ------------------------------------------------------------------ 「不进 CI」的边界

def live_only_scripts(data: dict) -> None:
    """真机脚本不进流水线，但必须在 README 里写明为什么 —— 否则下一个人会把它加回来。"""
    offenders: list[str] = []
    for run in data["runs"]:
        for name in LIVE_ONLY_SCRIPTS:
            if name in run:
                offenders.append(name)
        for token in re.findall(r"scripts/verify_[\w-]+\.py", run):
            if Path(token).name != SELF_NAME:
                offenders.append(token)
    check("需要演示数据/模型 Key 的真机脚本没有进 CI", not offenders,
          "、".join(sorted(set(offenders))))

    text = README_PATH.read_text(encoding="utf-8")
    index = text.find(README_CI_HEADING)
    if index < 0:
        check(f"README 有「{README_CI_HEADING}」一节", False, "没有这一节，CI 的边界没有写下来")
        return
    rest = text[index + len(README_CI_HEADING):]
    end = rest.find("\n## ")
    section = rest if end < 0 else rest[:end]
    check("README 的 CI 一节写明了哪些门禁只在本地跑",
          all(name in section for name in LIVE_ONLY_SCRIPTS),
          f"该节 {len(section)} 字符")
    check("README 的 CI 一节说清了「测试连不上库会 skip」这个坑",
          "skip" in section, "")


def coverage_docs() -> None:
    """两个门槛脚本的阈值必须在 README 里对得上 —— 数字只写在一个地方，另一处对着抄。"""
    if not README_PATH.exists():
        check("README 存在", False, str(README_PATH))
        return
    text = README_PATH.read_text(encoding="utf-8")
    index = text.find(README_COVERAGE_HEADING)
    if index < 0:
        check(f"README 有「{README_COVERAGE_HEADING}」一节", False,
              "阈值与理由得写下来，不然下一个人只会把阈值调低")
        return
    rest = text[index + len(README_COVERAGE_HEADING):]
    end = rest.find("\n#")
    section = rest if end < 0 else rest[:end]
    check("README 的「覆盖率门槛」一节写明了两个门槛脚本",
          all(name in section for name in (COVERAGE_GATE, FRONTEND_COVERAGE_GATE)),
          f"该节 {len(section)} 字符")
    for script, label in ((COVERAGE_GATE, "后端"), (FRONTEND_COVERAGE_GATE, "前端")):
        path = PROJECT_ROOT / "scripts" / script
        if not path.exists():
            check(f"{script} 存在", False, "门槛脚本不在，阈值也就无从谈起")
            continue
        values = sorted(set(THRESHOLD_RE.findall(path.read_text(encoding="utf-8"))))
        check(f"README 写明{label}阈值（与 {script} 一致）",
              bool(values) and all(value in section for value in values),
              "、".join(f"{value}%" for value in values) or "这脚本里没读到阈值常量")


def main() -> int:
    print(f"工作流：{WORKFLOW_PATH.relative_to(PROJECT_ROOT)}", flush=True)
    data = structure()
    if data:
        database_service(data["workflow"], data["backend"])
        env_names(data["workflow"], data["jobs"])
        referenced_paths(data)
        commands(data)
        live_only_scripts(data)
    coverage_docs()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 项通过", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 脚本要一句话说清哪儿炸了
        print(f"\n异常终止：{type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
