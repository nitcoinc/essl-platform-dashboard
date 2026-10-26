"""Backups to R2 and the database lifecycle commands (plan KTD1-KTD5, KTD8, KTD9).

    python -m essl_hub.backup                 # loop: snapshot every ESSL_BACKUP_MINUTES
    python -m essl_hub.backup init            # new empty database
    python -m essl_hub.backup restore [--key KEY] [--drill]
    python -m essl_hub.backup list | now | status | has-backups | adopt [--yes] | import-turso

Only this module holds R2 credentials. It never talks to devices. Every cycle
and every command that touches the database file or the bucket holds an
exclusive flock on ``backup.lock`` next to the database and works in its own
temp folder, so ``now`` cannot collide with the running loop.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator, Protocol
from zoneinfo import ZoneInfo

from . import config
from .config import TIMEZONE_NAME, db_path
from .db import SCHEMA_VERSION, Database

_TZ = ZoneInfo(TIMEZONE_NAME)
_logger = logging.getLogger(__name__)

RECENT_PREFIX = "recent/"
NIGHTLY_PREFIX = "nightly/"
LATEST_KEY = "latest.json"
SUFFIX = ".db.gz"
SCRUBBED_TABLES = ("sessions", "login_codes", "fix_report_links", "mail_outbox")
STATUS_CAP = 30
ERROR_MAX_CHARS = 500
DEFAULT_EXPORT_HOUR = 2
SIDECARS = ("", "-wal", "-shm")
DATABASE_ID_KEY = "database_id"
SETUP_HINT = (
    "database not set up: run `python -m essl_hub.backup init` (new install), "
    "`restore` (from R2) or `import-turso` (one-time cutover)"
)


class BackupError(Exception):
    pass


class CommandError(BackupError):
    """An admin command refused or failed; the message says what to do."""


# --- R2 --------------------------------------------------------------------


@dataclass(frozen=True)
class R2Config:
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str

    @property
    def endpoint(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    modified: datetime | None


class R2Client(Protocol):
    def put_file(self, path: str, key: str) -> None: ...
    def put_bytes(self, key: str, data: bytes) -> None: ...
    def get_bytes(self, key: str) -> bytes | None: ...  # None only on 404
    def download(self, key: str, dest: str) -> None: ...
    def list(self, prefix: str) -> list[ObjectInfo]: ...
    def delete(self, keys: list[str]) -> None: ...


def r2_config() -> R2Config | None:
    """Bucket-scoped credentials (R2_EXPORT_*), or None when any are unset."""
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


class BotoR2Client:
    def __init__(self, cfg: R2Config):
        import boto3  # lazy: only real R2 access needs it
        from botocore.config import Config

        self.bucket = cfg.bucket
        self._s3 = boto3.client(
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

    def put_file(self, path: str, key: str) -> None:
        self._s3.upload_file(path, self.bucket, key)

    def put_bytes(self, key: str, data: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get_bytes(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            response = self._s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if error.get("Code") in ("NoSuchKey", "404") or status == 404:
                return None
            raise
        return response["Body"].read()

    def download(self, key: str, dest: str) -> None:
        self._s3.download_file(self.bucket, key, dest)

    def list(self, prefix: str) -> list[ObjectInfo]:
        out: list[ObjectInfo] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                modified = item.get("LastModified")
                if modified is not None:
                    modified = modified.astimezone(_TZ).replace(tzinfo=None)
                out.append(ObjectInfo(item["Key"], int(item.get("Size", 0)), modified))
        return out

    def delete(self, keys: list[str]) -> None:
        for start in range(0, len(keys), 1000):
            chunk = keys[start:start + 1000]
            self._s3.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
            )


def make_client() -> R2Client | None:
    cfg = r2_config()
    return BotoR2Client(cfg) if cfg is not None else None


# --- keys and schedule -----------------------------------------------------


def _now() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


def _ts(at: datetime | None) -> str | None:
    return at.isoformat(sep=" ", timespec="seconds") if at else None


def recent_key(at: datetime) -> str:
    return f"{RECENT_PREFIX}{at.strftime('%Y%m%dT%H%M%S')}{SUFFIX}"


def nightly_key(at: datetime) -> str:
    return f"{NIGHTLY_PREFIX}{at.strftime('%Y%m%d')}{SUFFIX}"


def key_time(key: str) -> datetime | None:
    """IST time encoded in a snapshot key (recent/<ts>, nightly/<date> or the
    old nightly/<ts>); None for anything else, which pruning leaves alone."""
    name = key.rsplit("/", 1)[-1]
    if not name.endswith(SUFFIX):
        return None
    stem = name[: -len(SUFFIX)]
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(stem, fmt)
        except ValueError:
            continue
    return None


def export_hour() -> int:
    value = os.getenv("ESSL_EXPORT_HOUR")
    if value in (None, ""):
        return DEFAULT_EXPORT_HOUR
    hour = int(value)
    if not 0 <= hour <= 23:
        raise ValueError("ESSL_EXPORT_HOUR must be 0-23")
    return hour


_env_export_hour = export_hour


def prune(client: R2Client, now: datetime, *, keep_hours: int, keep_days: int) -> list[str]:
    """Delete recent/ older than keep_hours and nightly/ older than keep_days.
    Only keys with a parseable timestamp are touched; latest.json never is."""
    doomed: list[str] = []
    for prefix, keep in (
        (RECENT_PREFIX, timedelta(hours=keep_hours)),
        (NIGHTLY_PREFIX, timedelta(days=keep_days)),
    ):
        for obj in client.list(prefix):
            at = key_time(obj.key)
            if obj.key != LATEST_KEY and at is not None and now - at > keep:
                doomed.append(obj.key)
    if doomed:
        client.delete(doomed)
    return doomed


# --- status file -----------------------------------------------------------


def backup_status_path(database_path: str | None = None) -> str:
    return os.getenv("ESSL_BACKUP_STATUS_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(database_path or db_path())), "backup-status.json"
    )


def _empty_status() -> dict:
    return {
        "last_ok_at": None,
        "last_upload_at": None,
        "last_key": None,
        "last_sha256": None,
        "last_error": None,
        "blocked": False,
        "entries": [],
    }


def load_status(path: str) -> dict:
    """The status dict; missing or corrupt reads as empty."""
    status = _empty_status()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return status
    if not isinstance(data, dict):
        return status
    status.update(data)
    if not isinstance(status.get("entries"), list):
        status["entries"] = []
    status["entries"] = [e for e in status["entries"] if isinstance(e, dict)]
    return status


def status_entries(path: str) -> list[dict]:
    """Cycle entries, oldest first (status ok/failed/blocked/not_set_up)."""
    return load_status(path)["entries"]


def _write_json_atomic(path: str, payload: object) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".backup-status-", suffix=".tmp", dir=directory)
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


# --- locking and temp folders ----------------------------------------------


def _data_dir(database_path: str) -> str:
    return os.path.dirname(os.path.abspath(database_path))


@contextmanager
def backup_lock(database_path: str) -> Iterator[None]:
    """Exclusive flock on backup.lock next to the database (blocks until free)."""
    directory = _data_dir(database_path)
    os.makedirs(directory, exist_ok=True)
    fd = os.open(os.path.join(directory, "backup.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def _work_dir(database_path: str, prefix: str) -> Iterator[Path]:
    """Private temp folder on the database's filesystem (rename stays atomic)."""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=_data_dir(database_path)))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _existing_files(database_path: str) -> list[str]:
    return [database_path + s for s in SIDECARS if os.path.exists(database_path + s)]


