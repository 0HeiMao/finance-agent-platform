"""把爬取到的财务原始文件落到 data/raw/，并做完整性检查。

设计约束（来自 README「data/raw 只读」）：
- 本脚本只负责「下载 -> 校验 -> 落盘到新文件」，绝不覆盖或删除已有文件；
- 目标文件已存在时默认跳过（--force 才重新下载到新名字）；
- 无网络时用 --from-local 以本地已抓取副本作为输入，仍然走同一套校验，
  这样离线环境也能验证管道，而不是静默假装下载成功。

用法：
    # 在线抓取官方源
    python scripts/fetch_financial_source.py

    # 离线：校验本地已抓取副本并复制为带日期的归档文件
    python scripts/fetch_financial_source.py --from-local data/raw/receiver_general_accounting_transactions_sample_source.csv
"""

from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_URL = "https://donnees-data.tpsgc-pwgsc.gc.ca/ba1/tcrg-rgat/tcrg-rgat.csv"
# 默认目标就是本仓库已有的那份原始副本：已存在即跳过，用来证明只读约定确实生效。
DEFAULT_OUT = (
    PROJECT_ROOT / "data" / "raw" / "receiver_general_accounting_transactions_sample_source.csv"
)
LOCAL_FALLBACK = DEFAULT_OUT

# 源文件必须包含的关键字段前缀（中英双语列名以「英文-法文」拼接，故用前缀匹配）。
REQUIRED_PREFIXES = (
    "Accounting-Effective-Date",
    "DepartmentNumber",
    "Credit/Debit-Code",
    "Journal-Voucher-Item-Amount",
)


class FetchError(RuntimeError):
    """抓取或校验失败；调用方据此决定回退或中止。"""


def _console() -> None:
    """Windows 控制台默认 GBK，中文日志会乱码甚至抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def download(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "finance-platform-etl/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise FetchError(f"源站返回 HTTP {response.status}")
        return response.read()


def inspect(payload: bytes) -> dict:
    """只读取表头和行数，避免把整份大文件载入内存。"""
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise FetchError("文件不是有效的 UTF-8 或 GB18030 编码")

    lines = text.splitlines()
    if not lines:
        raise FetchError("文件为空")
    header = lines[0].split(",")
    missing = [p for p in REQUIRED_PREFIXES if not any(c.startswith(p) for c in header)]
    if missing:
        raise FetchError("缺少关键字段：" + "、".join(missing))
    return {
        "encoding": encoding,
        "columns": len(header),
        "rows": len([line for line in lines[1:] if line.strip()]),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def write_new_file(payload: bytes, target: Path, force: bool) -> Path:
    """写入新文件；目标已存在时默认跳过，绝不覆盖已有原始数据。"""
    if target.exists() and not force:
        raise FileExistsError(f"目标文件已存在，按只读约定不覆盖：{target.name}（如需另存请加 --force）")
    if target.exists() and force:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = target.with_name(f"{target.stem}.{stamp}{target.suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


# 退出码：0 成功落盘 / 1 目标已存在（正常跳过，非错误）/ 2 输入缺失
#         / 3 在线失败且无回退 / 4 完整性校验失败 / 5 其他 IO 失败
EXIT_OK = 0
EXIT_ALREADY_PRESENT = 1
EXIT_INPUT_MISSING = 2
EXIT_FETCH_FAILED = 3
EXIT_INVALID = 4
EXIT_IO_FAILED = 5


def main() -> int:
    _console()
    parser = argparse.ArgumentParser(description="抓取财务原始文件并落盘到 data/raw/")
    parser.add_argument("--url", default=DEFAULT_URL, help="源文件地址")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="落盘路径")
    parser.add_argument("--timeout", type=int, default=60, help="下载超时秒数")
    parser.add_argument("--from-local", default=None, help="离线模式：以本地已抓取副本作为输入")
    parser.add_argument("--force", action="store_true", help="目标存在时另存为带时间戳的新文件")
    parser.add_argument("--dry-run", action="store_true", help="只抓取并校验，不写入任何文件")
    args = parser.parse_args()

    target = Path(args.out).resolve()
    source_label = ""

    if args.from_local:
        local = Path(args.from_local).resolve()
        if not local.exists():
            print(f"FAIL: 本地副本不存在 {local}", file=sys.stderr)
            return EXIT_INPUT_MISSING
        payload = local.read_bytes()
        source_label = f"local:{local.name}"
    else:
        try:
            payload = download(args.url, args.timeout)
            source_label = f"remote:{args.url}"
        except (urllib.error.URLError, FetchError, OSError) as exc:
            # 离线回退：仍要用同一套校验，并在输出里明确标注来源是本地副本，
            # 不能让调用方误以为这是一次成功的在线抓取。
            if not LOCAL_FALLBACK.exists():
                print(f"FAIL: 在线抓取失败（{exc}），且无本地副本可回退", file=sys.stderr)
                return EXIT_FETCH_FAILED
            print(f"WARN: 在线抓取失败（{exc}），回退到本地副本 {LOCAL_FALLBACK.name}", file=sys.stderr)
            payload = LOCAL_FALLBACK.read_bytes()
            source_label = f"local-fallback:{LOCAL_FALLBACK.name}"

    try:
        info = inspect(payload)
    except FetchError as exc:
        print(f"FAIL: 校验未通过：{exc}", file=sys.stderr)
        return EXIT_INVALID

    print(f"来源      : {source_label}")
    print(f"编码      : {info['encoding']}")
    print(f"字段数    : {info['columns']}")
    print(f"数据行数  : {info['rows']}")
    print(f"sha256    : {info['sha256']}")

    if args.dry_run:
        print("PASS: 原始文件校验通过（--dry-run，未写入任何文件）")
        return EXIT_OK

    try:
        written = write_new_file(payload, target, args.force)
    except FileExistsError as exc:
        print(f"SKIP: {exc}")
        print("PASS: 现有原始文件在只读约定下保持不变（未覆盖、未删除）")
        return EXIT_ALREADY_PRESENT
    except OSError as exc:
        print(f"FAIL: 写入失败：{exc}", file=sys.stderr)
        return EXIT_IO_FAILED

    print(f"落盘路径  : {written.relative_to(PROJECT_ROOT)}")
    print("PASS: 原始文件已落盘并通过完整性检查（未覆盖任何既有文件）")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
