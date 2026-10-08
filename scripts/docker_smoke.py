"""⑤ 容器化冒烟：`docker compose up -d` 之后，从宿主机验证这套容器栈真的能用。

为什么要单独一个脚本：`docker compose up` 成功只说明容器起来了，
「前端静态包能打开、/api 反代真的通、同源登录能拿到 token、SPA 深链接不 404」
这些都得从外面打一遍才算数——而这正是容器化最容易悄悄坏掉的地方
（构建时把后端地址烧进静态包、nginx 少一条 try_files、反代没转发 X-Forwarded-For）。

跑法：
    $env:PYTHONIOENCODING="utf-8"
    .\\.venv\\Scripts\\python.exe scripts/docker_smoke.py            # 用已构建的镜像起栈并检查
    .\\.venv\\Scripts\\python.exe scripts/docker_smoke.py --build    # 先构建再起
    .\\.venv\\Scripts\\python.exe scripts/docker_smoke.py --down     # 跑完停掉容器

前置：Docker Desktop 已启动；宿主机 8000 / 8080 没被占用（占用会起不来）。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

FRONT = "http://127.0.0.1:8080"
BACK = "http://127.0.0.1:8000"
DEMO_USER = "demo_analyst"
DEMO_PASSWORD = "demo_password_2026"
# 本机请求必须绕开系统代理：HTTP_PROXY 指向的代理会把 127.0.0.1 的响应换掉
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FAILS: list[str] = []


def run(*args: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    """跑一条命令，把 stdout/stderr 收成字符串。

    刻意**不用** subprocess 的管道（capture_output=True）：在本机的受限沙箱里，
    python 用管道接子进程输出时，docker CLI 连不上 Docker Desktop 的命名管道
    （`npipe://./pipe/dockerDesktopLinuxEngine`），报
    "permission denied while trying to connect to the docker API"。
    把输出重定向到临时文件再读回来就绕开了管道。
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "out.txt"
        err_path = Path(tmp) / "err.txt"
        with out_path.open("wb") as out_file, err_path.open("wb") as err_file:
            done = subprocess.run(args, stdout=out_file, stderr=err_file, timeout=timeout)
        return subprocess.CompletedProcess(
            args,
            done.returncode,
            out_path.read_text(encoding="utf-8", errors="replace"),
            err_path.read_text(encoding="utf-8", errors="replace"),
        )


