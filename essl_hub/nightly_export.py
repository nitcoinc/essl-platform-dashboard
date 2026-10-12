"""Nightly read-only export to R2 (plan KTD14, KTD15, U3).

Runs as ``python -m essl_hub.nightly_export [--once]``. Uses its own replica
file and a read-only Turso token; never runs schema migrations (KTD8).
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config import TIMEZONE_NAME, db_path
from .db import SCHEMA_VERSION, Database, remove_replica_files

_TZ = ZoneInfo(TIMEZONE_NAME)
_logger = logging.getLogger(__name__)

KEY_PREFIX = "nightly/"
TEMP_DIR_NAME = "export-tmp"
SCRUBBED_TABLES = ("sessions", "login_codes")
STATUS_CAP = 30
ERROR_MAX_CHARS = 500
DEFAULT_EXPORT_HOUR = 2


@dataclass(frozen=True)
class R2Config:
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


Uploader = Callable[[str, str, R2Config], None]


def _now() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


def r2_config() -> R2Config | None:
    """Bucket-scoped export credentials (R2_EXPORT_*), or None when any are unset."""
    values = [
        (os.getenv(name) or "").strip()
        for name in (
            "R2_ACCOUNT_ID",
            "R2_BUCKET",
            "R2_EXPORT_ACCESS_KEY_ID",
            "R2_EXPORT_SECRET_ACCESS_KEY",
        )
    ]
    if not all(values):
        return None
    return R2Config(*values)


def upload_to_r2(path: str, key: str, cfg: R2Config) -> None:
    import boto3  # lazy: only the real upload needs it
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        region_name="auto",
        config=Config(
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
    client.upload_file(path, cfg.bucket, key)


def object_key(at: datetime) -> str:
    return f"{KEY_PREFIX}{at.strftime('%Y%m%dT%H%M%S')}.db.gz"


def export_hour() -> int:
    value = os.getenv("ESSL_EXPORT_HOUR")
    if value in (None, ""):
        return DEFAULT_EXPORT_HOUR
    hour = int(value)
    if not 0 <= hour <= 23:
        raise ValueError("ESSL_EXPORT_HOUR must be 0-23")
    return hour


def export_status_path(database_path: str | None = None) -> str:
    return os.getenv("ESSL_EXPORT_STATUS_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(database_path or db_path())), "export-status.json"
    )


def open_export_database() -> Database:
    """Replica of the primary with the read-only export token (KTD14)."""
    url = os.getenv("TURSO_DATABASE_URL") or None
    token = os.getenv("TURSO_EXPORT_AUTH_TOKEN") or None
    if url and not token:
        raise RuntimeError("TURSO_EXPORT_AUTH_TOKEN must be set when TURSO_DATABASE_URL is set")
    if url:
        return Database(db_path(), sync_url=url, auth_token=token, read_only=True)
    return Database(db_path(), sync_url="", auth_token=None)


# --- status file (KTD12) ---------------------------------------------------


def load_export_status(path: str) -> list[dict]:
    """Status entries, oldest first. Missing or corrupt reads as []."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _write_json_atomic(path: str, payload: object) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".export-status-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_status(path: str, entry: dict) -> None:
    entries = load_export_status(path)
    entries.append(entry)
    _write_json_atomic(path, entries[-STATUS_CAP:])


# --- export ----------------------------------------------------------------


class ExportError(Exception):
    pass


def _build_snapshot(source_path: str, work_dir: Path) -> Path:
    """Backup-API copy, scrub, VACUUM, integrity_check, gzip. Returns .gz path."""
    raw = work_dir / "export.db"
    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    copy = sqlite3.connect(raw, isolation_level=None)
    try:
        version = copy.execute("PRAGMA user_version").fetchone()[0]
        # Turso cannot write the pragma; settings.schema_version carries it.
        if copy.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
        ).fetchone():
            row = copy.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
            if row:
                version = max(version, int(row[0]))
        if version < SCHEMA_VERSION:
            raise ExportError(
                f"schema version {version} is behind {SCHEMA_VERSION}; skipping export"
            )
        for table in SCRUBBED_TABLES:
            copy.execute(f"DELETE FROM {table}")
        copy.execute("VACUUM")
        result = copy.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ExportError(f"integrity_check failed: {result}")
    finally:
        copy.close()

    packed = work_dir / "export.db.gz"
    with open(raw, "rb") as plain, gzip.open(packed, "wb") as gz:
        shutil.copyfileobj(plain, gz)
    raw.unlink()
    return packed


def run_export(
    database: Database,
    cfg: R2Config | None,
    *,
    status_path: str,
    uploader: Uploader | None = None,
) -> bool:
    """One export run; appends a status entry and returns True on success."""
    uploader = uploader or upload_to_r2
    started = _now()
    entry: dict = {"started": started.isoformat(sep=" "), "key": None, "size": None, "error": None}
    work_dir = Path(database.path).resolve().parent / TEMP_DIR_NAME
    try:
        if cfg is None:
            raise ExportError("R2 export credentials are not configured")
        if not database.sync():
            raise ExportError(f"sync failed: {database.last_sync_error}")
        database.close()  # KTD14: release libsql before the file copy
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True)
        packed = _build_snapshot(database.path, work_dir)
        key = object_key(started)
        size = packed.stat().st_size
        uploader(str(packed), key, cfg)
        entry.update(status="ok", key=key, size=size)
    except Exception as exc:  # noqa: BLE001 - recorded in the status file
        message = str(exc) or type(exc).__name__
        _logger.error("Nightly export failed: %s", message)
        entry.update(status="failed", error=message[:ERROR_MAX_CHARS])
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    entry["finished"] = _now().isoformat(sep=" ")
    append_status(status_path, entry)
    return entry["status"] == "ok"


# --- scheduler -------------------------------------------------------------


def next_run(now: datetime, hour: int) -> datetime:
    """Next HH:00 IST strictly after `now` (naive IST or aware)."""
    if now.tzinfo is not None:
        now = now.astimezone(_TZ).replace(tzinfo=None)
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _export_once() -> bool:
    # Fresh replica every run: a once-a-night bootstrap is seconds, and it
    # sidesteps replicas that silently stop advancing (see db.FRESHNESS_SQL).
    database = open_export_database()
    if database.replica:  # never in local mode: there the file IS the database
        remove_replica_files(database.path)
    try:
        return run_export(database, r2_config(), status_path=export_status_path(database.path))
    finally:
        database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="essl_hub.nightly_export")
    parser.add_argument("--once", action="store_true", help="run one export and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.once:
        return 0 if _export_once() else 1

    hour = export_hour()
    while True:
        target = next_run(_now(), hour)
        _logger.info("Next export at %s IST", target)
        while (remaining := (target - _now()).total_seconds()) > 0:
            time.sleep(min(remaining, 300))
        _export_once()


if __name__ == "__main__":
    sys.exit(main())
