from __future__ import annotations

import json
import logging
import os
import socket
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import libsql

from .config import DeviceConfig, TIMEZONE_NAME, db_path, turso_settings

_TZ = ZoneInfo(TIMEZONE_NAME)
_logger = logging.getLogger(__name__)

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
SCHEMA_VERSION = 3

# Seconds the TCP probe waits before calling the primary unreachable. libsql's
# own replica connect can block ~134s holding the GIL on a black-holed address
# (U8), so it is never attempted without a successful probe first.
PROBE_TIMEOUT_SECONDS = 3.0

# libsql exposes no declared column types (U8), so read-side conversion is by
# column name. Every TIMESTAMP column in the schema is listed here.
TIMESTAMP_COLUMNS = frozenset(
    {
        "last_attempt",
        "last_success",
        "connection_updated_at",
        "created_at",
        "updated_at",
        "event_timestamp",
        "collected_at",
        "expires_at",
        "revoked_at",
        "consumed_at",
        "last_used_at",
        "synced_at",
    }
)
JSON_COLUMNS = frozenset({"raw_json"})


class RuleViolation(ValueError):
    """A guarded write broke a data rule (last admin, manager cycle)."""


class IntegrityError(Exception):
    """A UNIQUE / FOREIGN KEY / CHECK constraint rejected a write (KTD6)."""


class OfflineError(Exception):
    """A write could not reach the Turso primary, or the database is unavailable
    because it is offline with no local replica (KTD6, KTD11)."""


# libsql raises plain ValueError for constraint failures and network failures
# alike (U8), so the message is the only thing to classify on.
_OFFLINE_MARKERS = (
    "sync error",
    "http dispatch error",
    "error trying to connect",
    "connection refused",
    "connection reset",
    "connection closed",
    "dns error",
    "timed out",
    "network is unreachable",
)


def classify_error(exc: BaseException) -> Exception | None:
    """Map a libsql error to IntegrityError / OfflineError, or None to re-raise it."""
    message = str(exc)
    lowered = message.lower()
    if "constraint failed" in lowered:
        return IntegrityError(message)
    if any(marker in lowered for marker in _OFFLINE_MARKERS):
        return OfflineError(message)
    return None


def _adapt_datetime(value: datetime) -> str:
    """Store every timestamp as naive IST text in one canonical, sortable form."""
    if value.tzinfo is not None:
        value = value.astimezone(_TZ).replace(tzinfo=None)
    return value.strftime(TIMESTAMP_FORMAT)


def _bind(params: Iterable[object]) -> tuple[object, ...]:
    """libsql has no adapters (KTD5): format datetimes and serialise dicts here."""
    bound = []
    for value in params:
        if isinstance(value, datetime):
            value = _adapt_datetime(value)
        elif isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        bound.append(value)
    return tuple(bound)


