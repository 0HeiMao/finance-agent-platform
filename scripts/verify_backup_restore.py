"""真机验证：备份与恢复（⑪）。

前置：数据库在跑（`docker compose up -d postgres`），`.env` 里的 `DATABASE_URL` 能连上。
跑法：

    .\\.venv\\Scripts\\python.exe scripts\\verify_backup_restore.py

它做的事：拿真库打一份归档 → 只对着归档自检 → **恢复进一个临时库 `finance_restore_check`**
（用一次预演确认没 `--yes` 就不动数据，再真恢复）→ 逐表对平行数、抽查 `vector` 列与序列 →
把上传件往返走一遍 → 用三个负例（篡改数据、篡改清单、清单里多一列）确认该红的会红 →
最后删掉临时库、探针文件与探针归档。

**不动演示库 `finance`，也不动 `data/uploads` 里的真实文件**（上传件探针写在
`data/uploads/verify-backup-probe.txt`，用完删掉；恢复出来的上传件落在 `output/_restore_uploads/`）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from backend.config import get_settings  # noqa: E402

SCRIPT = PROJECT_ROOT / "scripts" / "backup_restore.py"
ARCHIVE = PROJECT_ROOT / "output" / "_bk_verify.tar.gz"
FILES_ARCHIVE = PROJECT_ROOT / "output" / "_bk_verify_files.tar.gz"
TAMPERED = PROJECT_ROOT / "output" / "_bk_verify_tampered.tar.gz"
EXTRA_COLUMN = PROJECT_ROOT / "output" / "_bk_verify_extra_column.tar.gz"
BAD_DIGEST = PROJECT_ROOT / "output" / "_bk_verify_bad_digest.tar.gz"
RESTORE_FILES = PROJECT_ROOT / "output" / "_restore_uploads"
UPLOAD_PROBE = PROJECT_ROOT / "data" / "uploads" / "verify-backup-probe.txt"
UPLOAD_PROBE_TEXT = "备份恢复探针 2026-10-08\n第二行\n"

SCRATCH_DB = "finance_restore_check"
SOURCE_DB = "finance"

RESULTS: list[tuple[str, bool, str]] = []
PROBE_ARCHIVES = (ARCHIVE, FILES_ARCHIVE, TAMPERED, EXTRA_COLUMN, BAD_DIGEST)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" —— {detail}" if detail else ""))


def url_for(database: str):
    return create_engine(get_settings().database_url).url.set(database=database)


def engine_for(database: str):
    return create_engine(url_for(database), pool_pre_ping=True)


def conninfo(database: str) -> str:
    return url_for(database).render_as_string(hide_password=False).replace(
        "postgresql+psycopg://", "postgresql://")


def scalar(database: str, sql: str, **params):
    engine = engine_for(database)
    try:
        with engine.connect() as connection:
            return connection.scalar(text(sql), params or None)
    finally:
        engine.dispose()


def row_counts(database: str, tables: list[str]) -> dict[str, int]:
    engine = engine_for(database)
    try:
        with engine.connect() as connection:
            return {table: int(connection.scalar(text(f'SELECT count(*) FROM "{table}"'))) for table in tables}
    finally:
        engine.dispose()


def table_names(database: str) -> list[str]:
    engine = engine_for(database)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"))
            return [str(row[0]) for row in rows]
    finally:
        engine.dispose()


def database_names() -> list[str]:
    engine = engine_for("postgres")
    try:
        with engine.connect() as connection:
            return [row[0] for row in connection.execute(text("SELECT datname FROM pg_database"))]
    finally:
        engine.dispose()


def drop_database(name: str) -> None:
    if name not in database_names():
        return
    with psycopg.connect(conninfo("postgres"), autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{name}"')


def run(*args: str, timeout: int = 900) -> tuple[int, str]:
    """跑 CLI。输出走临时文件而不是管道：受限沙箱里 python 用管道接子进程输出会踩命名管道边界。"""
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT), "PYTHONIOENCODING": "utf-8"}
    with tempfile.TemporaryDirectory(prefix="verify-backup-") as tmp:
        log = Path(tmp) / "out.txt"
        with log.open("wb") as handle:
            done = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=PROJECT_ROOT,
                                  env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
        return done.returncode, log.read_text(encoding="utf-8", errors="replace")


def archive_members(path: Path) -> list[str]:
    with tarfile.open(path, "r:gz") as archive:
        return [member.name for member in archive.getmembers() if member.isfile()]


def archive_bytes(path: Path, member: str) -> bytes:
    with tarfile.open(path, "r:gz") as archive:
        handle = archive.extractfile(member)
        if handle is None:
            raise KeyError(member)
        return handle.read()


def manifest_of(path: Path) -> dict:
    return json.loads(archive_bytes(path, "manifest.json").decode("utf-8"))


def rewrite_archive(source: Path, target: Path, *, mutate_manifest=None, replace: tuple[bytes, bytes] | None = None,
                    replace_member: str = "database/users.copy") -> None:
    """按需改一个数据成员或改 manifest，再打成新归档 —— 用来造「被动过的归档」。"""
    with tarfile.open(source, "r:gz") as reader, tarfile.open(target, "w:gz") as writer:
        for member in reader.getmembers():
            handle = reader.extractfile(member)
            payload = b"" if handle is None else handle.read()
            if member.isfile() and member.name == "manifest.json" and mutate_manifest is not None:
                manifest = json.loads(payload.decode("utf-8"))
                mutate_manifest(manifest)
                payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            elif member.isfile() and member.name == replace_member and replace is not None:
                payload = payload.replace(*replace, 1)
            info = tarfile.TarInfo(member.name)
            info.size = len(payload)
            info.mode = member.mode
            writer.addfile(info, io.BytesIO(payload))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    print(f"备份恢复真机验证：源库 {SOURCE_DB}，临时库 {SCRATCH_DB}\n")

    # ---------------------------------------------------------------- 收尾现场
    drop_database(SCRATCH_DB)
    if RESTORE_FILES.exists():
        shutil.rmtree(RESTORE_FILES)
    for path in PROBE_ARCHIVES:
        path.unlink(missing_ok=True)
    UPLOAD_PROBE.unlink(missing_ok=True)

    source_tables = table_names(SOURCE_DB)
    live_counts = row_counts(SOURCE_DB, source_tables)
    live_total = sum(live_counts.values())
    print(f"活库 {len(source_tables)} 张表、{live_total} 行\n")

    # ---------------------------------------------------------------- 1. 打归档
    code, output = run("backup", "--out", str(ARCHIVE))
    check("backup 退出码 0 并写出归档", code == 0 and ARCHIVE.exists(),
          f"exit={code} {ARCHIVE.stat().st_size if ARCHIVE.exists() else 0} 字节")
    members = archive_members(ARCHIVE) if ARCHIVE.exists() else []
    expected = {f"database/{table}.copy" for table in source_tables}
    check("归档里每张表都有一个数据成员", expected <= set(members),
          f"缺 {sorted(expected - set(members))}" if not expected <= set(members) else f"{len(members)} 个成员")
    check("归档里有 manifest.json", "manifest.json" in members)
    leaked = [name for name in members if name.startswith(("models/", "index/")) or name.endswith(".env")]
    check("归档里没有 .env / data/models / data/index", not leaked, ", ".join(leaked))

    manifest = manifest_of(ARCHIVE)
    check("manifest 记录了来源库与排除清单",
          manifest.get("source", {}).get("database") == SOURCE_DB
          and any(item.get("path") == ".env" for item in manifest.get("excluded", [])))
    check("manifest 的行数与活库 count(*) 一致",
          manifest.get("database", {}).get("tables") == live_counts
          and manifest.get("database", {}).get("total_rows") == live_total,
          f"归档 {manifest.get('database', {}).get('total_rows')} / 活库 {live_total}")
    entries = {entry["table"]: entry for entry in manifest["database"]["entries"]}
    digest_ok = all(entry["sha256"] == sha256_bytes(archive_bytes(ARCHIVE, entry["file"]))
                    for entry in entries.values())
    check("manifest 里每个数据文件的 sha256 都对得上", digest_ok)

    code, output = run("verify", str(ARCHIVE))
    check("verify 说自己自洽", code == 0 and "归档自洽" in output, f"exit={code}")
    check("verify 把「为什么不备份 .env」打了出来", "不含 .env" in output and "密钥" in output)

    # ---------------------------------------------------------------- 2. 预演不动库
    check("预演前临时库不存在", SCRATCH_DB not in database_names())
    code, output = run("restore", str(ARCHIVE), "--database", SCRATCH_DB, "--create")
    check("restore 不带 --yes 只预演", code == 0 and "这是预演" in output, f"exit={code}")
    check("预演之后临时库仍然不存在", SCRATCH_DB not in database_names())

    # ---------------------------------------------------------------- 3. 真恢复 + 对平
    code, output = run("restore", str(ARCHIVE), "--database", SCRATCH_DB, "--create", "--yes")
    check("restore --create --yes 退出码 0", code == 0, f"exit={code}")
    check("恢复过程打印了序列推进", "序列" in output)

    code, output = run("compare", str(ARCHIVE), "--database", SCRATCH_DB)
    restored_counts = row_counts(SCRATCH_DB, source_tables) if SCRATCH_DB in database_names() else {}
    check("compare 逐表对平", code == 0 and "全部一致" in output, f"exit={code}")
    check("恢复出来的库逐表行数与活库一致", restored_counts == live_counts,
          "" if restored_counts == live_counts
          else "; ".join(f"{t}: {live_counts[t]}→{restored_counts.get(t)}" for t in source_tables
                         if live_counts[t] != restored_counts.get(t)))

    chunks = scalar(SCRATCH_DB, "SELECT count(*) FROM document_chunks")
    filled = scalar(SCRATCH_DB, "SELECT count(*) FROM document_chunks WHERE embedding IS NOT NULL")
    dims = scalar(SCRATCH_DB, "SELECT vector_dims(embedding) FROM document_chunks LIMIT 1")
    check("vector 列原样恢复（行数、非空、维度）",
          chunks == live_counts.get("document_chunks") and filled == chunks and int(dims or 0) == 384,
          f"chunks={chunks} 非空={filled} 维度={dims}")

    next_id = scalar(SCRATCH_DB, "SELECT nextval(pg_get_serial_sequence('users', 'id'))")
    max_id = scalar(SCRATCH_DB, "SELECT max(id) FROM users")
    check("恢复后序列已推到 max(id) 之后（不会撞主键）", int(next_id or 0) > int(max_id or 0),
          f"nextval={next_id} max(id)={max_id}")
    newest = scalar(SCRATCH_DB, "SELECT count(*) FROM audit_logs WHERE created_at IS NOT NULL")
    check("恢复出来的表能正常查询", int(newest or 0) == live_counts.get("audit_logs"),
          f"{newest}/{live_counts.get('audit_logs')}")

    # ---------------------------------------------------------------- 4. 上传件往返
    UPLOAD_PROBE.parent.mkdir(parents=True, exist_ok=True)
    # 显式 newline="\n"：Windows 上默认会把 \n 翻成 \r\n，那样「磁盘上的字节」和常量就对不上了
    UPLOAD_PROBE.write_text(UPLOAD_PROBE_TEXT, encoding="utf-8", newline="\n")
    probe_bytes = UPLOAD_PROBE.read_bytes()
    code, output = run("backup", "--out", str(FILES_ARCHIVE))
    files_manifest = manifest_of(FILES_ARCHIVE) if FILES_ARCHIVE.exists() else {}
    probe_member = "files/uploads/verify-backup-probe.txt"
    check("备份把上传件也带上了", code == 0 and probe_member in archive_members(FILES_ARCHIVE),
          f"exit={code}")
    recorded = next((item for item in files_manifest.get("files", {}).get("entries", [])
                     if item.get("path") == probe_member), {})
    check("上传件在归档里的 sha256 与磁盘一致", recorded.get("sha256") == sha256_bytes(probe_bytes),
          f"manifest {str(recorded.get('sha256'))[:12]} / 磁盘 {sha256_bytes(probe_bytes)[:12]}")
    UPLOAD_PROBE.unlink(missing_ok=True)
    code, output = run("restore", str(FILES_ARCHIVE), "--database", SCRATCH_DB, "--yes",
                       "--drop-existing", "--files-target", str(RESTORE_FILES), "--overwrite-files")
    restored_file = RESTORE_FILES / "verify-backup-probe.txt"
    check("恢复把删掉的上传件放回来了", code == 0 and restored_file.exists(), f"exit={code}")
    check("放回来的上传件内容一致",
          restored_file.exists() and restored_file.read_bytes() == probe_bytes)
    check("没有把探针文件写回真实 data/uploads", not UPLOAD_PROBE.exists())

    # ---------------------------------------------------------------- 5. 负例
    rewrite_archive(ARCHIVE, TAMPERED, replace=(b"analyst", b"an4lyst"))
    code, output = run("verify", str(TAMPERED))
    check("被改过的数据成员会被 verify 抓出来", code == 1 and "sha256 对不上" in output, f"exit={code}")

    def add_column(manifest: dict) -> None:
        manifest["database"]["entries"][0]["columns"].append("column_that_the_model_does_not_have")

    rewrite_archive(ARCHIVE, EXTRA_COLUMN, mutate_manifest=add_column)
    code, output = run("restore", str(EXTRA_COLUMN), "--database", SCRATCH_DB, "--yes", "--drop-existing")
    check("清单里多一列时拒绝恢复并点名",
          code == 1 and "当前模型没有的列" in output and "column_that_the_model_does_not_have" in output,
          f"exit={code}")

    def break_digest(manifest: dict) -> None:
        manifest["database"]["entries"][0]["sha256"] = "0" * 64

    rewrite_archive(ARCHIVE, BAD_DIGEST, mutate_manifest=break_digest)
    code, output = run("restore", str(BAD_DIGEST), "--database", SCRATCH_DB, "--yes")
    check("清单里的 sha256 对不上时拒绝恢复", code == 1 and "归档自身有问题" in output, f"exit={code}")

    code, output = run("restore", str(ARCHIVE), "--database", SCRATCH_DB, "--yes", "--drop-existing")
    repeat_counts = row_counts(SCRATCH_DB, source_tables)
    check("同一个归档可以反复恢复（先清空再装）",
          code == 0 and repeat_counts == live_counts, f"exit={code}")

    # ---------------------------------------------------------------- 6. 收尾
    drop_database(SCRATCH_DB)
    for path in PROBE_ARCHIVES:
        path.unlink(missing_ok=True)
    UPLOAD_PROBE.unlink(missing_ok=True)
    if RESTORE_FILES.exists():
        shutil.rmtree(RESTORE_FILES)
    check("收尾：临时库与探针文件都已清掉",
          SCRATCH_DB not in database_names() and not UPLOAD_PROBE.exists()
          and not any(path.exists() for path in PROBE_ARCHIVES))

    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 项通过")
    if passed != len(RESULTS):
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  失败：{name} —— {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 —— 顶层兜底：把异常打成人话再退出
        print(f"\n异常终止：{type(exc).__name__}: {exc}")
        print("检查：数据库在不在（docker compose up -d postgres）、.env 里的 DATABASE_URL 对不对")
        sys.exit(1)
