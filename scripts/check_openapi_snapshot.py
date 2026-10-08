"""⑧ 接口契约快照 —— 把 FastAPI 自动生成的 OpenAPI 文档冻结进仓库，接口一改就必须显式更新。

跑法（不需要数据库、不需要起后端、不需要向量模型）：
    .\\.venv\\Scripts\\python.exe scripts\\check_openapi_snapshot.py            # 比对（check_all 与 CI 都会跑）
    .\\.venv\\Scripts\\python.exe scripts\\check_openapi_snapshot.py --update   # 接口确实改了：重新生成并一起提交

为什么需要它：`docs/api-contract.md` 是手写的散文契约，它能写得很细（先 409 再 400 的顺序、两处 429
的文案必须一字不差、`session_version` 为什么不叫 `token_version`），但**没有任何东西保证它和代码一致**：
删掉一个接口、把 `period` 从必填改成可选、给响应模型加一个字段，散文不会自己跟着变。FastAPI 生成的
OpenAPI 是这份契约的机器可读的一半 —— 把整份冻结成 `docs/openapi.json`，接口变化就变成提交时一次
显式的 diff 审查，而不是几个月后由前端发现。

它查四件事：
1. `docs/openapi.json` 已提交，且与**当前代码生成的**接口逐字节一致（`sort_keys` + `ensure_ascii=False`，
   所以键顺序稳定、中文说明可读 —— diff 里看到的就是真实变化，不是序列化抖动）。
2. 不一致时打印人话差异（新增/删除/改动的接口与模型），而不是丢一份 130 KB 的 JSON diff 让人自己看。
3. `docs/api-contract.md` 接口表里点名的每个「方法 + 路径」都真实存在 —— 散文契约同样不许漂。
4. 每个接口都带一句说明（`summary` / `description`）：契约里出现光秃秃的接口就是文档债。

接口改了怎么办：跑 `--update`，把 `docs/openapi.json` **和代码一起提交**，并在提交信息里点明改了哪些接口。
依赖升级（fastapi / pydantic / starlette）也可能让这份快照合法地变化（生成器写进 OpenAPI 的细节），
同样按「跑 --update 后 review diff」处理，而不是把这项检查关掉。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# `python scripts/xxx.py` 只把 `scripts/` 放进 sys.path（不是当前目录），而 CI 与 check_all 都是这么调的；
# 少了这一行，`from backend.main import app` 在 runner 上会 ModuleNotFoundError。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SNAPSHOT_PATH = PROJECT_ROOT / "docs" / "openapi.json"
CONTRACT_PATH = PROJECT_ROOT / "docs" / "api-contract.md"
API_PREFIX = "/api/v1"
# 契约定里表格行形如：| POST | `/financial/import` | 说明 |（路径带反引号、可能带查询串）
CONTRACT_ROW_RE = re.compile(r"^\|\s*(GET|POST|PATCH|PUT|DELETE)\s*\|\s*([^|]+?)\s*\|", re.MULTILINE)
PARAM_RE = re.compile(r"\{[^}]+\}")
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")
MAX_DIFF_LINES = 40

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def build_schema() -> dict:
    """生成当前代码的 OpenAPI（导入应用不会连数据库，所以不需要后端在跑）。"""
    from backend.main import app

    return app.openapi()


def dump_text(schema: dict) -> str:
    """稳定的文本形态：键排序消掉序列化抖动，`ensure_ascii=False` 让中文说明可读。"""
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_snapshot(text: str) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 显式指定换行：本机是 Windows，默认会写成 CRLF，而仓库的 .gitattributes 把文本文件统一成 LF。
    SNAPSHOT_PATH.write_text(text, encoding="utf-8", newline="\n")


def operations(schema: dict) -> dict[tuple[str, str], dict]:
    result: dict[tuple[str, str], dict] = {}
    for path, item in (schema.get("paths") or {}).items():
        for method, operation in item.items():
            if method.lower() in HTTP_METHODS:
                result[(method.upper(), path)] = operation
    return result


def models(schema: dict) -> dict[str, dict]:
    return (schema.get("components") or {}).get("schemas") or {}


def describe_changes(old: dict, new: dict) -> list[str]:
    """人话差异：接口与模型的新增、删除、改动。"""
    lines: list[str] = []
    if old.get("openapi") != new.get("openapi"):
        lines.append(f"~ OpenAPI 版本：{old.get('openapi')} → {new.get('openapi')}")
    if old.get("info") != new.get("info"):
        lines.append(f"~ 文档信息：{old.get('info')} → {new.get('info')}")

    old_ops, new_ops = operations(old), operations(new)
    for key in sorted(new_ops.keys() - old_ops.keys()):
        lines.append(f"+ 新增接口 {key[0]} {key[1]}")
    for key in sorted(old_ops.keys() - new_ops.keys()):
        lines.append(f"- 删除接口 {key[0]} {key[1]}")
    for key in sorted(old_ops.keys() & new_ops.keys()):
        if json.dumps(old_ops[key], sort_keys=True) != json.dumps(new_ops[key], sort_keys=True):
            lines.append(f"~ 接口改动 {key[0]} {key[1]}（参数/请求体/响应变了）")

    old_models, new_models = models(old), models(new)
    for name in sorted(new_models.keys() - old_models.keys()):
        lines.append(f"+ 新增模型 {name}")
    for name in sorted(old_models.keys() - new_models.keys()):
        lines.append(f"- 删除模型 {name}")
    for name in sorted(old_models.keys() & new_models.keys()):
        if json.dumps(old_models[name], sort_keys=True) != json.dumps(new_models[name], sort_keys=True):
            lines.append(f"~ 模型改动 {name}（字段或约束变了）")
    return lines


def normalize_path(path: str) -> str:
    """`/financial/transactions/{id}` 与 `{transaction_id}` 视为同一个路径。"""
    stripped = PARAM_RE.sub("{}", path).rstrip("/")
    return stripped or "/"


def contract_drift(schema: dict) -> list[str]:
    """`docs/api-contract.md` 的接口表点名了这个「方法 + 路径」，代码里就必须真的有。"""
    if not CONTRACT_PATH.exists():
        return [f"{CONTRACT_PATH.name} 不存在"]
    live = {(method.upper(), normalize_path(path)) for method, path in operations(schema)}
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    drift: list[str] = []
    for method, raw in CONTRACT_ROW_RE.findall(text):
        path = raw.strip().strip("`").split("?")[0].strip()
        if not path.startswith("/"):
            continue
        candidate = API_PREFIX + path
        if (method, normalize_path(candidate)) not in live:
            drift.append(f"{method} {candidate}")
    return drift


def undocumented(schema: dict) -> list[str]:
    """每个接口都该有一句说明：契约里出现光秃秃的接口就是文档债。"""
    return [f"{method} {path}"
            for (method, path), operation in operations(schema).items()
            if not (operation.get("summary") or operation.get("description"))]


def compare(schema: dict) -> None:
    text = dump_text(schema)
    live_ops, live_models = operations(schema), models(schema)

    if not SNAPSHOT_PATH.exists():
        check("docs/openapi.json 已提交", False,
              "快照不在：跑 --update 生成后一起提交（否则这份门禁等于没接上）")
    else:
        saved = SNAPSHOT_PATH.read_text(encoding="utf-8")
        same = saved == text
        check("快照与当前接口一致", same,
              f"{len(live_ops)} 个接口 / {len(live_models)} 个模型" if same else "接口变了而快照没更新")
        if not same:
            try:
                changes = describe_changes(json.loads(saved), schema)
            except json.JSONDecodeError as exc:
                changes = [f"快照不是合法 JSON：{exc}"]
            for line in changes[:MAX_DIFF_LINES]:
                print(f"    {line}", flush=True)
            if len(changes) > MAX_DIFF_LINES:
                print(f"    …（还有 {len(changes) - MAX_DIFF_LINES} 条差异）", flush=True)
            print("    接口改动是有意的就跑：python scripts\\check_openapi_snapshot.py --update，"
                  "然后把 docs/openapi.json 和代码一起提交", flush=True)

    drift = contract_drift(schema)
    check("docs/api-contract.md 点名的接口都真实存在", not drift,
          f"核对了 {len(CONTRACT_ROW_RE.findall(CONTRACT_PATH.read_text(encoding='utf-8')))} 行接口表"
          if not drift else "、".join(drift[:10]))

    missing = undocumented(schema)
    check("每个接口都带一句说明（summary / description）", not missing,
          f"{len(live_ops)} 个接口都有说明" if not missing else "、".join(missing[:10]))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="OpenAPI 接口契约快照")
    parser.add_argument("--update", action="store_true", help="接口确实改了：重新生成 docs/openapi.json")
    args = parser.parse_args()

    schema = build_schema()
    text = dump_text(schema)
    ops, api_models = operations(schema), models(schema)

    if args.update:
        write_snapshot(text)
        print(f"已写入 {SNAPSHOT_PATH.relative_to(PROJECT_ROOT)}："
              f"{len(ops)} 个接口 / {len(api_models)} 个模型 / {len(text.encode('utf-8')) // 1024} KB", flush=True)

    compare(schema)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    if passed == len(RESULTS):
        print(f"\n接口快照检查通过：{passed}/{len(RESULTS)} 项、{len(ops)} 个接口 / {len(api_models)} 个模型、"
              f"快照 {SNAPSHOT_PATH.name} {SNAPSHOT_PATH.stat().st_size // 1024} KB", flush=True)
    else:
        print(f"\n接口快照检查未通过：{passed}/{len(RESULTS)} 项", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 脚本要一句话说清哪儿炸了
        print(f"\n异常终止：{type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