def _refuse_existing(database_path: str) -> None:
    found = _existing_files(database_path)
    if found:
        raise CommandError(
            f"refusing: {', '.join(found)} already exists. With the stack stopped, "
            f"move {database_path}, {database_path}-wal and {database_path}-shm aside first "
            "(a leftover -wal would replay another database's pages onto the new file)."
        )


# --- snapshot --------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    path: Path
    database_id: str | None
    punches: int


def _schema_version(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
    ).fetchone():
        row = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
        if row:
            version = max(version, int(row[0]))
    return version


def _read_setting(conn: sqlite3.Connection, key: str) -> str | None:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
    ).fetchone():
        return None
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row and row[0] else None


def _write_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _count_punches(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM punches").fetchone()[0])


def _gzip_deterministic(src: Path, dest: Path) -> None:
    """mtime=0 and no filename, so identical databases give identical bytes."""
    with open(src, "rb") as plain, open(dest, "wb") as raw_out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_out, mtime=0) as gz:
            shutil.copyfileobj(plain, gz)


def _open_source(source_path: str) -> sqlite3.Connection:
    # Read-only first; with the stack stopped and no -shm, a read-only open of
    # a WAL file can fail, and a read-write open is still safe for backup().
    try:
        conn = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.OperationalError:
        return sqlite3.connect(f"file:{source_path}?mode=rw", uri=True, timeout=5)