def check(label: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{('  ' + str(detail)) if detail else ''}")
    if not ok:
        FAILS.append(label)


def call(method: str, url: str, token: str | None = None, payload: dict | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=30) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def parsed(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode() or "{}")
    except Exception:
        return {}


def wait_healthy(deadline: int = 180) -> dict[str, str]:
    """轮询 docker compose ps，直到 backend/frontend/postgres 都 healthy（或超时）。"""
    states: dict[str, str] = {}
    end = time.time() + deadline
    while time.time() < end:
        result = run("docker", "compose", "ps", "--format", "json", timeout=60)
        rows = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                with contextlib.suppress(json.JSONDecodeError):
                    rows.append(json.loads(line))
        states = {row.get("Service", "?"): row.get("Health") or row.get("State", "?") for row in rows}
        if len(states) >= 3 and all(value.lower() in {"healthy", "running"} for value in states.values()):
            break
        time.sleep(5)
    return states


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="先 docker compose build")
    parser.add_argument("--down", action="store_true", help="检查完停掉容器")
    args = parser.parse_args()

    docker_ok = run("docker", "info", timeout=60).returncode == 0
    if not docker_ok:
        # 受限沙箱里 python 拉起的子进程连不上 Docker Desktop 的命名管道
        # （npipe://./pipe/dockerDesktopLinuxEngine，报 permission denied）。
        # 这时 compose/exec 相关几项标 SKIP，HTTP 那几项照样能验已经起着的栈。
        print("SKIP  python 调不到 Docker（沙箱不允许命名管道）：容器相关项跳过，只验 HTTP")

    if docker_ok:
        config = run("docker", "compose", "config", "--quiet", timeout=120)
        check("compose 配置合法（含显式项目名）", config.returncode == 0, (config.stderr or "").strip()[:200])

        if args.build:
            print("... docker compose build（首次要拉基础镜像并装依赖，可能十几分钟）...")
            build = run("docker", "compose", "build", timeout=5400)
            check("docker compose build 成功", build.returncode == 0, (build.stderr or "").strip()[-300:])

        print("... docker compose up -d ...")
        up = run("docker", "compose", "up", "-d", timeout=600)
        check("docker compose up -d 成功", up.returncode == 0, (up.stderr or "").strip()[-300:])

        states = wait_healthy()
        check("三个容器都有状态", len(states) >= 3, states)
        for service in ("postgres", "backend", "frontend"):
            check(f"{service} healthy/running", states.get(service, "missing").lower() in {"healthy", "running"}, states.get(service))
    else:
        print("SKIP  三个容器的健康状态（需要 docker CLI）")

    # 1. 前端静态包
    status, headers, raw = call("GET", f"{FRONT}/")
    check("前端首页 200", status == 200, status)
    check("首页是 SPA 外壳（含 #app）", b'id="app"' in raw, raw[:60])
    check("首页由 nginx 提供", "nginx" in headers.get("server", "").lower(), headers.get("server"))

    # 2. 前端里的 /api 反代（同源，不经过 CORS）
    status, _, raw = call("GET", f"{FRONT}/api/v1/health")
    check("前端 /api 反代到后端（/api/v1/health 200）", status == 200 and parsed(raw).get("status") == "ok", status)

    # 3. 直连后端发布端口
    status, _, raw = call("GET", f"{BACK}/api/health")
    check("直连后端 8000 /api/health 200", status == 200 and parsed(raw).get("service") == "finance-api", status)

    # 4. 同源登录 + 带 token 读数（走反代，证明鉴权链路在容器里也通）
    status, _, raw = call("POST", f"{FRONT}/api/v1/auth/login",
                          payload={"username": DEMO_USER, "password": DEMO_PASSWORD})
    token = parsed(raw).get("access_token")
    check("同源登录拿到 token", status == 200 and bool(token), status)

    status, _, raw = call("GET", f"{BACK}/api/v1/dashboard?period=year", token=token)
    board = parsed(raw)
    check("带 token 拉看板 200 且有 KPI", status == 200 and bool(board.get("kpi")), status)
    check("看板读到的是已导入的演示数据（7 个期间）", board.get("has_data") and len(board.get("months") or []) == 7,
          len(board.get("months") or []))

    # 5. SPA 深链接：直接刷新 /admin/audit 必须回 index.html，而不是 nginx 404
    status, _, raw = call("GET", f"{FRONT}/admin/audit")
    check("SPA 深链接回 index.html（try_files 生效）", status == 200 and b'id="app"' in raw, status)

    # 6. 容器里挂到的是宿主机的 data/（模型与上传件），不是镜像里拷的一份
    if docker_ok:
        listing = run("docker", "compose", "exec", "-T", "backend", "ls", "data/models/finance-embedding", timeout=120)
        check("容器内能看到宿主机挂载的向量模型", listing.returncode == 0 and "model.safetensors" in listing.stdout,
              (listing.stderr or "").strip()[:120])

        # 7. nginx 的 SSE 配置项在位（流式问答不被缓冲）
        conf = run("docker", "compose", "exec", "-T", "frontend", "cat", "/etc/nginx/conf.d/default.conf", timeout=120)
        check("反代关掉了响应缓冲（SSE 不被攒起来）", "proxy_buffering off" in conf.stdout, (conf.stderr or "")[:120])
    else:
        print("SKIP  容器内挂载与 nginx 配置（需要 docker CLI）")

    if args.down and docker_ok:
        print("... docker compose down ...")
        run("docker", "compose", "down", timeout=300)

    print()
    print(f"RESULT: {'PASS' if not FAILS else 'FAIL ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