def _convert(name: str, value: object) -> object:
    if not isinstance(value, str):
        return value
    if name in TIMESTAMP_COLUMNS:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    if name in JSON_COLUMNS:
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _dict_row(names: list[str], row: tuple | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {name: _convert(name, value) for name, value in zip(names, row)}


def _parse_timestamp(value: object) -> datetime | None:
    """Normalise a collector timestamp; None when empty, ValueError when malformed."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip())
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_TZ).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def primary_reachable(sync_url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """Fast TCP probe of the sync URL's host, so a dead network costs seconds,
    not a GIL-holding libsql connect."""
    parsed = urlparse(sync_url)
    if not parsed.hostname:
        return False
    port = parsed.port or (80 if parsed.scheme in ("http", "ws") else 443)
    try:
        socket.create_connection((parsed.hostname, port), timeout=timeout).close()
    except OSError:
        return False
    return True


# Replica freshness (stale-replica self-heal). libsql 0.1.11 replicas can stop
# advancing after Turso rolls to a new generation, while sync() still returns
# cleanly. After each sync we compare these watermarks with what the primary
# reported at the previous check: anything committed then must be local now.
FRESHNESS_SQL = (
    "SELECT (SELECT coalesce(max(rowid), 0) FROM punches),"
    " (SELECT coalesce(max(last_attempt), '') FROM devices)"
)
# Consecutive behind-checks before the replica file is rebuilt from scratch.
STALE_CHECKS_BEFORE_REBUILD = 2
_REPLICA_SUFFIXES = ("", "-wal", "-shm", "-info")


def remove_replica_files(path: str) -> None:
    """Delete a replica and its sidecars so the next open bootstraps fresh."""
    for suffix in _REPLICA_SUFFIXES:
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def replica_behind(local: tuple | None, seen_remote: tuple | None) -> bool:
    """True when the replica lacks something the primary already had."""
    if local is None or seen_remote is None:
        return False
    (local_rowid, local_attempt), (seen_rowid, seen_attempt) = local, seen_remote
    return (local_rowid or 0) < (seen_rowid or 0) or str(local_attempt or "") < str(seen_attempt or "")


class _Cursor:
    """Dict rows over a libsql cursor, with the sqlite3 attributes callers use."""

    def __init__(self, cursor, offline: bool):
        self._cursor = cursor
        self._offline = offline
        self._names = [column[0] for column in cursor.description or ()]

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    def fetchone(self) -> dict[str, object] | None:
        return _dict_row(self._names, _call(self._cursor.fetchone, self._offline))

    def fetchall(self) -> list[dict[str, object]]:
        return [_dict_row(self._names, row) for row in _call(self._cursor.fetchall, self._offline)]

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.fetchall())


def _call(fn, offline: bool, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        mapped = classify_error(exc)
        if mapped is None and offline and "readonly database" in str(exc):
            mapped = OfflineError("Turso is unreachable; the database is read-only until it reconnects")
        if mapped is None:
            raise
        raise mapped from exc


class _Connection:
    """The one shared libsql connection, wrapped so callers keep sqlite3-style
    dict rows and get IntegrityError / OfflineError instead of ValueError."""

    def __init__(self, raw, *, offline: bool = False):
        self.raw = raw
        self.offline = offline
        self._trace = None

    def set_trace_callback(self, callback) -> None:
        self._trace = callback

    def execute(self, sql: str, params: Iterable[object] = ()) -> _Cursor:
        if self._trace is not None:
            self._trace(sql)
        return _Cursor(_call(self.raw.execute, self.offline, sql, _bind(params)), self.offline)

    def executemany(self, sql: str, rows: Iterable[Iterable[object]]) -> None:
        if self._trace is not None:
            self._trace(sql)
        _call(self.raw.executemany, self.offline, sql, [_bind(row) for row in rows])

    def commit(self) -> None:
        _call(self.raw.commit, self.offline)

    def sync(self) -> None:
        _call(self.raw.sync, self.offline)

    def close(self) -> None:
        self.raw.close()


# Schema version 1. Timestamps carry no DEFAULT: SQLite's CURRENT_TIMESTAMP is
# UTC, so every write binds _now() (naive IST) explicitly.
SCHEMA_STATEMENTS = tuple(
    statement.strip()
    for statement in """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    model TEXT NOT NULL,
    ip TEXT NOT NULL,
    port INTEGER NOT NULL,
    protocol TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'never_synced',
    transport TEXT,
    serial_number TEXT,
    firmware TEXT,
    last_attempt TIMESTAMP,
    last_success TIMESTAMP,
    last_error TEXT,
    records_seen INTEGER NOT NULL DEFAULT 0,
    comm_password TEXT,
    connection_updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_user_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role TEXT NOT NULL DEFAULT 'employee'
        CHECK (role IN ('employee', 'manager', 'hr', 'admin')),
    manager_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_employees_manager ON employees(manager_id);

CREATE TABLE IF NOT EXISTS punches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL REFERENCES devices(id),
    site TEXT NOT NULL,
    device_serial TEXT,
    device_user_id TEXT,
    employee_name TEXT,
    event_timestamp TIMESTAMP,
    raw_state TEXT,
    door TEXT,
    reader TEXT,
    raw_direction_byte INTEGER,
    trusted_direction TEXT,
    raw_json JSON TEXT NOT NULL,
    collected_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_punches_timestamp ON punches(event_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_punches_device ON punches(device_id);
CREATE INDEX IF NOT EXISTS idx_punches_user ON punches(device_user_id);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    csrf_token TEXT,
    created_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS login_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    pending_token_hash TEXT NOT NULL,
    requested_ip TEXT,
    expires_at TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_codes_email ON login_codes(email, created_at);
CREATE INDEX IF NOT EXISTS idx_login_codes_ip ON login_codes(requested_ip, created_at);
CREATE INDEX IF NOT EXISTS idx_login_codes_pending ON login_codes(pending_token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_employee ON sessions(employee_id);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prefix TEXT NOT NULL UNIQUE,
    key_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP,
    last_used_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TIMESTAMP NOT NULL,
    created_by INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    object_key TEXT,
    size_bytes INTEGER,
    status TEXT NOT NULL,
    error TEXT
)
""".split(";")
    if statement.strip()
)

# Ordered upgrade steps keyed by the version they produce. A fresh file gets
# SCHEMA_STATEMENTS (v1) and then every step, so both paths end identical.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    # v2: per-punch Frappe HR push state (essl_hub.frappe_sync).
    2: (
        """
        CREATE TABLE IF NOT EXISTS frappe_sync_log (
            punch_id INTEGER PRIMARY KEY REFERENCES punches(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            checkin_name TEXT,
            error TEXT,
            synced_at TIMESTAMP NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_frappe_sync_status ON frappe_sync_log(status)",
    ),
    # v3: manual backups removed; Turso keeps point-in-time history (R5).
    3: ("DROP TABLE IF EXISTS backups",),
}

DEFAULT_TARGET_MINUTES = "480"
DEFAULT_DAY_START_HOUR = "6"


def _schema_version(connection: Any) -> int:
    """Higher of PRAGMA user_version and settings.schema_version (Turso cannot
    write the pragma, so a replica records its version only in settings)."""
    version = connection.execute("PRAGMA user_version").fetchone()["user_version"]
    has_settings = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
    ).fetchone()
    if has_settings:
        row = connection.execute(
            "SELECT value FROM settings WHERE key='schema_version'"
        ).fetchone()
        if row:
            version = max(version, int(row["value"]))
    return version


class Database:
    """One libsql connection per process, shared under an RLock (KTD1).

    Local mode (no TURSO_DATABASE_URL, KTD2) opens `path` as a plain file.
    Replica mode opens `path` as an embedded replica of the Turso primary; if
    the primary is unreachable at open time it falls back to the existing
    replica file read-only (`offline` True) and `reconnect()` upgrades later.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        sync_url: str | None = None,
        auth_token: str | None = None,
        read_only: bool = False,
    ):
        self.path = path or db_path()
        # Read-only token (nightly export): skip PRAGMA foreign_keys, which a
        # replica forwards to the primary as a write and Turso then blocks.
        self.read_only = read_only
        if sync_url is None:
            settings = turso_settings()
            if settings is not None:
                sync_url, auth_token = settings
        self.sync_url = sync_url or None
        self.auth_token = auth_token
        self.offline = False
        self.last_sync_ok_at: datetime | None = None
        self.last_sync_error: str | None = None
        self._lock = threading.RLock()
        self._conn: _Connection | None = None
        self._stop = threading.Event()
        self.last_rebuild_at: datetime | None = None
        self._remote = None
        self._remote_seen: tuple | None = None
        self._behind_checks = 0

    @property
    def replica(self) -> bool:
        return self.sync_url is not None

    def _open_replica(self) -> _Connection | None:
        """Replica connection, or None when the primary is unreachable. Probes
        first so a dead network never reaches libsql's blocking connect."""
        if not primary_reachable(self.sync_url):
            return None
        try:
            raw = libsql.connect(
                self.path,
                sync_url=self.sync_url,
                auth_token=self.auth_token,
                isolation_level=None,
            )
        except ValueError as exc:
            if not isinstance(classify_error(exc), OfflineError):
                raise
            _logger.warning("Turso replica connect failed: %s", exc)
            return None
        if not self.read_only:
            raw.execute("PRAGMA foreign_keys=ON")
        return _Connection(raw)

    def _open(self) -> _Connection:
        if not self.replica:
            raw = libsql.connect(self.path, isolation_level=None)
            raw.execute("PRAGMA foreign_keys=ON")
            return _Connection(raw)
        connection = self._open_replica()
        if connection is not None:
            self.offline = False
            return connection
        self.offline = True
        if not os.path.exists(self.path):
            raise OfflineError("Turso is unreachable and no local replica file exists yet")
        _logger.warning("Turso unreachable; serving %s read-only until it reconnects", self.path)
        # Degraded: plain local open of the replica file. query_only keeps it
        # untouched so the later replica connect sees a clean file.
        raw = libsql.connect(self.path, isolation_level=None)
        raw.execute("PRAGMA query_only=ON")
        return _Connection(raw, offline=True)

    def _connection(self) -> _Connection:
        with self._lock:
            if self._conn is None:
                self._conn = self._open()
            return self._conn

    def reconnect(self) -> bool:
        """Upgrade a degraded (or unopened) replica to a live one. Never blocks
        on a dead network; True when a live connection is in place."""
        if not self.replica:
            return True
        with self._lock:
            if self._conn is not None and not self.offline:
                return True
            connection = self._open_replica()
            if connection is None:
                return False
            if self._conn is not None:
                self._conn.close()
            self._conn = connection
            self.offline = False
            _logger.info("Turso replica connection established")
            return True

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._remote = None
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def connect(self) -> Iterator[_Connection]:
        """Borrow the shared connection for reads (each statement autocommits)."""
        with self._lock:
            yield self._connection()

    @contextmanager
    def transaction(self) -> Iterator[_Connection]:
        """Write context: explicit BEGIN IMMEDIATE on the autocommit connection
        (KTD7). Refused up front while degraded so nothing lands locally."""
        with self._lock:
            connection = self._connection()
            if self.offline:
                raise OfflineError("Turso is unreachable; writes are paused until it reconnects")
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except Exception:  # noqa: BLE001 - the original error matters more
                    _logger.exception("ROLLBACK failed")
                raise
            connection.commit()

    def sync(self) -> bool:
        """Pull the primary into the replica (KTD4). Records the outcome on
        last_sync_ok_at / last_sync_error and never raises. No-op locally."""
        if not self.replica:
            return True
        with self._lock:
            try:
                if not self.reconnect():
                    raise OfflineError("Turso is unreachable")
                if not primary_reachable(self.sync_url):
                    raise OfflineError("Turso is unreachable")
                self._conn.sync()
                self._check_freshness()
            except Exception as exc:  # noqa: BLE001 - recorded, never fatal
                self.last_sync_error = str(exc) or type(exc).__name__
                _logger.warning("Turso sync failed: %s", self.last_sync_error)
                return False
            self.last_sync_ok_at = _now()
            self.last_sync_error = None
            return True

    def _watermarks(self, connection) -> tuple | None:
        try:
            row = connection.execute(FRESHNESS_SQL).fetchone()
        except Exception as exc:  # noqa: BLE001 - no schema yet, or a blip
            _logger.debug("freshness probe failed: %s", exc)
            return None
        return tuple(row.values()) if isinstance(row, dict) else tuple(row)

    def _remote_watermarks(self) -> tuple | None:
        try:
            if self._remote is None:
                self._remote = libsql.connect(self.sync_url, auth_token=self.auth_token)
            return self._watermarks(self._remote)
        except Exception as exc:  # noqa: BLE001 - skip this check, retry next sync
            _logger.debug("remote freshness probe failed: %s", exc)
            self._remote = None
            return None

    def _check_freshness(self) -> None:
        """Rebuild the replica when it stays behind the primary (see FRESHNESS_SQL)."""
        local = self._watermarks(self._conn)
        if replica_behind(local, self._remote_seen):
            self._behind_checks += 1
            _logger.warning(
                "Turso replica %s behind primary (local %s, primary had %s), check %d",
                self.path, local, self._remote_seen, self._behind_checks,
            )
            if self._behind_checks >= STALE_CHECKS_BEFORE_REBUILD:
                self.rebuild_replica()
        else:
            self._behind_checks = 0
        self._remote_seen = self._remote_watermarks()

    def rebuild_replica(self) -> None:
        """Drop the replica file and bootstrap a fresh one from the primary."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            remove_replica_files(self.path)
            self._behind_checks = 0
            self._remote_seen = None
            connection = self._open_replica()
            if connection is None:
                raise OfflineError("Turso became unreachable while rebuilding the replica")
            self._conn = connection
            self.offline = False
            self.last_rebuild_at = _now()
            _logger.warning("Turso replica %s rebuilt from the primary", self.path)

    def start_sync_loop(self, interval: float) -> threading.Thread | None:
        """Daemon thread calling sync() every `interval` seconds; None locally."""
        if not self.replica:
            return None

        def loop() -> None:
            while not self._stop.wait(interval):
                self.sync()

        thread = threading.Thread(target=loop, name="turso-sync", daemon=True)
        thread.start()
        return thread

    def migrate_schema(self) -> None:
        """Bring the schema to SCHEMA_VERSION; no seeding (KTD8). Every step is
        IF [NOT] EXISTS-safe, so a re-run or a race cannot fail."""
        with self.transaction() as connection:
            version = _schema_version(connection)
            if version >= SCHEMA_VERSION:
                return
            if version < 1:
                for statement in SCHEMA_STATEMENTS:
                    connection.execute(statement)
            for target in sorted(MIGRATIONS):
                if version < target:
                    for statement in MIGRATIONS[target]:
                        connection.execute(statement)
            # Turso rejects PRAGMA writes, so the version lives in settings;
            # local files (tests, migrate_postgres output) also get the pragma.
            connection.execute(
                "INSERT INTO settings (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            if not self.replica:
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    create_schema = migrate_schema

    def seed_defaults(self, devices: Iterable[DeviceConfig]) -> None:
        devices = tuple(devices)
        with self.transaction() as connection:
            for device in devices:
                connection.execute(
                    """
                    INSERT INTO devices (id, site, model, ip, port, protocol)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        site = excluded.site,
                        model = excluded.model,
                        protocol = excluded.protocol
                    """,
                    (device.id, device.site, device.model, device.ip, device.port, device.protocol),
                )
            connection.execute(
                "INSERT INTO settings (key, value) VALUES ('daily_target_minutes', ?) "
                "ON CONFLICT (key) DO NOTHING",
                (DEFAULT_TARGET_MINUTES,),
            )
            connection.execute(
                "INSERT INTO settings (key, value) VALUES ('day_start_hour', ?) "
                "ON CONFLICT (key) DO NOTHING",
                (DEFAULT_DAY_START_HOUR,),
            )

    def initialize(self, devices: Iterable[DeviceConfig]) -> None:
        self.create_schema()
        self.seed_defaults(devices)
        with self.transaction() as connection:
            connection.execute(
                "UPDATE devices SET status='error', "
                "last_error='Synchronization was interrupted by a server restart' "
                "WHERE status='syncing'"
            )

    def ensure_bootstrap_admin(self, email: str | None) -> None:
        """Promote the bootstrap address to admin while the database has no admin.

        Idempotent and safe to call on every web boot. Promoting only while no
        admin exists makes the variable a true bootstrap and break-glass: it
        cannot re-promote someone an admin later demoted (another admin still
        exists), and it does restore access if the last admin is lost. Unset,
        not-yet-on-the-roster, or an already-present admin all mean no-op.
        """
        if not email:
            return
        with self.transaction() as connection:
            existing_admin = connection.execute(
                "SELECT 1 FROM employees WHERE role = 'admin' LIMIT 1"
            ).fetchone()
            if existing_admin is not None:
                return
            connection.execute(
                "UPDATE employees SET role = 'admin', updated_at = ? "
                "WHERE lower(email) = lower(?)",
                (_now(), email),
            )

    def update_device(
        self, device_id: str, *, ip: str, port: int, comm_password: str | None
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE devices SET ip=?, port=?, comm_password=?, connection_updated_at=? "
                "WHERE id=?",
                (ip, port, comm_password, _now(), device_id),
            )

    def device_by_id(self, device_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
        return row

    def device_connection_info(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, ip, port, comm_password, connection_updated_at FROM devices ORDER BY id"
            ).fetchall()
        return list(rows)

    def warn_device_drift(self, devices: Iterable[DeviceConfig]) -> None:
        expected = {device.id: device for device in devices}
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, ip, port FROM devices WHERE connection_updated_at IS NULL"
            ).fetchall()
        for row in rows:
            config = expected.get(row["id"])
            if config is None:
                continue
            if row["ip"] != config.ip or row["port"] != config.port:
                _logger.warning(
                    "Device %s connection config drifted from config.DEVICES: "
                    "database has %s:%s, config expects %s:%s",
                    row["id"],
                    row["ip"],
                    row["port"],
                    config.ip,
                    config.port,
                )

    def _unseen(self, punches: Iterable[dict[str, object]]) -> list[dict[str, object]]:
        """Drop punches the replica already holds. Devices return their whole
        history every poll; this read is local, so only new rows go to Turso.
        A stale replica only costs extra rows that ON CONFLICT then ignores."""
        punches = list(punches)
        if not punches:
            return punches
        device_ids = sorted({str(p["device_id"]) for p in punches})
        marks = ", ".join("?" * len(device_ids))
        with self.connect() as connection:
            known = {
                row["fingerprint"]
                for row in connection.execute(
                    f"SELECT fingerprint FROM punches WHERE device_id IN ({marks})", device_ids
                ).fetchall()
            }
        return [p for p in punches if p["fingerprint"] not in known]

    def _insert_chunks(self, punches: list[dict[str, object]]) -> tuple[int, list[dict[str, object]]]:
        """Commit all but the last PUNCH_TX_ROWS-sized chunk in their own
        transactions, so a first-run backlog never holds Turso's write lock
        for minutes. Inserts are idempotent, so a partial run is safe to
        repeat. Returns (rows inserted, last chunk for the caller's tx)."""
        inserted = 0
        while len(punches) > PUNCH_TX_ROWS:
            chunk, punches = punches[:PUNCH_TX_ROWS], punches[PUNCH_TX_ROWS:]
            with self.transaction() as connection:
                inserted += _insert_punches(connection, chunk)
        return inserted, punches

    def insert_punches(self, punches: Iterable[dict[str, object]]) -> int:
        inserted, rest = self._insert_chunks(self._unseen(punches))
        if not rest:
            return inserted
        with self.transaction() as connection:
            return inserted + _insert_punches(connection, rest)

    def record_poll(
        self,
        device_id: str,
        *,
        punches: Iterable[dict[str, object]] = (),
        status: str,
        transport: str | None = None,
        serial_number: str | None = None,
        firmware: str | None = None,
        records_seen: int = 0,
        error: str | None = None,
    ) -> int:
        """Insert a poll's new punches and write its device row (KTD11). The
        row commits with the last chunk of punches; earlier chunks of a large
        backlog commit first (see _insert_chunks)."""
        now = _now()
        inserted, rest = self._insert_chunks(self._unseen(punches))
        with self.transaction() as connection:
            inserted += _insert_punches(connection, rest)
            if status == "connected":
                connection.execute(
                    """
                    UPDATE devices SET status='connected', transport=?, serial_number=?,
                        firmware=?, last_success=?, last_attempt=?, last_error=NULL,
                        records_seen=? WHERE id=?
                    """,
                    (transport, serial_number, firmware, now, now, records_seen, device_id),
                )
            else:
                connection.execute(
                    "UPDATE devices SET status=?, last_attempt=?, last_error=? WHERE id=?",
                    (status, now, (error or "")[:500] or None, device_id),
                )
        return inserted

    def devices(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*,
                    (SELECT COUNT(*) FROM punches p WHERE p.device_id = d.id) AS stored_punches
                FROM devices d
                ORDER BY CASE d.id WHEN 'uppal-x990' THEN 0 ELSE 1 END, d.site
                """
            ).fetchall()
        return list(rows)

    def punches_for_employee_range(
        self, device_user_id: str, start: datetime, end: datetime
    ) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_timestamp, trusted_direction, raw_direction_byte, raw_state
                FROM punches
                WHERE device_user_id = ? AND event_timestamp >= ? AND event_timestamp < ?
                ORDER BY event_timestamp ASC
                """,
                (device_user_id, start, end),
            ).fetchall()
        return list(rows)

    def punches_for_device_users_range(
        self, device_user_ids: Iterable[str], start: datetime, end: datetime
    ) -> list[dict[str, object]]:
        """All punches for a set of people over a range in one query, so Team
        and exports cost the same whatever the people x days product (U9)."""
        ids = sorted(set(device_user_ids))
        if not ids:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT device_user_id, event_timestamp, trusted_direction
                FROM punches
                WHERE device_user_id IN (SELECT value FROM json_each(?))
                  AND event_timestamp >= ? AND event_timestamp < ?
                ORDER BY device_user_id, event_timestamp ASC
                """,
                (json.dumps(ids), start, end),
            ).fetchall()
        return list(rows)

    def upsert_employee(self, *, device_user_id: str, name: str, email: str) -> str:
        email = email.strip().lower()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM employees WHERE device_user_id=?", (device_user_id,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO employees (device_user_id, name, email, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (device_user_id) DO UPDATE SET
                    name = excluded.name, email = excluded.email, updated_at = excluded.updated_at
                """,
                (device_user_id, name, email, _now(), _now()),
            )
        return "updated" if existing else "added"

    def update_employee(
        self, employee_id: int, *, name: str, email: str, device_user_id: str
    ) -> None:
        """Identity edit from the People form. Callers check access first; a
        clash with another employee's email or device user ID raises
        IntegrityError."""
        email = email.strip().lower()
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE employees SET name = ?, email = ?, device_user_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, email, device_user_id, _now(), employee_id),
            )

    def employees(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM employees ORDER BY name"
            ).fetchall()
        return list(rows)

    def employee_by_email(self, email: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM employees WHERE lower(email) = lower(?)",
                (email,),
            ).fetchone()
        return row

    def employee_by_id(self, employee_id: int) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM employees WHERE id = ?", (employee_id,)
            ).fetchone()
        return row

    def set_employee_role(self, employee_id: int, role: str) -> None:
        """Guarded in one statement (KTD7): the admin count is evaluated by the
        UPDATE itself on the primary, so two concurrent demotions cannot both
        succeed and a stale replica read cannot let one through."""
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE employees SET role = ?, updated_at = ?
                WHERE id = ? AND (
                    role <> 'admin' OR ? = 'admin'
                    OR (SELECT COUNT(*) FROM employees WHERE role = 'admin') > 1
                )
                """,
                (role, _now(), employee_id, role),
            )
            if cursor.rowcount:
                return
            if connection.execute(
                "SELECT 1 FROM employees WHERE id = ?", (employee_id,)
            ).fetchone() is None:
                raise RuleViolation("No such employee.")
            raise RuleViolation("The last admin cannot be demoted.")

    def set_employee_manager(self, employee_id: int, manager_id: int | None) -> None:
        """Guarded in one statement (KTD7): the UPDATE only matches when the
        manager exists and is outside this employee's subtree (no cycle)."""
        if manager_id is not None and manager_id == employee_id:
            raise RuleViolation("An employee cannot be their own manager.")
        with self.transaction() as connection:
            if manager_id is None:
                cursor = connection.execute(
                    "UPDATE employees SET manager_id = NULL, updated_at = ? WHERE id = ?",
                    (_now(), employee_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE employees SET manager_id = ?, updated_at = ?
                    WHERE id = ?
                      AND EXISTS (SELECT 1 FROM employees WHERE id = ?)
                      AND ? NOT IN (
                          WITH RECURSIVE sub(id) AS (
                              SELECT ?
                              UNION
                              SELECT e.id FROM employees e JOIN sub ON e.manager_id = sub.id
                          )
                          SELECT id FROM sub
                      )
                    """,
                    (manager_id, _now(), employee_id, manager_id, manager_id, employee_id),
                )
            if cursor.rowcount:
                return
            # The guard refused; work out which rule for the message.
            if connection.execute(
                "SELECT 1 FROM employees WHERE id = ?", (employee_id,)
            ).fetchone() is None:
                raise RuleViolation("No such employee.")
            if connection.execute(
                "SELECT 1 FROM employees WHERE id = ?", (manager_id,)
            ).fetchone() is None:
                raise RuleViolation("No such manager.")
            raise RuleViolation("That manager reports to this employee; it would create a cycle.")

    def subtree_ids(self, root_id: int) -> list[int]:
        """root_id plus every direct and indirect report."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                WITH RECURSIVE sub(id) AS (
                    SELECT id FROM employees WHERE id = ?
                    UNION
                    SELECT e.id FROM employees e JOIN sub ON e.manager_id = sub.id
                )
                SELECT id FROM sub
                """,
                (root_id,),
            ).fetchall()
        return [row["id"] for row in rows]

    def manager_chain(self, employee_id: int) -> list[int]:
        """employee_id followed by each manager above it."""
        with self.connect() as connection:
            return _manager_chain(connection, employee_id)

    def employee_by_device_user_id(self, device_user_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM employees WHERE device_user_id = ?", (device_user_id,)
            ).fetchone()
        return row

    def has_admin(self) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM employees WHERE role = 'admin' LIMIT 1"
            ).fetchone()
        return row is not None

    def create_first_admin(self, *, name: str, email: str, device_user_id: str) -> None:
        """Create the initial admin, or promote the employee already using that email.

        Called only by the one-time `/setup` flow, which runs only while
        `has_admin()` is false. Matching an existing row by email first means a
        database that already has employees (but no admin) is repaired rather
        than duplicated.
        """
        email = email.strip().lower()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM employees WHERE lower(email) = lower(?) LIMIT 1",
                (email,),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "UPDATE employees SET role = 'admin', updated_at = ? WHERE id = ?",
                    (_now(), existing["id"]),
                )
                return
            connection.execute(
                "INSERT INTO employees (device_user_id, name, email, role, created_at, updated_at) "
                "VALUES (?, ?, ?, 'admin', ?, ?)",
                (device_user_id, name, email, _now(), _now()),
            )

    def unmapped_device_user_ids(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.device_user_id, COUNT(*) AS punch_count
                FROM punches p
                LEFT JOIN employees e ON e.device_user_id = p.device_user_id
                WHERE e.id IS NULL AND p.device_user_id IS NOT NULL
                    AND p.device_user_id <> ''
                GROUP BY p.device_user_id
                ORDER BY p.device_user_id
                """,
            ).fetchall()
        return list(rows)

    def device_id_summary(self) -> list[dict[str, object]]:
        """One row per device user ID seen in punches, for the ID issues page."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT device_user_id, COUNT(*) AS punch_count,
                    MAX(event_timestamp) AS last_seen, MAX(employee_name) AS device_name,
                    GROUP_CONCAT(DISTINCT site) AS sites
                FROM punches
                WHERE device_user_id IS NOT NULL AND device_user_id <> ''
                GROUP BY device_user_id
                """,
            ).fetchall()
        summary = []
        for row in rows:
            item = dict(row)
            # MAX() drops the column's declared type, so the timestamp comes back as text.
            if isinstance(item["last_seen"], str):
                item["last_seen"] = datetime.fromisoformat(item["last_seen"])
            summary.append(item)
        return summary

    def count_recent_login_codes(self, *, email: str, since: datetime) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM login_codes WHERE lower(email)=lower(?) AND created_at >= ?",
                (email, since),
            ).fetchone()
        return row["n"]

    def count_recent_login_codes_by_ip(self, *, ip: str, since: datetime) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM login_codes WHERE requested_ip=? AND created_at >= ?",
                (ip, since),
            ).fetchone()
        return row["n"]

    def create_login_code(
        self,
        *,
        email: str,
        code_hash: str,
        pending_token_hash: str,
        requested_ip: str | None,
        expires_at: datetime,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO login_codes (
                    email, code_hash, pending_token_hash, requested_ip, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (email, code_hash, pending_token_hash, requested_ip, expires_at, _now()),
            )

    def active_login_code(self, *, pending_token_hash: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM login_codes
                WHERE pending_token_hash = ? AND consumed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (pending_token_hash,),
            ).fetchone()
        return row

    def increment_login_code_attempts(self, login_code_id: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE login_codes SET attempts = attempts + 1 WHERE id = ?", (login_code_id,)
            )

    def consume_login_code(self, login_code_id: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE login_codes SET consumed_at = ? WHERE id = ?", (_now(), login_code_id)
            )

    def create_session(
        self, *, employee_id: int, token_hash: str, csrf_token: str, expires_at: datetime
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO sessions (token_hash, employee_id, csrf_token, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_hash, employee_id, csrf_token, expires_at, _now()),
            )

    def session_employee(self, token_hash: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT e.*, s.csrf_token FROM sessions s
                JOIN employees e ON e.id = s.employee_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?
                """,
                (token_hash, _now()),
            ).fetchone()
        return row

    def revoke_session(self, token_hash: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ?", (_now(), token_hash)
            )

    def create_api_key(self, *, prefix: str, key_hash: str, label: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO api_keys (prefix, key_hash, label, created_at) VALUES (?, ?, ?, ?)",
                (prefix, key_hash, label, _now()),
            )

    def active_api_key_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM api_keys WHERE revoked_at IS NULL"
            ).fetchone()
        return row["n"]

    def api_key_by_hash(self, key_hash: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (key_hash,)
            ).fetchone()
        return row

    def touch_api_key(self, key_hash: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?", (_now(), key_hash)
            )

    def revoke_api_key(self, prefix: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE prefix = ?", (_now(), prefix)
            )

    def api_keys(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
        return list(rows)

    def setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    # --- Frappe HR push (essl_hub.frappe_sync) ---------------------------

    def frappe_pending_punches(self, *, since: datetime | None, limit: int) -> list[dict]:
        """Punches with no frappe_sync_log row, oldest first."""
        sql = (
            "SELECT p.id, p.device_id, p.device_user_id, p.event_timestamp, p.trusted_direction "
            "FROM punches p LEFT JOIN frappe_sync_log f ON f.punch_id = p.id "
            "WHERE f.punch_id IS NULL"
        )
        params: list[object] = []
        if since is not None:
            sql += " AND p.event_timestamp >= ?"
            params.append(since)
        sql += " ORDER BY p.event_timestamp, p.id LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            return connection.execute(sql, params).fetchall()

    def record_frappe_sync(
        self, punch_id: int, *, status: str, checkin_name: str | None = None, error: str | None = None
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO frappe_sync_log (punch_id, status, checkin_name, error, synced_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (punch_id) DO UPDATE SET "
                "status = excluded.status, checkin_name = excluded.checkin_name, "
                "error = excluded.error, synced_at = excluded.synced_at",
                (punch_id, status, checkin_name, error, _now()),
            )

    def frappe_sync_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM frappe_sync_log GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def min_device_last_success(self) -> datetime | None:
        """Oldest successful poll across devices; None if any device never succeeded."""
        with self.connect() as connection:
            rows = connection.execute("SELECT last_success FROM devices").fetchall()
        values = [row["last_success"] for row in rows]
        if not values or any(v is None for v in values):
            return None
        return min(values)

    def checkins_since(self, *, since: datetime | None, limit: int) -> list[dict]:
        sql = (
            "SELECT id, device_id, device_user_id, event_timestamp, trusted_direction "
            "FROM punches WHERE event_timestamp IS NOT NULL "
            "AND device_user_id IS NOT NULL AND device_user_id != ''"
        )
        params: list[object] = []
        if since is not None:
            sql += " AND event_timestamp > ?"
            params.append(since)
        sql += " ORDER BY event_timestamp, id LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            return connection.execute(sql, params).fetchall()

    def set_setting(self, key: str, value: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def _manager_chain(connection: _Connection, employee_id: int) -> list[int]:
    # UNION (not UNION ALL) stops on a pre-existing cycle instead of looping.
    rows = connection.execute(
        """
        WITH RECURSIVE up(id) AS (
            SELECT id FROM employees WHERE id = ?
            UNION
            SELECT e.manager_id FROM employees e JOIN up ON e.id = up.id
            WHERE e.manager_id IS NOT NULL
        )
        SELECT id FROM up
        """,
        (employee_id,),
    ).fetchall()
    return [row["id"] for row in rows]


def _now() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


_PUNCH_COLUMNS = (
    "fingerprint, device_id, site, device_serial, device_user_id, employee_name, "
    "event_timestamp, raw_state, door, reader, raw_direction_byte, "
    "trusted_direction, raw_json, collected_at"
)
# Rows per INSERT. Each statement on a replica is a round trip to Turso
# (~130 ms measured), so a device's full history must not go row by row.
# 100 rows x 14 params stays far under SQLite's bound-parameter limit.
PUNCH_INSERT_BATCH = 100
# Rows per write transaction (~10 statements, a few seconds of write lock).
PUNCH_TX_ROWS = 1000


def _insert_punches(connection: _Connection, punches: Iterable[dict[str, object]]) -> int:
    rows: list[tuple[object, ...]] = []
    for punch in punches:
        try:
            event_timestamp = _parse_timestamp(punch.get("event_timestamp"))
        except ValueError:
            _logger.warning(
                "Skipping punch %s from %s: unparseable timestamp %r",
                punch.get("fingerprint"),
                punch.get("device_id"),
                punch.get("event_timestamp"),
            )
            continue
        rows.append(
            (
                punch["fingerprint"],
                punch["device_id"],
                punch["site"],
                punch.get("device_serial", ""),
                punch.get("device_user_id", ""),
                punch.get("employee_name", ""),
                event_timestamp,
                punch.get("raw_state", ""),
                punch.get("door", ""),
                punch.get("reader", ""),
                punch.get("raw_direction_byte"),
                punch.get("trusted_direction"),
                json.dumps(punch.get("raw", {}), ensure_ascii=False, sort_keys=True, default=str),
                _now(),
            )
        )
    inserted = 0
    for start in range(0, len(rows), PUNCH_INSERT_BATCH):
        batch = rows[start : start + PUNCH_INSERT_BATCH]
        placeholders = ", ".join(["(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"] * len(batch))
        cursor = connection.execute(
            f"INSERT INTO punches ({_PUNCH_COLUMNS}) VALUES {placeholders} "
            "ON CONFLICT (fingerprint) DO NOTHING",
            [value for row in batch for value in row],
        )
        inserted += cursor.rowcount
    return inserted