def _build_snapshot(source_path: str, work_dir: Path) -> Snapshot:
    """Backup-API copy, scrub, VACUUM, integrity_check, deterministic gzip."""
    work_dir = Path(work_dir)
    raw = work_dir / "export.db"
    src = _open_source(source_path)
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
        version = _schema_version(copy)
        if version < SCHEMA_VERSION:
            raise BackupError(
                f"schema version {version} is behind {SCHEMA_VERSION}; skipping backup"
            )
        for table in SCRUBBED_TABLES:
            copy.execute(f"DELETE FROM {table}")
        copy.execute("PRAGMA journal_mode=DELETE")
        copy.execute("VACUUM")
        result = copy.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(f"integrity_check failed: {result}")
        database_id = _read_setting(copy, DATABASE_ID_KEY)
        punches = _count_punches(copy)
    finally:
        copy.close()

    packed = work_dir / "export.db.gz"
    _gzip_deterministic(raw, packed)
    raw.unlink()
    return Snapshot(packed, database_id, punches)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_latest(client: R2Client) -> dict | None:
    """latest.json, None only when it does not exist (404). Other errors raise."""
    raw = client.get_bytes(LATEST_KEY)
    if raw is None:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise BackupError("latest.json is not an object")
    return data


def _latest_payload(database_id: str, key: str | None, punches: int, at: datetime) -> bytes:
    return json.dumps(
        {"database_id": database_id, "key": key, "punches": punches, "at": _ts(at)},
        sort_keys=True,
    ).encode()


# --- one cycle -------------------------------------------------------------


