"""离线拉取 python-pptx 及其依赖的 wheel（本机 pip 解析器会空转，改走手工下载）。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\fetch_pptx_wheels.py --out D:\\tmp\\pptx-wheels
之后：
    .\\.venv\\Scripts\\python.exe -m pip install --no-index --no-deps <下载到的 whl...>
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

JSON_API = "https://pypi.org/pypi/{name}/json"
# python-pptx 的运行期依赖：lxml 解析、Pillow 图片、XlsxWriter（pptx 内部写 xlsx 用）
PACKAGES = ("python-pptx", "lxml", "Pillow", "XlsxWriter", "typing_extensions")


def _console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _wheels(name: str) -> list[tuple[str, str]]:
    with urllib.request.urlopen(JSON_API.format(name=name), timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    urls = payload["urls"]
    picks: list[tuple[str, str]] = []
    for entry in urls:
        filename = entry["filename"]
        if not filename.endswith(".whl"):
            continue
        if filename.endswith("py3-none-any.whl") or ("cp311" in filename and "win_amd64" in filename):
            picks.append((filename, entry["url"]))
    return picks


def main() -> int:
    _console()
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in PACKAGES:
        picks = _wheels(name)
        if not picks:
            print(f"SKIP {name}：没有匹配 cp311/win_amd64 或 py3-none-any 的 wheel")
            continue
        filename, url = picks[0]
        target = out / filename
        if target.exists() and target.stat().st_size:
            print(f"EXISTS {filename}")
            continue
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        target.write_bytes(data)
        print(f"OK {filename} {len(data)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
