"""备份与恢复：一条命令把「数据库 + 上传件」打成归档，`restore` 默认只预演。

## 为什么不用 pg_dump

第一版这个脚本是拿 `docker exec finance-postgres pg_dump` 做的，被沙箱按命名管道边界挡死了
（python 子进程碰不到 Docker Desktop 的命名管道，捕获输出、DEVNULL、重定向到文件、inherit
四种 stdio 全试过，都是 `permission denied ... npipe:////./pipe/dockerDesktopLinuxEngine`）。
更根本的理由是：**本项目的库结构由代码定义**（`backend/models.py` + `backend/migrations.py`
的 `ensure_schema()`），所以备份只需要数据，不需要 DDL —— 结构与代码一起躺在 git 里，
把 DDL 再抄一份进归档反而会造出「归档里的结构与当前代码不一致」这种没人能修的状态。

于是这里走 PostgreSQL 自己的 `COPY`：

* 备份：每张表 `COPY (SELECT 列清单 FROM 表 ORDER BY 主键) TO STDOUT`，原样落成
  `database/<表名>.copy`。**文本格式由服务端生成、也由服务端解析回去**，所以
  `vector`（pgvector 384 维）、`jsonb`、`timestamptz`、`numeric` 都不需要在这里做序列化。
* 恢复：先 `ensure_schema()` 按当前代码把表建出来，再 `COPY 表 (列清单) FROM STDIN`。

「表结构来自代码，数据来自归档」也顺带解释了恢复旧归档时会发生什么：归档里有、当前模型里
已经没有的列会被点名报错，而不是悄悄丢掉。

## 归档里有什么，没有什么

有：`manifest.json`、`database/<表>.copy`、`files/uploads/**`。
没有（都在 manifest 的 `excluded` 里写明理由）：`.env`（含 `AUTH_SECRET_KEY`/`DEEPSEEK_API_KEY`）、
`data/models`（470 MB，可重新下载）、`data/index`（可由 `scripts/reindex_embeddings.py` 重建）、
`data/raw` 与 `data/processed`（随 git 分发）。

## 用法

    .\\.venv\\Scripts\\python.exe scripts\\backup_restore.py backup
    .\\.venv\\Scripts\\python.exe scripts\\backup_restore.py verify backups\\finance-*.tar.gz
    .\\.venv\\Scripts\\python.exe scripts\\backup_restore.py restore <归档> --database finance_restore_check --create --yes
    .\\.venv\\Scripts\\python.exe scripts\\backup_restore.py compare <归档> --database finance_restore_check

`restore` 不带 `--yes` 时只打印预演；真正的恢复与对平在 `scripts/verify_backup_restore.py` 里
落到临时库上跑一遍（不动演示库）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from sqlalchemy import Engine, create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = PROJECT_ROOT / "backups"
UPLOAD_DIR = PROJECT_ROOT / "data" / "uploads"

ARCHIVE_GLOB = "finance-*.tar.gz"
FORMAT = "finance-backup/2"
MANIFEST_NAME = "manifest.json"
DATABASE_PREFIX = "database/"
FILES_PREFIX = "files/uploads/"
COPY_FORMAT = "copy-text"

DEFAULT_DATABASE = "finance"
ADMIN_DATABASE = "postgres"

EXCLUDED: list[dict[str, str]] = [
    {"path": ".env", "why": "含 AUTH_SECRET_KEY / DEEPSEEK_API_KEY 等密钥，归档会被拷来拷去，不进归档"},
    {"path": "data/models", "why": "约 470 MB 的向量模型，可从发布渠道重新下载"},
    {"path": "data/index", "why": "可由 scripts/reindex_embeddings.py 从数据库重建"},
    {"path": "data/raw", "why": "样例数据随 git 分发，不需要进归档"},
    {"path": "data/processed", "why": "中间产物，可由流水线重新生成"},
]

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class BackupError(RuntimeError):
    """归档自身有问题，或者当前环境做不了这件事 —— 一律用这句人话报出去。"""


# --------------------------------------------------------------------------- 纯工具


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def default_archive_path(now: datetime | None = None, directory: Path = BACKUP_DIR) -> Path:
    moment = now or datetime.now(UTC)
    return directory / f"finance-{moment:%Y%m%d-%H%M%S}.tar.gz"


def safe_member_name(name: str) -> str:
    """归档成员名必须留在归档内部。

    备份文件是要被人拷来拷去的，解包时的路径穿越（tar slip）就是从这个字段进来的：
    绝对路径、`..`、盘符、反斜杠一律拒绝，而不是「提取时小心一点」。
    """
    cleaned = name.replace("\\", "/").strip()
    if not cleaned:
        raise BackupError("归档里有空成员名")
    if cleaned.startswith(("/", "../")) or "/../" in cleaned or cleaned.endswith("/.."):
        raise BackupError(f"归档里的成员名想跑到归档外面，拒绝处理：{name!r}")
    if ":" in cleaned.split("/")[0]:
        raise BackupError(f"归档里的成员名带盘符，拒绝处理：{name!r}")
    return cleaned


def upload_entries(root: Path = UPLOAD_DIR) -> list[dict]:
    """要进归档的上传件：归档内路径、字节数、sha256。目录不存在就是空。"""
    if not root.exists():
        return []
    entries: list[dict] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        entries.append({
            "path": f"{FILES_PREFIX}{relative}",
            "source": str(path),
            "bytes": path.stat().st_size,
            "sha256": file_digest(path),
        })
    return entries


def serial_columns(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """`information_schema.columns` 查出来的 (表, 列) 过滤成安全标识符，交给 `setval` 用。"""
    return [(table, column) for table, column in rows if IDENTIFIER.match(table) and IDENTIFIER.match(column)]


def git_commit() -> str | None:
    try:
        done = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT,
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def counts_diff(archived: dict[str, int], actual: dict[str, int]) -> list[str]:
    """归档里的行数 vs 目标库现在数出来的行数，逐表对平。"""
    problems: list[str] = []
    for table in sorted(set(archived) - set(actual)):
        problems.append(f"归档里有表 {table}，目标库里没有（模型里被删了？）")
    for table in sorted(set(actual) - set(archived)):
        problems.append(f"目标库里有表 {table}，归档里没有")
    for table in sorted(set(archived) & set(actual)):
        if archived[table] != actual[table]:
            problems.append(f"{table} 行数不一致：归档 {archived[table]}，目标库 {actual[table]}")
    return problems


def manifest_problems(manifest: dict, *, member_names: set[str], digests: dict[str, str],
                      sizes: dict[str, int]) -> list[str]:
    """只对着归档自己检查，不碰数据库：`verify` 与 `restore` 的第一步都用它。"""
    problems: list[str] = []
    if manifest.get("format") != FORMAT:
        problems.append(f"归档格式版本不认识：{manifest.get('format')!r}（本脚本支持 {FORMAT}）")
    created = manifest.get("created_at")
    if not created:
        problems.append("manifest 里没有 created_at")
    else:
        try:
            datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except ValueError:
            problems.append(f"manifest 里的 created_at 不是时间：{created!r}")
    source = manifest.get("source")
    if not isinstance(source, dict) or not source.get("database"):
        problems.append("manifest 里没有记录来源库（source.database）")

    database = manifest.get("database")
    if not isinstance(database, dict):
        problems.append("manifest 里没有 database 段")
    else:
        if database.get("format") != COPY_FORMAT:
            problems.append(f"数据库转储格式不认识：{database.get('format')!r}（本脚本支持 {COPY_FORMAT}）")
        entries = database.get("entries")
        if not isinstance(entries, list) or not entries:
            problems.append("manifest 里没有表清单（database.entries）")
            entries = []
        tables = database.get("tables")
        if not isinstance(tables, dict) or not tables:
            problems.append("manifest 里没有逐表行数（database.tables）")
            tables = {}
        total = 0
        for table, rows in sorted(tables.items()):
            if not isinstance(rows, int) or rows < 0:
                problems.append(f"{table} 的行数不是非负整数：{rows!r}")
            else:
                total += rows
        if isinstance(database.get("total_rows"), int) and database["total_rows"] != total:
            problems.append(f"total_rows（{database['total_rows']}）与逐表行数之和（{total}）对不上")
        seen_tables: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append(f"表清单里有不是字典的条目：{entry!r}")
                continue
            table = entry.get("table")
            path = entry.get("file")
            if not table or not path:
                problems.append(f"表清单条目缺 table/file：{entry!r}")
                continue
            if table in seen_tables:
                problems.append(f"表 {table} 在清单里出现多次")
            seen_tables.add(table)
            if path not in member_names:
                problems.append(f"{table} 的数据文件不在归档里：{path}")
                continue
            if entry.get("bytes") != sizes.get(path):
                problems.append(f"{table} 的字节数对不上：manifest {entry.get('bytes')}，实际 {sizes.get(path)}")
            if entry.get("sha256") != digests.get(path):
                problems.append(f"{table} 的 sha256 对不上，归档被动过或写坏了：{path}")
            if table not in tables:
                problems.append(f"{table} 有数据文件，却没记行数")
            elif entry.get("rows") != tables.get(table):
                problems.append(f"{table} 的行数与清单不一致：条目 {entry.get('rows')}，tables {tables.get(table)}")
            columns = entry.get("columns")
            if not isinstance(columns, list) or not columns or not all(isinstance(c, str) and c for c in columns):
                problems.append(f"{table} 的列清单不是非空字符串列表：{columns!r}")

    files = manifest.get("files")
    accounted = {MANIFEST_NAME}
    if isinstance(database, dict) and isinstance(database.get("entries"), list):
        accounted |= {e.get("file") for e in database["entries"] if isinstance(e, dict) and e.get("file")}
    if not isinstance(files, dict):
        problems.append("manifest 里没有 files 段")
    else:
        entries = files.get("entries")
        if not isinstance(entries, list):
            problems.append("manifest 里没有上传件清单（files.entries）")
            entries = []
        if files.get("count") != len(entries):
            problems.append(f"files.count（{files.get('count')}）与清单条数（{len(entries)}）对不上")
        total_bytes = 0
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append(f"上传件清单里有不是字典的条目：{entry!r}")
                continue
            path = entry.get("path")
            if not path or not str(path).startswith(FILES_PREFIX):
                problems.append(f"上传件路径不在 {FILES_PREFIX} 下：{path!r}")
                continue
            accounted.add(path)
            if path not in member_names:
                problems.append(f"上传件不在归档里：{path}")
                continue
            if entry.get("bytes") != sizes.get(path):
                problems.append(f"上传件字节数对不上：{path}")
            if entry.get("sha256") != digests.get(path):
                problems.append(f"上传件 sha256 对不上：{path}")
            if isinstance(entry.get("bytes"), int):
                total_bytes += entry["bytes"]
        if isinstance(files.get("bytes"), int) and files["bytes"] != total_bytes:
            problems.append(f"files.bytes（{files['bytes']}）与清单之和（{total_bytes}）对不上")

    extra = sorted(name for name in member_names - accounted if not name.endswith("/"))
    for name in extra:
        problems.append(f"归档里有 manifest 没记录的文件：{name}")
    return problems


def read_archive(path: Path) -> tuple[dict, dict[str, str], dict[str, int]]:
    """读归档的 manifest + 每个成员的 sha256/字节数（不解包到磁盘）。"""
    if not path.exists():
        raise BackupError(f"归档不存在：{path}")
    digests: dict[str, str] = {}
    sizes: dict[str, int] = {}
    try:
        with tarfile.open(path, "r:gz") as archive:
            manifest: dict | None = None
            for member in archive.getmembers():
                name = safe_member_name(member.name)
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    raise BackupError(f"归档成员读不出来：{name}")
                payload = handle.read()
                digests[name] = digest_bytes(payload)
                sizes[name] = member.size
                if name == MANIFEST_NAME:
                    try:
                        manifest = json.loads(payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise BackupError(f"归档里的 {MANIFEST_NAME} 不是合法 JSON：{exc}") from exc
    except tarfile.TarError as exc:
        raise BackupError(f"归档打不开：{exc}") from exc
    if manifest is None:
        raise BackupError(f"归档里没有 {MANIFEST_NAME}")
    return manifest, digests, sizes


def extract_members(archive_path: Path, destination: Path, names: set[str]) -> list[Path]:
    """把指定成员解到 `destination`（成员名先过 `safe_member_name`，落点必须在目标目录内）。"""
    written: list[Path] = []
    destination = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            name = safe_member_name(member.name)
            if name not in names or not member.isfile():
                continue
            target = (destination / name).resolve()
            if not str(target).startswith(str(destination)):
                raise BackupError(f"成员 {name} 会落到目标目录外面，拒绝解包")
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = archive.extractfile(member)
            if handle is None:
                raise BackupError(f"归档成员读不出来：{name}")
            target.write_bytes(handle.read())
            written.append(target)
    return written


def copy_tree(source: Path, destination: Path, *, overwrite: bool = False) -> tuple[int, int]:
    """把上传件铺回目标目录，返回 (写入, 跳过)。"""
    copied = skipped = 0
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        relative = path.relative_to(source)
        target = destination / relative
        if target.exists() and not overwrite:
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    return copied, skipped


def prune_archives(directory: Path = BACKUP_DIR, keep: int = 5) -> list[Path]:
    """只保留最近的 `keep` 份归档，返回被删掉的。"""
    if keep < 0 or not directory.exists():
        return []
    archives = sorted(directory.glob(ARCHIVE_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    for path in archives[keep:]:
        path.unlink()
        removed.append(path)
    return removed


# --------------------------------------------------------------------------- 数据库


def engine_for(database: str | None = None) -> Engine:
    from backend.config import get_settings

    url = create_engine(get_settings().database_url).url
    target = url if not database else url.set(database=database)
    return create_engine(target, pool_pre_ping=True)


def dsn(database: str) -> str:
    from backend.config import get_settings

    url = create_engine(get_settings().database_url).url.set(database=database)
    return url.render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://")


def model_tables() -> list[tuple[str, list[str]]]:
    """按外键顺序排出「代码里定义的」表和列 —— 恢复时的建表与装载顺序都用它。

    注意 `document_chunks` 不在 `backend/models.py` 里而在 `backend/rag/pgvector_store.py`，
    两个模块都要导入，否则这张表会被当成「代码没定义的表」（`backend/migrations.py:45` 记的是同一个坑）。
    """
    import backend.models  # noqa: F401  （导入即注册到 Base.metadata）
    from backend.db import Base
    from backend.rag import pgvector_store as _pgvector_store  # noqa: F401  （向量表也在这个模块里）

    return [(table.name, [column.name for column in table.columns]) for table in Base.metadata.sorted_tables]


def database_tables(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename"))
        return [row[0] for row in rows]


def database_row_counts(engine: Engine, tables: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for table in tables:
            counts[table] = int(connection.scalar(text(f'SELECT count(*) FROM "{table}"')))
    return counts


def server_version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(text("SHOW server_version")))


def dump_table(conninfo: str, table: str, columns: list[str]) -> bytes:
    collist = ", ".join(f'"{column}"' for column in columns)
    statement = f'COPY (SELECT {collist} FROM "{table}" ORDER BY {collist}) TO STDOUT'
    with (psycopg.connect(conninfo) as connection, connection.cursor() as cursor,
          cursor.copy(statement) as copy):
        return b"".join(bytes(chunk) for chunk in copy)


def load_table(conninfo: str, table: str, columns: list[str], payload: bytes) -> None:
    collist = ", ".join(f'"{column}"' for column in columns)
    with psycopg.connect(conninfo) as connection, connection.cursor() as cursor:
        with cursor.copy(f'COPY "{table}" ({collist}) FROM STDIN') as copy:
            copy.write(payload)
        connection.commit()


def existing_columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = :table"), {"table": table})
        return {row[0] for row in rows}


def advance_sequences(engine: Engine) -> list[str]:
    """`COPY` 进来的 id 不会推进序列，恢复完必须 `setval` 到 max(id)，否则下次插入就撞主键。"""
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND column_default LIKE 'nextval(%'"))
        pairs = serial_columns([(row[0], row[1]) for row in rows])
    advanced: list[str] = []
    for table, column in pairs:
        with engine.begin() as connection:
            maximum = connection.scalar(text(f'SELECT max("{column}") FROM "{table}"'))
            connection.execute(
                text("SELECT setval(pg_get_serial_sequence(:table, :column),"
                     " COALESCE(:maximum, 1), :called)"),
                {"table": table, "column": column, "maximum": maximum, "called": maximum is not None})
        advanced.append(f"{table}.{column}")
    return advanced


def ensure_database(name: str) -> bool:
    """目标库不存在就建一个（要连到 postgres 库并 autocommit）。已存在返回 False。"""
    if not IDENTIFIER.match(name):
        raise BackupError(f"库名不是合法标识符，拒绝建库：{name!r}")
    with psycopg.connect(dsn(ADMIN_DATABASE), autocommit=True) as connection:
        exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if exists:
            return False
        connection.execute(f'CREATE DATABASE "{name}"')
    return True


def create_schema(engine: Engine) -> list[str]:
    from backend.migrations import ensure_schema

    return ensure_schema(engine)


def truncate_tables(engine: Engine, tables: list[str]) -> None:
    if not tables:
        return
    joined = ", ".join(f'"{table}"' for table in reversed(tables))
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {joined} CASCADE"))


# --------------------------------------------------------------------------- 子命令


def stage_database(engine: Engine, staging: Path, tables: list[tuple[str, list[str]]]) -> tuple[list[dict], dict[str, int]]:
    """把每张表 COPY 成 `database/<表名>.copy`，返回清单条目与逐表行数。"""
    conninfo = dsn(engine.url.database or DEFAULT_DATABASE)
    entries: list[dict] = []
    counts: dict[str, int] = {}
    folder = staging / "database"
    folder.mkdir(parents=True, exist_ok=True)
    for table, columns in tables:
        payload = dump_table(conninfo, table, columns)
        target = folder / f"{table}.copy"
        target.write_bytes(payload)
        with engine.connect() as connection:
            rows = int(connection.scalar(text(f'SELECT count(*) FROM "{table}"')))
        counts[table] = rows
        entries.append({
            "table": table,
            "file": f"{DATABASE_PREFIX}{table}.copy",
            "columns": columns,
            "rows": rows,
            "bytes": len(payload),
            "sha256": digest_bytes(payload),
        })
    return entries, counts


def command_backup(args: argparse.Namespace) -> int:
    database = args.database or DEFAULT_DATABASE
    engine = engine_for(database)
    tables = model_tables()
    known = {name for name, _columns in tables}
    unknown = sorted(set(database_tables(engine)) - known)
    if unknown:
        raise BackupError("库里有代码没定义的表，先补进 backend/models.py 再备份，否则会静默漏数据：" + ", ".join(unknown))
    uploads = upload_entries()
    target = Path(args.out) if args.out else default_archive_path(directory=Path(args.directory))
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="finance-backup-") as staging_name:
        staging = Path(staging_name)
        entries, counts = stage_database(engine, staging, tables)
        if uploads:
            for entry in uploads:
                relative = entry["path"][len(FILES_PREFIX):]
                destination = staging / FILES_PREFIX / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry["source"], destination)
        manifest = {
            "format": FORMAT,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source": {
                "database": database,
                "server_version": server_version(engine),
                "git_commit": git_commit(),
            },
            "database": {
                "format": COPY_FORMAT,
                "note": "每张表一个 COPY 文本成员；恢复时先由 backend.migrations.ensure_schema() 按代码建表，再 COPY FROM STDIN",
                "entries": entries,
                "tables": counts,
                "total_rows": sum(counts.values()),
            },
            "files": {
                "root": "data/uploads",
                "prefix": FILES_PREFIX,
                "count": len(uploads),
                "bytes": sum(entry["bytes"] for entry in uploads),
                "entries": [{k: v for k, v in entry.items() if k != "source"} for entry in uploads],
            },
            "excluded": EXCLUDED,
        }
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8", newline="\n")
        with tarfile.open(target, "w:gz") as archive:
            archive.add(manifest_path, arcname=MANIFEST_NAME)
            for entry in entries:
                archive.add(staging / entry["file"], arcname=entry["file"])
            for entry in uploads:
                archive.add(staging / entry["path"], arcname=entry["path"])

    kept = prune_archives(Path(args.directory), args.keep) if args.keep is not None else []
    print(f"归档已写好：{target}")
    print(f"  {format_bytes(target.stat().st_size)}（{len(entries)} 张表 · {sum(counts.values())} 行 · {len(uploads)} 个上传件）")
    for table, rows in sorted(counts.items()):
        print(f"    {table:<28} {rows:>7} 行")
    print("  没有进去的：" + "、".join(item["path"] for item in EXCLUDED))
    for path in kept:
        print(f"  按 --keep 删掉了旧归档：{path}")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    path = Path(args.archive)
    manifest, digests, sizes = read_archive(path)
    problems = manifest_problems(manifest, member_names=set(digests), digests=digests, sizes=sizes)
    total = manifest.get("database", {}).get("total_rows")
    print(f"归档：{path}")
    print(f"  来源库 {manifest.get('source', {}).get('database')} · PostgreSQL {manifest.get('source', {}).get('server_version')}"
          f" · 建于 {manifest.get('created_at')}"
          f" · 代码 {manifest.get('source', {}).get('git_commit') or '未知'}")
    print(f"  {len(digests)} 个成员 · {sum(sizes.values())} 字节 · {total} 行数据")
    for item in manifest.get("excluded", []):
        print(f"  不含 {item.get('path')}：{item.get('why')}")
    if problems:
        print(f"\n归档自身就有 {len(problems)} 处问题：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\n归档自洽：manifest 与每个成员都对得上，没有多余文件。")
    print("注意：这里只检查归档自己，没碰数据库；要确认「恢复得回来」请用 restore / compare。")
    return 0


def command_restore(args: argparse.Namespace) -> int:
    path = Path(args.archive)
    manifest, digests, sizes = read_archive(path)
    problems = manifest_problems(manifest, member_names=set(digests), digests=digests, sizes=sizes)
    if problems:
        print("归档自身有问题，先修归档：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    database = args.database or manifest["source"]["database"]
    entries = manifest["database"]["entries"]
    uploads = manifest["files"]["entries"]
    files_target = Path(args.files_target) if args.files_target else UPLOAD_DIR

    print(f"归档：{path}")
    print(f"计划把 {len(entries)} 张表 / {manifest['database']['total_rows']} 行恢复到库 {database}"
          f"（{'不存在，将新建' if args.create else '必须已存在'}）")
    print(f"计划把 {len(uploads)} 个上传件恢复到 {files_target}（{'覆盖' if args.overwrite_files else '已存在就跳过'}）")
    print("恢复顺序（外键在前）：" + " → ".join(entry["table"] for entry in entries))
    if not args.yes:
        print("\n这是预演：没有动任何数据。确认无误后加 --yes 再跑一次。")
        return 0

    created = ensure_database(database) if args.create else False
    if created:
        print(f"已新建库 {database}")
    engine = engine_for(database)
    if not created:
        existing = set(database_tables(engine))
        expected = {entry["table"] for entry in entries}
        if not expected <= existing:
            missing = ", ".join(sorted(expected - existing))
            raise BackupError(f"目标库 {database} 里缺表：{missing}（加 --create 建库，或先跑一次后端让 ensure_schema 建表）")
    applied = create_schema(engine)
    if applied:
        print(f"ensure_schema 建了 {len(applied)} 项结构：{', '.join(applied[:6])}{' …' if len(applied) > 6 else ''}")

    conninfo = dsn(database)
    with tempfile.TemporaryDirectory(prefix="finance-restore-") as staging_name:
        staging = Path(staging_name)
        names = {entry["file"] for entry in entries} | {entry["path"] for entry in uploads}
        extract_members(path, staging, names)
        if args.drop_existing:
            truncate_tables(engine, [entry["table"] for entry in entries])
            print("已按 --drop-existing 清空目标表")
        for entry in entries:
            table = entry["table"]
            columns = entry["columns"]
            unknown = sorted(set(columns) - existing_columns(engine, table))
            if unknown:
                raise BackupError(
                    f"{table} 归档里有当前模型没有的列：{', '.join(unknown)}"
                    "（归档来自更老的代码；先用当时的代码恢复，或把列加回后端再恢复）")
            load_table(conninfo, table, columns, (staging / entry["file"]).read_bytes())
            print(f"  装入 {table:<28} {entry['rows']:>7} 行")
        advanced = advance_sequences(engine)
        if advanced:
            print(f"已把 {len(advanced)} 个序列推到 max(id)：{', '.join(advanced)}")
        if uploads:
            copied, skipped = copy_tree(staging / FILES_PREFIX.rstrip("/"), files_target,
                                        overwrite=args.overwrite_files)
            print(f"上传件：写入 {copied} 个，跳过 {skipped} 个 → {files_target}")

    actual = database_row_counts(engine, [entry["table"] for entry in entries])
    problems = counts_diff(manifest["database"]["tables"], actual)
    if problems:
        print("\n恢复完对不平：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"\n恢复完成并已逐表对平：{sum(actual.values())} 行")
    return 0


def command_compare(args: argparse.Namespace) -> int:
    path = Path(args.archive)
    manifest, _digests, _sizes = read_archive(path)
    archived = manifest.get("database", {}).get("tables", {})
    database = args.database or manifest["source"]["database"]
    engine = engine_for(database)
    actual = database_row_counts(engine, sorted(archived))
    print(f"归档：{path}\n目标库：{database}")
    for table in sorted(archived):
        flag = "OK " if archived[table] == actual.get(table) else "差异"
        print(f"  {flag} {table:<28} 归档 {archived[table]:>7} 库 {actual.get(table, 0):>7}")
    problems = counts_diff(archived, actual)
    if problems:
        print(f"\n{len(problems)} 张表对不平：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"\n{len(archived)} 张表行数全部一致，共 {sum(archived.values())} 行。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="备份与恢复：数据库（COPY 文本）+ data/uploads 打成一份 tar.gz",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="打一份归档")
    backup.add_argument("--out", help=f"归档路径（默认 {BACKUP_DIR}\\{ARCHIVE_GLOB}）")
    backup.add_argument("--directory", default=str(BACKUP_DIR), help="归档目录（默认 backups/）")
    backup.add_argument("--database", default=None, help=f"源库（默认 {DEFAULT_DATABASE}）")
    backup.add_argument("--keep", type=int, default=None, help="只保留最近 N 份归档，其余删掉")
    backup.set_defaults(func=command_backup)

    verify = sub.add_parser("verify", help="只看归档自身是否自洽（不碰数据库）")
    verify.add_argument("archive")
    verify.set_defaults(func=command_verify)

    restore = sub.add_parser("restore", help="把归档恢复进数据库（不加 --yes 只预演）")
    restore.add_argument("archive")
    restore.add_argument("--database", default=None, help="目标库（默认归档记录的来源库）")
    restore.add_argument("--create", action="store_true", help="目标库不存在就新建")
    restore.add_argument("--yes", action="store_true", help="真的写；不加就只打印预演")
    restore.add_argument("--drop-existing", action="store_true", help="恢复前先清空目标表")
    restore.add_argument("--files-target", default=None, help=f"上传件恢复到哪（默认 {UPLOAD_DIR}）")
    restore.add_argument("--overwrite-files", action="store_true", help="上传件同名也覆盖")
    restore.set_defaults(func=command_restore)

    compare = sub.add_parser("compare", help="逐表对平行数")
    compare.add_argument("archive")
    compare.add_argument("--database", default=None, help="目标库（默认归档记录的来源库）")
    compare.set_defaults(func=command_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BackupError as exc:
        print(f"\n失败：{exc}")
        print("检查：数据库在不在（docker compose up -d postgres）、.env 里的 DATABASE_URL 对不对")
        return 1


if __name__ == "__main__":
    sys.exit(main())