def run_cycle(
    database_path: str,
    client: R2Client | None,
    *,
    status_path: str | None = None,
    now: datetime | None = None,
    keep_hours: int | None = None,
    keep_days: int | None = None,
    export_hour: int | None = None,
) -> str:
    """One backup cycle. Returns ok, skipped, blocked, failed or not_set_up.
    Never deletes or modifies local database files."""
    status_path = status_path or backup_status_path(database_path)
    keep_hours = keep_hours if keep_hours is not None else config.backup_keep_hours()
    keep_days = keep_days if keep_days is not None else config.backup_keep_days()
    hour = export_hour if export_hour is not None else _env_export_hour()

    with backup_lock(database_path):
        started = now or _now()
        status = load_status(status_path)
        entry: dict = {"started": _ts(started), "status": "failed", "key": None,
                       "size": None, "error": None}
        result = "failed"
        blocked = False
        try:
            if client is None:
                raise BackupError("R2 credentials are not configured (R2_* variables)")
            if not os.path.exists(database_path):
                result = "not_set_up"
                raise BackupError(SETUP_HINT)
            with _work_dir(database_path, ".backup-") as work:
                snap = _build_snapshot(database_path, work)
                if not snap.database_id:
                    result, blocked = "blocked", True
                    raise BackupError(
                        "this database has no database_id; run "
                        "`python -m essl_hub.backup adopt` to make it the backups' owner"
                    )
                sha = _sha256(snap.path)
                size = snap.path.stat().st_size
                skip = sha == status.get("last_sha256")
                nightly = nightly_key(started)
                nightly_due = started.hour >= hour and not any(
                    o.key == nightly for o in client.list(nightly)
                )
                if skip and not nightly_due:
                    result = "skipped"
                    entry.update(status="ok", skipped=True, key=status.get("last_key"), size=size)
                else:
                    try:
                        latest = _read_latest(client)
                    except Exception as exc:  # noqa: BLE001
                        raise BackupError(f"could not read latest.json, skipping upload: {exc}")
                    if latest is not None and latest.get("database_id") != snap.database_id:
                        result, blocked = "blocked", True
                        raise BackupError(
                            "R2 backups belong to a different database "
                            f"({latest.get('database_id')}); run `python -m essl_hub.backup "
                            "restore` to take the bucket's database, or `adopt` to make this "
                            "database the owner"
                        )
                if skip and nightly_due:
                    client.put_file(str(snap.path), nightly)
                    result = "skipped"
                    entry.update(status="ok", skipped=True, key=status.get("last_key"),
                                 size=size, nightly=nightly)
                elif not skip:
                    key = recent_key(started)
                    client.put_file(str(snap.path), key)
                    if nightly_due:
                        client.put_file(str(snap.path), nightly)
                    client.put_bytes(
                        LATEST_KEY,
                        _latest_payload(snap.database_id, key, snap.punches, started),
                    )
                    status.update(last_upload_at=_ts(started), last_key=key, last_sha256=sha)
                    result = "ok"
                    entry.update(status="ok", key=key, size=size)
            status.update(last_ok_at=_ts(started), last_error=None, blocked=False)
            if client is not None and result == "ok":
                try:
                    prune(client, started, keep_hours=keep_hours, keep_days=keep_days)
                except Exception as exc:  # noqa: BLE001 - retried next cycle
                    message = f"prune failed: {exc}"[:ERROR_MAX_CHARS]
                    _logger.error("%s", message)
                    status["last_error"] = message
                    entry["error"] = message
        except Exception as exc:  # noqa: BLE001 - recorded in the status file
            message = (str(exc) or type(exc).__name__)[:ERROR_MAX_CHARS]
            _logger.error("Backup cycle %s: %s", result, message)
            if result in ("ok", "skipped"):
                result = "failed"
            entry.update(status=result, error=message)
            status.update(last_error=message, blocked=blocked)
        entry["finished"] = _ts(now or _now())
        # The web container holds no R2 credentials (KTD9), so the health page
        # learns whether backups are configured, and their interval, from here.
        status.update(r2_configured=client is not None, interval_minutes=config.backup_minutes())
        status["entries"] = (status["entries"] + [entry])[-STATUS_CAP:]
        _write_json_atomic(status_path, status)
        return result


# --- admin commands (U2) ---------------------------------------------------


def cmd_init(database_path: str) -> str:
    with backup_lock(database_path):
        _refuse_existing(database_path)
        database = Database(database_path)
        try:
            database.initialize(config.DEVICES)
            database_id = uuid.uuid4().hex
            database.set_setting(DATABASE_ID_KEY, database_id)
        finally:
            database.close()
    print(f"Created {database_path} (database_id {database_id}).")
    print("Next: start the stack, then finish setup on the setup page or sign in as "
          "ESSL_BOOTSTRAP_ADMIN_EMAIL.")
    return database_id


def _snapshot_keys(client: R2Client) -> list[ObjectInfo]:
    objects = client.list(RECENT_PREFIX) + client.list(NIGHTLY_PREFIX)
    return [o for o in objects if o.key.endswith(SUFFIX)]


def _verify_restored(path: str) -> str | None:
    """integrity_check, foreign_key_check and schema check; returns database_id."""
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error as exc:
        raise CommandError(f"snapshot does not open: {exc}") from None
    try:
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise CommandError(f"snapshot is not a valid database: {exc}") from None
        if result != "ok":
            raise CommandError(f"snapshot failed integrity_check: {result}")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise CommandError(f"snapshot failed foreign_key_check ({len(problems)} rows)")
        version = _schema_version(conn)
        if version > SCHEMA_VERSION:
            raise CommandError(
                f"snapshot schema v{version} is newer than this code (v{SCHEMA_VERSION}); "
                "upgrade the app before restoring it"
            )
        return _read_setting(conn, DATABASE_ID_KEY)
    finally:
        conn.close()


def cmd_restore(
    database_path: str, client: R2Client | None, key: str | None = None, *, drill: bool = False
) -> str:
    """drill=True gives the restored file a fresh random database_id so it can
    never pass the bucket guard and upload over production backups."""
    with backup_lock(database_path):
        _refuse_existing(database_path)
        if client is None:
            raise CommandError("R2 credentials are not configured (R2_* variables)")
        if key is None:
            recent = [o for o in client.list(RECENT_PREFIX) if key_time(o.key)]
            pool = recent or [o for o in client.list(NIGHTLY_PREFIX) if key_time(o.key)]
            if not pool:
                raise CommandError("no snapshots found in R2")
            key = max(pool, key=lambda o: (key_time(o.key), o.key)).key
        with _work_dir(database_path, ".restore-") as work:
            packed = work / "download.db.gz"
            fd, plain = tempfile.mkstemp(prefix="restore-", suffix=".db", dir=work)
            os.fchmod(fd, 0o600)
            try:
                client.download(key, str(packed))
                with os.fdopen(fd, "wb") as out, gzip.open(packed, "rb") as gz:
                    shutil.copyfileobj(gz, out)
                    out.flush()
                    os.fsync(out.fileno())
            except (OSError, EOFError, gzip.BadGzipFile) as exc:
                raise CommandError(f"could not unpack {key}: {exc}") from None
            database_id = _verify_restored(plain)
            if drill:
                database_id = uuid.uuid4().hex
                conn = sqlite3.connect(plain)
                try:
                    with conn:
                        _write_setting(conn, DATABASE_ID_KEY, database_id)
                finally:
                    conn.close()
            _refuse_existing(database_path)
            os.rename(plain, database_path)
    print(f"Restored {key} to {database_path}.")
    if drill:
        print(f"Drill: new database_id {database_id}; this copy can never upload to R2.")
    elif database_id:
        print(f"database_id {database_id} kept; backups continue on the next cycle.")
    else:
        print("This snapshot has no database_id: run `python -m essl_hub.backup adopt` "
              "before backups resume.")
    print("Note: everyone signs in again (sessions are not in snapshots).")
    return key


def cmd_has_backups(client: R2Client | None) -> bool:
    """True when R2 already holds latest.json (an install must restore, not init)."""
    if client is None:
        raise CommandError("R2 credentials are not configured (R2_* variables)")
    try:
        return _read_latest(client) is not None
    except Exception as exc:  # noqa: BLE001
        raise CommandError(f"could not read latest.json: {exc}") from None


def cmd_list(client: R2Client | None) -> list[ObjectInfo]:
    if client is None:
        raise CommandError("R2 credentials are not configured (R2_* variables)")
    objects = sorted(_snapshot_keys(client), key=lambda o: o.key)
    for obj in objects:
        when = _ts(key_time(obj.key)) or "?"
        print(f"{obj.key}\t{obj.size / 1_048_576:.1f} MB\t{when} IST")
    if not objects:
        print("no snapshots")
    return objects


def cmd_status(database_path: str, client: R2Client | None) -> dict:
    status = load_status(backup_status_path(database_path))
    local_id = None
    if os.path.exists(database_path):
        conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            local_id = _read_setting(conn, DATABASE_ID_KEY)
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    remote_id: str | None = None
    remote_note = "R2 not configured"
    if client is not None:
        try:
            latest = _read_latest(client)
            remote_id = latest.get("database_id") if latest else None
            remote_note = remote_id or "no latest.json yet"
        except Exception as exc:  # noqa: BLE001
            remote_note = f"unreadable ({exc})"
    last_cycle = status["entries"][-1].get("finished") if status["entries"] else None
    next_due = None
    if last_cycle:
        try:
            next_due = _ts(datetime.fromisoformat(last_cycle)
                           + timedelta(minutes=config.backup_minutes()))
        except ValueError:
            pass
    match = "n/a" if not (local_id and remote_id) else ("yes" if local_id == remote_id else "NO")
    lines = [
        ("database", database_path if local_id or os.path.exists(database_path)
         else f"{database_path} (missing)"),
        ("database_id (local)", local_id or "none"),
        ("database_id (R2)", remote_note),
        ("database_id match", match),
        ("last ok", status.get("last_ok_at") or "never"),
        ("last upload", status.get("last_upload_at") or "never"),
        ("last key", status.get("last_key") or "-"),
        ("next due", next_due or "next loop start"),
        ("blocked", "yes" if status.get("blocked") else "no"),
        ("last error", status.get("last_error") or "-"),
    ]
    for label, value in lines:
        print(f"{label:22} {value}")
    return status


def cmd_adopt(
    database_path: str,
    client: R2Client | None,
    *,
    confirm: Callable[[], bool],
) -> str:
    if client is None:
        raise CommandError("R2 credentials are not configured (R2_* variables)")
    if not os.path.exists(database_path):
        raise CommandError(SETUP_HINT)
    with backup_lock(database_path):
        print(f"This makes {database_path} the owner of the R2 backups; snapshots from "
              "any other database are no longer protected from being replaced.")
        if not confirm():
            raise CommandError("adopt cancelled")
        database = Database(database_path, must_exist=True)
        try:
            with database.connect() as conn:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key=?", (DATABASE_ID_KEY,)
                ).fetchone()
                punches = int(
                    conn.execute("SELECT COUNT(*) AS n FROM punches").fetchone()["n"]
                )
            database_id = row["value"] if row and row["value"] else None
            if not database_id:
                database_id = uuid.uuid4().hex
                database.set_setting(DATABASE_ID_KEY, database_id)
        finally:
            database.close()
        status_path = backup_status_path(database_path)
        status = load_status(status_path)
        client.put_bytes(
            LATEST_KEY, _latest_payload(database_id, None, punches, _now())
        )
        status.update(blocked=False, last_error=None, last_sha256=None)
        _write_json_atomic(status_path, status)
    print(f"Adopted: R2 backups now belong to database_id {database_id}.")
    return database_id


def _libsql_connect(path: str, sync_url: str, auth_token: str):
    import libsql  # the only Turso code left; removed with the libsql driver

    return libsql.connect(path, sync_url=sync_url, auth_token=auth_token)


def cmd_import_turso(
    database_path: str,
    url: str | None,
    token: str | None,
    *,
    connect: Callable | None = None,
) -> str:
    """One-time cutover: bootstrap a replica, checkpoint, verify, move in (KTD8)."""
    with backup_lock(database_path):
        _refuse_existing(database_path)
        if not url or not token:
            raise CommandError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set")
        connect = connect or _libsql_connect
        with _work_dir(database_path, ".import-") as work:
            replica_path = str(work / "import.db")
            replica = connect(replica_path, sync_url=url, auth_token=token)
            try:
                replica.sync()
                remote_punches = _count_punches(replica)
            finally:
                replica.close()
            # libsql keeps recent frames in -wal: fold them into the file
            # before any sidecar is deleted.
            conn = sqlite3.connect(replica_path, timeout=0, isolation_level=None)
            try:
                conn.execute("PRAGMA busy_timeout=0")
                busy, _, _ = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if busy:
                    raise CommandError("could not checkpoint the replica's -wal (busy)")
                conn.execute("PRAGMA journal_mode=DELETE")
            finally:
                conn.close()
            for name in os.listdir(work):
                if name.startswith("import.db") and name != "import.db":
                    os.unlink(work / name)
            conn = sqlite3.connect(replica_path, isolation_level=None)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise CommandError(f"imported file failed integrity_check: {result}")
                version = _schema_version(conn)
                if version > SCHEMA_VERSION:
                    raise CommandError(
                        f"Turso schema v{version} is newer than this code (v{SCHEMA_VERSION})"
                    )
                local_punches = _count_punches(conn)
                if local_punches != remote_punches:
                    raise CommandError(
                        f"punch count mismatch: local {local_punches}, remote {remote_punches}"
                    )
                database_id = uuid.uuid4().hex
                _write_setting(conn, DATABASE_ID_KEY, database_id)
            finally:
                conn.close()
            os.chmod(replica_path, 0o600)
            _refuse_existing(database_path)
            os.rename(replica_path, database_path)
    print(f"Imported {remote_punches} punches from Turso into {database_path} "
          f"(database_id {database_id}).")
    print("Next: docker compose up -d --build --remove-orphans")
    return database_id


# --- entry point -----------------------------------------------------------


def run_loop() -> None:
    path = db_path()
    interval = config.backup_minutes() * 60
    while True:
        started = time.monotonic()
        try:
            run_cycle(path, make_client())
        except Exception:  # noqa: BLE001 - the loop must survive anything
            _logger.exception("Backup cycle crashed")
        time.sleep(max(1.0, interval - (time.monotonic() - started)))


def _ask_yes() -> bool:
    try:
        return input("Type 'adopt' to continue: ").strip().lower() == "adopt"
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="essl_hub.backup")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="create a new empty database")
    restore = sub.add_parser("restore", help="restore the latest (or --key) snapshot")
    restore.add_argument("--key", help="exact R2 key, e.g. recent/20261014T101500.db.gz")
    restore.add_argument("--drill", action="store_true",
                         help="restore drill: give the copy a new database_id (never uploads)")
    sub.add_parser("list", help="list snapshots in R2")
    sub.add_parser("has-backups", help="exit 0 if R2 has latest.json, 3 if not")
    sub.add_parser("now", help="run one backup cycle now")
    sub.add_parser("status", help="show backup status")
    adopt = sub.add_parser("adopt", help="make this database the backups' owner")
    adopt.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    sub.add_parser("import-turso", help="one-time import from Turso (cutover)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    path = db_path()
    try:
        if args.command is None:
            run_loop()
        elif args.command == "init":
            cmd_init(path)
        elif args.command == "restore":
            cmd_restore(path, make_client(), key=args.key, drill=args.drill)
        elif args.command == "has-backups":
            return 0 if cmd_has_backups(make_client()) else 3
        elif args.command == "list":
            cmd_list(make_client())
        elif args.command == "now":
            result = run_cycle(path, make_client())
            print(f"backup: {result}")
            return 0 if result in ("ok", "skipped") else 1
        elif args.command == "status":
            cmd_status(path, make_client())
        elif args.command == "adopt":
            cmd_adopt(path, make_client(), confirm=(lambda: True) if args.yes else _ask_yes)
        elif args.command == "import-turso":
            cmd_import_turso(
                path, os.getenv("TURSO_DATABASE_URL"), os.getenv("TURSO_AUTH_TOKEN")
            )
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
