from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator
from zoneinfo import ZoneInfo

import libsql

from .config import C3_PROTOCOL, X990_PROTOCOL, DeviceConfig, TIMEZONE_NAME, db_path
from .db_alerts import (
    ALERT_KINDS,
    ALERT_LIVE,
    ALERT_MODES,
    ALERT_MORNING,
    ALERT_RETENTION_DAYS,
    ALERT_STATUSES,
    BASELINE_SNAPSHOTS_KEPT,
    MAIL_MAX_ATTEMPTS,
    MAIL_RETENTION_DAYS,
    MAIL_RETRY_BASE,
    MAIL_RETRY_MAX,
    STATUS_BREAKER,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RECORDED,
    STATUS_SENT,
    AlertsMixin,
    mail_retry_delay,
)
from .db_archives import _ACTIVE_PUNCH_SQL, ARCHIVE_REASON_MAX_CHARS, ArchivesMixin, archive_range
from .db_fixes import (
    DIRECTIONS,
    FIX_ADD_KINDS,
    FIX_CLOSED_STATUSES,
    FIX_DECIDED_STATUSES,
    FIX_EVENT_KINDS,
    FIX_LINE_KINDS,
    FIX_MAX_LINES,
    FIX_NOTE_MAX_CHARS,
    FIX_SOURCES,
    OPPOSITE_DIRECTION,
    FixesMixin,
)
from .hours import apply_corrections

# Layout: this module is the core (connection and transaction machinery,
# type conversion, schema and MIGRATIONS, errors, devices, punches and their
# correction overlay, employees, sign-in). The missed-punch alert tables live
# in db_alerts (AlertsMixin), fix requests in db_fixes (FixesMixin) and punch
# archive rules in db_archives (ArchivesMixin); all are mixed into Database
# below. They never import this module at import time
# (only inside functions), so the import graph stays acyclic. Their constants
# are re-exported here, so ``from .db import ...`` keeps working.
__all__ = [
    # Core.
    "BUSY_TIMEOUT_MS", "C3_UNDECODED_BYTE", "DATE_COLUMNS", "DEFAULT_DAY_START_HOUR",
    "DEFAULT_TARGET_MINUTES", "DEVICE_CHANGE_KINDS", "DIRECTIONLESS_WINDOW_MIN", "DUPLICATE_WINDOW_MAX",
    "FRAPPE_GUESS_HOLD",
    "JSON_COLUMNS", "MIGRATIONS", "PUNCH_INSERT_BATCH", "PUNCH_TX_ROWS", "SCHEMA_STATEMENTS",
    "SCHEMA_VERSION", "SETUP_COMMANDS", "TIMESTAMP_COLUMNS", "TIMESTAMP_FORMAT",
    "BusyError", "Database", "DatabaseMissing", "FixLineLimit", "IntegrityError", "OfflineError",
    "RuleViolation", "classify_error", "device_ids_of", "reports_message",
    # Re-exported from db_alerts.
    "ALERT_KINDS", "ALERT_LIVE", "ALERT_MODES", "ALERT_MORNING", "ALERT_RETENTION_DAYS",
    "ALERT_STATUSES", "BASELINE_SNAPSHOTS_KEPT", "MAIL_MAX_ATTEMPTS", "MAIL_RETENTION_DAYS",
    "MAIL_RETRY_BASE", "MAIL_RETRY_MAX", "STATUS_BREAKER", "STATUS_FAILED", "STATUS_QUEUED",
    "STATUS_RECORDED", "STATUS_SENT", "AlertsMixin", "mail_retry_delay",
    # Re-exported from db_fixes.
    "DIRECTIONS", "FIX_ADD_KINDS", "FIX_CLOSED_STATUSES", "FIX_DECIDED_STATUSES",
    "FIX_EVENT_KINDS", "FIX_LINE_KINDS", "FIX_MAX_LINES", "FIX_NOTE_MAX_CHARS", "FIX_SOURCES",
    "OPPOSITE_DIRECTION", "FixesMixin",
    # Re-exported from db_archives.
    "ARCHIVE_REASON_MAX_CHARS", "ArchivesMixin", "archive_range",
]

_TZ = ZoneInfo(TIMEZONE_NAME)
_logger = logging.getLogger(__name__)

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
SCHEMA_VERSION = 10

# libsql exposes no declared column types (U8), so read-side conversion is by
# column name. Every TIMESTAMP column in the schema is listed here (a test
# checks SCHEMA_STATEMENTS and MIGRATIONS against it).
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
        "archived_at",
        "last_punch",
        "door_denied_since",
        "door_params_read_at",
        "door_params_previous_for",
        "door_params_requested_at",
        "policy_updated_at",
        "computed_at",
        "next_attempt_at",
        "sent_at",
        "decided_at",
        "event_time",
        "hr_event_time",
        "retired_at",
        "from_ts",
        "to_ts",
        "restored_at",
    }
)
JSON_COLUMNS = frozenset(
    {
        "raw_json",
        "door_params",
        "door_params_previous",
        "door_params_request",
        "door_params_result",
        "old_value",
        "new_value",
        "metrics",
        "recipients",
        "detail",
    }
)
# Work days (v8) are stored as ISO 'YYYY-MM-DD' text and read back as dates.
DATE_COLUMNS = frozenset({"work_day"})


class RuleViolation(ValueError):
    """A guarded write broke a data rule (last admin, manager cycle)."""


class FixLineLimit(RuleViolation):
    """A fix request would hold more than FIX_MAX_LINES lines (KTD7)."""


class IntegrityError(Exception):
    """A UNIQUE / FOREIGN KEY / CHECK constraint rejected a write (KTD6)."""


class OfflineError(Exception):
    """The database is temporarily unavailable; callers answer 503 or spool.
    BusyError is the case the local file raises (KTD6)."""


class BusyError(OfflineError):
    """Another process held the write lock past busy_timeout ("database is
    locked"). Transient: the web answers 503 and the collector spools (KTD6)."""


class DatabaseMissing(Exception):
    """A must-exist open found no database file. Only the backup commands
    (init, restore, import-turso) create it (KTD5)."""


# Busy wait on the shared file (KTD6). libsql holds the GIL while it waits
# (KTD7), so this also caps how long other threads in the process pause.
BUSY_TIMEOUT_MS = 5000

SETUP_COMMANDS = tuple(
    f"docker compose run --rm backup python -m essl_hub.backup {command}"
    for command in ("init", "restore", "import-turso")
)


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
    if "database is locked" in lowered:
        return BusyError(message)
    if any(marker in lowered for marker in _OFFLINE_MARKERS):
        return OfflineError(message)
    return None


def _adapt_datetime(value: datetime) -> str:
    """Store every timestamp as naive IST text in one canonical, sortable form."""
    if value.tzinfo is not None:
        value = value.astimezone(_TZ).replace(tzinfo=None)
    return value.strftime(TIMESTAMP_FORMAT)


def _bind(params: Iterable[object]) -> tuple[object, ...]:
    """libsql has no adapters (KTD5): format datetimes and dates and serialise
    dicts here. datetime subclasses date, so it is checked first."""
    bound = []
    for value in params:
        if isinstance(value, datetime):
            value = _adapt_datetime(value)
        elif isinstance(value, date):
            value = value.isoformat()
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
    if name in DATE_COLUMNS:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    return value


def _dict_row(names: list[str], row: tuple | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {name: _convert(name, value) for name, value in zip(names, row)}


def device_ids_of(employee: dict) -> list[str]:
    """Every device user ID of one employee, main ID first (v6). Rows from
    Database employee lookups carry ``device_ids``; anything else (a bare
    dict) falls back to the main ID alone."""
    ids = employee.get("device_ids")
    if ids:
        return list(ids)
    main = employee.get("device_user_id")
    return [str(main)] if main else []


def _attach_device_ids(connection, rows):
    """Add ``device_ids`` (main first, then extras oldest first) to employee rows."""
    rows = [row for row in rows if row is not None]
    if not rows:
        return rows
    extras: dict[object, list[str]] = {}
    for row in connection.execute(
        "SELECT employee_id, device_user_id FROM employee_device_ids "
        "ORDER BY created_at, device_user_id"
    ).fetchall():
        extras.setdefault(row["employee_id"], []).append(str(row["device_user_id"]))
    for row in rows:
        row["device_ids"] = [str(row["device_user_id"]), *extras.get(row["id"], [])]
    return rows


# Punch device user ID as Frappe knows it: an extra ID becomes its owner's main ID.
_MAIN_ID_SQL = (
    "COALESCE((SELECT e.device_user_id FROM employee_device_ids x "
    "JOIN employees e ON e.id = x.employee_id WHERE x.device_user_id = p.device_user_id), "
    "p.device_user_id)"
)


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


class _Cursor:
    """Dict rows over a libsql cursor, with the sqlite3 attributes callers use."""

    def __init__(self, cursor):
        self._cursor = cursor
        self._names = [column[0] for column in cursor.description or ()]

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    def fetchone(self) -> dict[str, object] | None:
        return _dict_row(self._names, _call(self._cursor.fetchone))

    def fetchall(self) -> list[dict[str, object]]:
        return [_dict_row(self._names, row) for row in _call(self._cursor.fetchall)]

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.fetchall())


def _call(fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        mapped = classify_error(exc)
        if mapped is None:
            raise
        raise mapped from exc


class _Connection:
    """The one shared libsql connection, wrapped so callers keep sqlite3-style
    dict rows and get IntegrityError / OfflineError instead of ValueError."""

    def __init__(self, raw):
        self.raw = raw
        self._trace = None

    def set_trace_callback(self, callback) -> None:
        self._trace = callback

    def execute(self, sql: str, params: Iterable[object] = ()) -> _Cursor:
        if self._trace is not None:
            self._trace(sql)
        return _Cursor(_call(self.raw.execute, sql, _bind(params)))

    def executemany(self, sql: str, rows: Iterable[Iterable[object]]) -> None:
        if self._trace is not None:
            self._trace(sql)
        _call(self.raw.executemany, sql, [_bind(row) for row in rows])

    def commit(self) -> None:
        _call(self.raw.commit)

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
    # v3: manual backups table removed (off-site copies are R2 snapshots).
    3: ("DROP TABLE IF EXISTS backups",),
    # v4: the devices table is authoritative (admins add and retire devices).
    # SQLite has no ADD COLUMN IF NOT EXISTS; migrate_schema skips a column
    # that already exists, so a re-run or a race stays safe.
    4: (
        "ALTER TABLE devices ADD COLUMN retired_at TIMESTAMP",
        "ALTER TABLE devices ADD COLUMN created_at TIMESTAMP",
    ),
    # v5: sign-in branding images live in the database so they survive
    # snapshots and restores (a host-volume file would not).
    5: (
        """
        CREATE TABLE IF NOT EXISTS branding_assets (
            key TEXT PRIMARY KEY,
            content_type TEXT NOT NULL,
            data BLOB NOT NULL,
            sha256 TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
        """,
    ),
    # v6: archive instead of delete, and extra device IDs per person. A
    # device user ID lives in exactly one place: employees.device_user_id or
    # employee_device_ids. The triggers guard every write path; the message
    # carries "constraint failed" so classify_error maps it to IntegrityError.
    6: (
        "ALTER TABLE employees ADD COLUMN archived_at TIMESTAMP",
        """
        CREATE TABLE IF NOT EXISTS employee_device_ids (
            device_user_id TEXT PRIMARY KEY,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            created_at TIMESTAMP NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_employee_device_ids_employee ON employee_device_ids(employee_id)",
        """
        CREATE TRIGGER IF NOT EXISTS employees_device_id_insert_unique
        BEFORE INSERT ON employees
        WHEN EXISTS (SELECT 1 FROM employee_device_ids WHERE device_user_id = NEW.device_user_id)
        BEGIN
            SELECT RAISE(ABORT, 'UNIQUE constraint failed: device user ID is an extra ID');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS employees_device_id_update_unique
        BEFORE UPDATE OF device_user_id ON employees
        WHEN EXISTS (SELECT 1 FROM employee_device_ids WHERE device_user_id = NEW.device_user_id)
        BEGIN
            SELECT RAISE(ABORT, 'UNIQUE constraint failed: device user ID is an extra ID');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS employee_device_ids_insert_unique
        BEFORE INSERT ON employee_device_ids
        WHEN EXISTS (SELECT 1 FROM employees WHERE device_user_id = NEW.device_user_id)
        BEGIN
            SELECT RAISE(ABORT, 'UNIQUE constraint failed: device user ID is a main ID');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS employee_device_ids_update_unique
        BEFORE UPDATE OF device_user_id ON employee_device_ids
        WHEN EXISTS (SELECT 1 FROM employees WHERE device_user_id = NEW.device_user_id)
        BEGIN
            SELECT RAISE(ABORT, 'UNIQUE constraint failed: device user ID is a main ID');
        END
        """,
    ),
    # v7: per-device swipe policy (KTD3, KTD5), C3 door-parameter state kept
    # for the collector (KTD6, KTD9), and an append-only audit log (KTD11).
    7: (
        "ALTER TABLE devices ADD COLUMN duplicate_window_seconds INTEGER NOT NULL DEFAULT 60",
        "ALTER TABLE devices ADD COLUMN door_denied_since TIMESTAMP",
        "ALTER TABLE devices ADD COLUMN door_params TEXT",
        "ALTER TABLE devices ADD COLUMN door_params_read_at TIMESTAMP",
        "ALTER TABLE devices ADD COLUMN door_params_previous TEXT",
        "ALTER TABLE devices ADD COLUMN door_params_previous_for TIMESTAMP",
        "ALTER TABLE devices ADD COLUMN door_params_request TEXT",
        "ALTER TABLE devices ADD COLUMN door_params_requested_at TIMESTAMP",
        "ALTER TABLE devices ADD COLUMN door_params_result TEXT",
        "ALTER TABLE devices ADD COLUMN policy_updated_at TIMESTAMP",
        """
        CREATE TABLE IF NOT EXISTS device_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL REFERENCES devices(id),
            actor_employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
            kind TEXT NOT NULL CHECK (kind IN
                ('policy', 'door_write', 'door_read', 'door_restore', 'door_dismiss')),
            old_value TEXT,
            new_value TEXT,
            created_at TIMESTAMP NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_device_changes_device ON device_changes(device_id, created_at)",
    ),
    # v8: missed-punch alerts, employee fix requests and HR approval. Weekly
    # baseline snapshots (KTD2), send-once alerts and the mail outbox (KTD8),
    # hashed report links (KTD7), requests with line items (KTD5), approved
    # fixes as an overlay keyed by employee (KTD4) and their audit log.
    # Work days are ISO date text (DATE_COLUMNS).
    8: (
        """
        CREATE TABLE IF NOT EXISTS attendance_baselines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            punched_days INTEGER NOT NULL,
            clean_days INTEGER NOT NULL,
            metrics TEXT,
            computed_at TIMESTAMP NOT NULL,
            UNIQUE (snapshot_id, employee_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS mail_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipients TEXT NOT NULL,
            subject TEXT NOT NULL,
            text_body TEXT,
            html_body TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            next_attempt_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL,
            sent_at TIMESTAMP
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_mail_outbox_due ON mail_outbox(status, next_attempt_at)",
        """
        CREATE TABLE IF NOT EXISTS attendance_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            work_day TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('live', 'morning')),
            mode TEXT NOT NULL CHECK (mode IN ('shadow', 'live')),
            status TEXT NOT NULL,
            site TEXT,
            detail TEXT,
            outbox_id INTEGER REFERENCES mail_outbox(id) ON DELETE SET NULL,
            created_at TIMESTAMP NOT NULL,
            UNIQUE (employee_id, work_day, kind)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_attendance_alerts_day ON attendance_alerts(work_day)",
        "CREATE INDEX IF NOT EXISTS idx_attendance_alerts_outbox ON attendance_alerts(outbox_id)",
        """
        CREATE TABLE IF NOT EXISTS fix_report_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            work_day TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS fix_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            work_day TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'closed_correct',
                'closed_left', 'approved', 'partial', 'rejected')),
            punch_set_hash TEXT,
            source TEXT NOT NULL CHECK (source IN ('link', 'session')),
            previous_request_id INTEGER REFERENCES fix_requests(id) ON DELETE SET NULL,
            decision_note TEXT,
            created_at TIMESTAMP NOT NULL,
            decided_at TIMESTAMP,
            decided_by INTEGER REFERENCES employees(id) ON DELETE SET NULL
        )
        """,
        # KTD5: at most one pending request per employee per work day.
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_fix_requests_one_pending "
        "ON fix_requests(employee_id, work_day) WHERE status = 'pending'",
        "CREATE INDEX IF NOT EXISTS idx_fix_requests_employee_day "
        "ON fix_requests(employee_id, work_day)",
        "CREATE INDEX IF NOT EXISTS idx_fix_requests_status ON fix_requests(status, created_at)",
        """
        CREATE TABLE IF NOT EXISTS fix_request_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL REFERENCES fix_requests(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('add_in', 'add_out', 'flip', 'ignore')),
            target_fingerprint TEXT,
            event_time TIMESTAMP,
            direction TEXT CHECK (direction IN ('entry', 'exit')),
            note TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected')),
            hr_event_time TIMESTAMP,
            hr_direction TEXT CHECK (hr_direction IN ('entry', 'exit')),
            decided_by INTEGER REFERENCES employees(id) ON DELETE SET NULL,
            decided_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_fix_request_lines_request ON fix_request_lines(request_id)",
        """
        CREATE TABLE IF NOT EXISTS punch_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            work_day TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('add', 'ignore', 'set_direction')),
            fingerprint TEXT,
            event_timestamp TIMESTAMP,
            direction TEXT CHECK (direction IN ('entry', 'exit')),
            request_line_id INTEGER NOT NULL UNIQUE REFERENCES fix_request_lines(id),
            created_at TIMESTAMP NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_punch_corrections_employee "
        "ON punch_corrections(employee_id, work_day)",
        """
        CREATE TABLE IF NOT EXISTS fix_request_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL REFERENCES fix_requests(id) ON DELETE CASCADE,
            line_id INTEGER REFERENCES fix_request_lines(id) ON DELETE CASCADE,
            actor_employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
            kind TEXT NOT NULL CHECK (kind IN ('filed', 'lines_added', 'closed',
                'reopened', 'line_approved', 'line_rejected', 'decided')),
            old_value TEXT,
            new_value TEXT,
            created_at TIMESTAMP NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_fix_request_events_request "
        "ON fix_request_events(request_id, created_at)",
    ),
    # v9: archive rules for ID-issue punches (KTD1). A rule hides one device
    # user ID's punches in [from_ts, to_ts) at read time (_ACTIVE_PUNCH_SQL);
    # the punch rows themselves are never edited. Restore stamps restored_at
    # and keeps the row as the audit record.
    9: (
        """
        CREATE TABLE IF NOT EXISTS punch_archives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_user_id TEXT NOT NULL,
            from_ts TIMESTAMP,
            to_ts TIMESTAMP NOT NULL,
            reason TEXT,
            created_by INTEGER REFERENCES employees(id) ON DELETE SET NULL,
            created_at TIMESTAMP NOT NULL,
            restored_by INTEGER REFERENCES employees(id) ON DELETE SET NULL,
            restored_at TIMESTAMP
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_punch_archives_user ON punch_archives(device_user_id)",
    ),
    # v10: an audit trail for corrections. A direct admin fix has no request
    # line, so request_line_id becomes nullable (SQLite cannot relax a NOT NULL
    # in place: rebuild, copying ids). Each row keeps who made it, their name
    # and role at that moment, the path and the reason. The new columns are
    # defaulted, so v9 code still inserts into a v10 file (a rollback). The
    # backfill names the old decider but not a role, which was never recorded;
    # a decider who is gone shows as "before audit trail".
    10: (
        """
        CREATE TABLE IF NOT EXISTS punch_corrections_v10 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            work_day TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('add', 'ignore', 'set_direction')),
            fingerprint TEXT,
            event_timestamp TIMESTAMP,
            direction TEXT CHECK (direction IN ('entry', 'exit')),
            request_line_id INTEGER UNIQUE REFERENCES fix_request_lines(id),
            created_at TIMESTAMP NOT NULL,
            created_by INTEGER REFERENCES employees(id) ON DELETE SET NULL,
            created_by_name TEXT NOT NULL DEFAULT 'before audit trail',
            created_by_role TEXT,
            path TEXT NOT NULL DEFAULT 'request' CHECK (path IN ('request', 'direct')),
            reason TEXT
        )
        """,
        """
        INSERT INTO punch_corrections_v10 (id, employee_id, work_day, kind, fingerprint,
            event_timestamp, direction, request_line_id, created_at, created_by,
            created_by_name, created_by_role, path, reason)
        SELECT c.id, c.employee_id, c.work_day, c.kind, c.fingerprint, c.event_timestamp,
            c.direction, c.request_line_id, c.created_at, l.decided_by,
            COALESCE(e.name, 'before audit trail'), NULL, 'request', r.decision_note
        FROM punch_corrections c
        LEFT JOIN fix_request_lines l ON l.id = c.request_line_id
        LEFT JOIN employees e ON e.id = l.decided_by
        LEFT JOIN fix_requests r ON r.id = l.request_id
        ORDER BY c.id
        """,
        "DROP TABLE punch_corrections",
        "ALTER TABLE punch_corrections_v10 RENAME TO punch_corrections",
        "CREATE INDEX IF NOT EXISTS idx_punch_corrections_employee "
        "ON punch_corrections(employee_id, work_day)",
        # Append-only: a correction is never edited, and only leaves with its
        # employee (the cascade runs once the employee row is gone).
        """
        CREATE TRIGGER IF NOT EXISTS punch_corrections_no_update
        BEFORE UPDATE OF employee_id, work_day, kind, fingerprint, event_timestamp, direction,
            request_line_id, created_at, created_by_name, created_by_role, path, reason
        ON punch_corrections
        BEGIN SELECT RAISE(ABORT, 'punch corrections are append-only'); END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS punch_corrections_no_delete
        BEFORE DELETE ON punch_corrections
        WHEN EXISTS (SELECT 1 FROM employees WHERE id = OLD.employee_id)
        BEGIN SELECT RAISE(ABORT, 'punch corrections are append-only'); END
        """,
    ),
}

# The C3 library's "unknown" in/out value: a realtime exit (byte 1) arrives as
# 15 and its direction is a guess until a history read gives the real byte.
C3_UNDECODED_BYTE = 15
# How long such a guess waits for that confirmation before Frappe gets it
# anyway (history is re-read every ESSL_C3_HISTORY_HOURS, default 6).
FRAPPE_GUESS_HOLD = timedelta(hours=24)
# Params: C3_UNDECODED_BYTE, now - FRAPPE_GUESS_HOLD. COALESCE keeps NOT (...)
# true for rows with no byte or no source (SQL NULL would hide them).
_FRAPPE_HELD_SQL = (
    "(COALESCE(p.raw_direction_byte, -1) = ? "
    "AND COALESCE(json_extract(p.raw_json, '$.source'), '') = 'realtime_fallback' "
    "AND p.event_timestamp > ?)"
)

# Quick-swipe window bounds in seconds (KTD3); 0 counts every swipe.
DUPLICATE_WINDOW_MAX = 300
# The X990 records no direction, so hours alternate In/Out from its swipes and
# one re-swipe the window lets through flips every later swipe that day. A
# device with directions (the C3) keeps the full 0-max range.
DIRECTIONLESS_WINDOW_MIN = 30


def window_floor(protocol: str | None) -> int:
    return DIRECTIONLESS_WINDOW_MIN if protocol == X990_PROTOCOL else 0
DEVICE_CHANGE_KINDS = frozenset(
    {"policy", "door_write", "door_read", "door_restore", "door_dismiss"}
)

_ADD_COLUMN = re.compile(r"^\s*ALTER TABLE (\w+) ADD COLUMN (\w+)", re.IGNORECASE)


def _column_exists(connection: Any, table: str, column: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _run_migration_statement(connection: Any, statement: str) -> None:
    match = _ADD_COLUMN.match(statement)
    if match and _column_exists(connection, match.group(1), match.group(2)):
        return
    connection.execute(statement)

DEFAULT_TARGET_MINUTES = "480"
DEFAULT_DAY_START_HOUR = "6"


def _schema_version(connection: Any) -> int:
    """Higher of PRAGMA user_version and settings.schema_version (files that
    came from Turso record their version only in settings)."""
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


class Database(AlertsMixin, FixesMixin, ArchivesMixin):
    """One libsql connection per process, shared under an RLock (KTD1).

    Opens `path` as a plain local file in WAL mode, shared by the web app and
    the collector (KTD6); `must_exist` refuses to create it (DatabaseMissing,
    KTD5). Alert, fix-request and punch-archive methods come from the mixins
    (db_alerts, db_fixes, db_archives).
    """

    def __init__(self, path: str | None = None, *, must_exist: bool = False):
        self.path = path or db_path()
        # Web app and collector: never create the file (KTD5); a missing file
        # raises DatabaseMissing on every use until a backup command makes it.
        self.must_exist = must_exist
        self._lock = threading.RLock()
        self._conn: _Connection | None = None

    def _open(self) -> _Connection:
        if self.must_exist and not os.path.exists(self.path):
            raise DatabaseMissing(f"{self.path} does not exist")
        raw = libsql.connect(self.path, isolation_level=None)
        # Two processes share the file (KTD6): WAL so readers never block
        # the writer, NORMAL sync (safe under WAL), bounded lock waits.
        raw.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("PRAGMA synchronous=NORMAL")
        raw.execute("PRAGMA foreign_keys=ON")
        return _Connection(raw)

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def _connection(self) -> _Connection:
        with self._lock:
            if self._conn is None:
                self._conn = self._open()
            return self._conn

    def close(self) -> None:
        with self._lock:
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
        (KTD7)."""
        with self._lock:
            connection = self._connection()
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

    def schema_version(self) -> int:
        """The database's schema version (see _schema_version)."""
        with self.connect() as connection:
            return _schema_version(connection)

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
                        _run_migration_statement(connection, statement)
            # The version lives in settings (older files only had it there)
            # and in the pragma.
            connection.execute(
                "INSERT INTO settings (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    create_schema = migrate_schema

    def seed_defaults(self, devices: Iterable[DeviceConfig]) -> None:
        """Insert the static devices only if missing. The DB is authoritative:
        an existing row's site, model, connection and retirement are kept."""
        devices = tuple(devices)
        with self.transaction() as connection:
            for device in devices:
                connection.execute(
                    """
                    INSERT INTO devices (id, site, model, ip, port, protocol, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        device.id, device.site, device.model, device.ip, device.port,
                        device.protocol, _now(),
                    ),
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
                "SELECT 1 FROM employees WHERE role = 'admin' AND archived_at IS NULL LIMIT 1"
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

    def add_device(
        self,
        *,
        device_id: str,
        site: str,
        model: str,
        ip: str,
        port: int,
        protocol: str,
        comm_password: str | None,
    ) -> None:
        """Insert an admin-added device. ``connection_updated_at`` is set so the
        card reads as pending until the collector's first attempt."""
        now = _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO devices (id, site, model, ip, port, protocol, comm_password, "
                "created_at, connection_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (device_id, site, model, ip, port, protocol, comm_password, now, now),
            )

    def set_device_retired(self, device_id: str, retired: bool) -> None:
        """Retire (stop polling) or re-enable a device. Punches are untouched."""
        with self.transaction() as connection:
            connection.execute(
                "UPDATE devices SET retired_at=? WHERE id=?",
                (_now() if retired else None, device_id),
            )

    def devices_at_address(self, ip: str, port: int) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM devices WHERE ip = ? AND port = ? ORDER BY created_at, id",
                (ip, port),
            ).fetchall()
        return list(rows)

    def device_by_id(self, device_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
        return row

    def device_connection_info(self) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, site, model, protocol, ip, port, comm_password, connection_updated_at, "
                "duplicate_window_seconds, door_denied_since, door_params_request, "
                "door_params_requested_at "
                "FROM devices WHERE retired_at IS NULL ORDER BY site, created_at, id"
            ).fetchall()
        return list(rows)

    # --- Swipe policy and C3 door parameters (v7) -------------------------

    def set_device_policy(
        self,
        device_id: str,
        *,
        window_seconds: int,
        wait_for_door_close: bool,
        actor_id: int | None = None,
    ) -> bool:
        """Set the quick-swipe window (KTD3) and "Wait for door close" (KTD5),
        with the audit row in the same transaction (KTD11). Unchecking starts
        ``door_denied_since`` once; re-checking clears it. False if no device."""
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int)
            or not 0 <= window_seconds <= DUPLICATE_WINDOW_MAX
        ):
            raise ValueError(f"window must be 0-{DUPLICATE_WINDOW_MAX} seconds")
        now = _now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT duplicate_window_seconds, door_denied_since, protocol FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            if row is None:
                return False
            if window_seconds < window_floor(row["protocol"]):
                raise ValueError(
                    f"window must be {window_floor(row['protocol'])}-{DUPLICATE_WINDOW_MAX} seconds on this device"
                )
            connection.execute(
                "UPDATE devices SET duplicate_window_seconds = ?, "
                "door_denied_since = CASE WHEN ? THEN NULL "
                "ELSE COALESCE(door_denied_since, ?) END, policy_updated_at = ? WHERE id = ?",
                (window_seconds, 1 if wait_for_door_close else 0, now, now, device_id),
            )
            _insert_device_change(
                connection, device_id, actor_id, "policy",
                {
                    "duplicate_window_seconds": row["duplicate_window_seconds"],
                    "wait_for_door_close": row["door_denied_since"] is None,
                },
                {
                    "duplicate_window_seconds": window_seconds,
                    "wait_for_door_close": bool(wait_for_door_close),
                },
            )
        return True

    def file_door_request(
        self,
        device_id: str,
        request: dict[str, int],
        *,
        kind: str = "door_write",
        requested_at: datetime | None = None,
        actor_id: int | None = None,
    ) -> bool:
        """File a door-parameter request for the collector (KTD6), replacing
        any pending one; an empty map is "Read from device". The audit row
        holds the last-read value of each requested key as old (KTD11).
        Whitelist checks live in essl_hub.door_params (KTD8)."""
        if kind not in ("door_write", "door_read", "door_restore"):
            raise ValueError(f"not a door request kind: {kind}")
        if not isinstance(request, dict) or any(
            not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int)
            for key, value in request.items()
        ):
            raise ValueError("door request must map keys to integers")
        requested_at = requested_at or _now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT door_params FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
            if row is None:
                return False
            current = row["door_params"] if isinstance(row["door_params"], dict) else {}
            connection.execute(
                "UPDATE devices SET door_params_request = ?, door_params_requested_at = ? "
                "WHERE id = ?",
                (dict(request), requested_at, device_id),
            )
            _insert_device_change(
                connection, device_id, actor_id, kind,
                {key: current.get(key) for key in request},
                dict(request),
            )
        return True

    def dismiss_door_request(self, device_id: str, *, actor_id: int | None = None) -> bool:
        """Clear the pending request and the last result; files nothing."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT door_params_request, door_params_requested_at, door_params_result "
                "FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE devices SET door_params_request = NULL, door_params_requested_at = NULL, "
                "door_params_result = NULL WHERE id = ?",
                (device_id,),
            )
            _insert_device_change(
                connection, device_id, actor_id, "door_dismiss",
                {
                    "request": row["door_params_request"],
                    "requested_at": row["door_params_requested_at"],
                    "result": row["door_params_result"],
                },
                None,
            )
        return True

    def record_door_params(
        self,
        device_id: str,
        *,
        params: dict[str, int] | None,
        read_at: datetime | None,
        result: dict | None = None,
        previous: dict[str, int] | None = None,
        previous_for: datetime | None = None,
    ) -> None:
        """Collector side: store a door read and, for a request, its per-key
        result. ``previous`` is kept once per request (KTD9): a second call for
        the same ``previous_for`` leaves the first snapshot. ``params`` None
        (an expired request, nothing read) keeps the last read. Never touches
        the policy columns (the web owns them)."""
        with self.transaction() as connection:
            if params is not None:
                connection.execute(
                    "UPDATE devices SET door_params = ?, door_params_read_at = ? WHERE id = ?",
                    (dict(params), read_at, device_id),
                )
            if result is not None:
                connection.execute(
                    "UPDATE devices SET door_params_result = ? WHERE id = ?",
                    (dict(result), device_id),
                )
            if previous is not None and previous_for is not None:
                connection.execute(
                    "UPDATE devices SET door_params_previous = ?, door_params_previous_for = ? "
                    "WHERE id = ? AND door_params_previous_for IS NOT ?",
                    (dict(previous), previous_for, device_id, previous_for),
                )

    def door_request_at(self, device_id: str) -> datetime | None:
        """The pending door request's ``requested_at``, read fresh so the
        collector never sends a request dismissed or replaced since its device
        list was read."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT door_params_requested_at FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
        return row["door_params_requested_at"] if row else None

    def clear_door_request(self, device_id: str, requested_at: datetime) -> bool:
        """Clear the request only if it is still the one filed at
        ``requested_at`` (KTD6), so a newer web request survives."""
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE devices SET door_params_request = NULL, door_params_requested_at = NULL "
                "WHERE id = ? AND door_params_requested_at = ?",
                (device_id, requested_at),
            )
            return cursor.rowcount > 0

    def device_windows(self) -> dict[str, int]:
        """Quick-swipe window per device, retired ones included (KTD3). A
        value stored below its device's floor scores at the floor."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, protocol, duplicate_window_seconds FROM devices"
            ).fetchall()
        return {
            row["id"]: max(int(row["duplicate_window_seconds"]), window_floor(row["protocol"]))
            for row in rows
        }

    def record_device_change(
        self,
        device_id: str,
        *,
        kind: str,
        old_value: dict | None,
        new_value: dict | None,
        actor_id: int | None = None,
    ) -> None:
        """Append one audit row on its own (KTD11)."""
        with self.transaction() as connection:
            _insert_device_change(connection, device_id, actor_id, kind, old_value, new_value)

    def device_changes(self, device_id: str, limit: int = 20) -> list[dict[str, object]]:
        """Newest audit rows first, with the actor's name when known."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT c.*, e.name AS actor_name FROM device_changes c "
                "LEFT JOIN employees e ON e.id = c.actor_employee_id "
                "WHERE c.device_id = ? ORDER BY c.created_at DESC, c.id DESC LIMIT ?",
                (device_id, limit),
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

    def unseen_punches(self, punches: Iterable[dict[str, object]]) -> list[dict[str, object]]:
        """Public read of _unseen for the collector's skip-unchanged check."""
        return self._unseen(punches)

    def _unseen(self, punches: Iterable[dict[str, object]]) -> list[dict[str, object]]:
        """Drop punches the database already holds. Devices return their whole
        history every poll, so only new rows are written; ON CONFLICT covers
        any race."""
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
        transactions, so a first-run backlog never holds the shared write
        lock for long (the web waits at most busy_timeout). Inserts are idempotent, so a partial run is safe to
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

    def fill_punch_directions(self, punches: Iterable[dict[str, object]]) -> int:
        """Set trusted_direction on stored C3 punches from a history download
        carrying the same fingerprints: rows with none, and realtime-sourced
        rows whose guess the history byte contradicts (KTD4; history is
        authoritative). A realtime row read as in/out 15 also takes the
        history's real byte, which confirms its direction for Frappe
        (``frappe_pending_punches``). Collector metadata, not a punch edit:
        history-sourced and non-C3 rows are never touched, and nothing is
        inserted. Returns the rows updated."""
        wanted = {
            str(p["fingerprint"]): (str(p["trusted_direction"]), p.get("raw_direction_byte"))
            for p in punches
            if p.get("trusted_direction") in ("entry", "exit") and p.get("fingerprint")
        }
        if not wanted:
            return 0
        # Read first: a history download is the device's whole log, so only
        # rows still open to repair are compared.
        with self.connect() as connection:
            stored = {
                row["fingerprint"]: (row["trusted_direction"], row["raw_direction_byte"])
                for row in connection.execute(
                    "SELECT fingerprint, trusted_direction, raw_direction_byte FROM punches "
                    "WHERE (trusted_direction IS NULL "
                    "OR json_extract(raw_json, '$.source') = 'realtime_fallback') "
                    "AND device_id IN (SELECT id FROM devices WHERE protocol = ?)",
                    (C3_PROTOCOL,),
                ).fetchall()
            }
        updates = []
        for fingerprint, (direction, byte) in wanted.items():
            if fingerprint not in stored:
                continue
            old_direction, old_byte = stored[fingerprint]
            new_byte = byte if old_byte == C3_UNDECODED_BYTE and byte is not None else old_byte
            if (old_direction, old_byte) != (direction, new_byte):
                updates.append((direction, new_byte, fingerprint))
        if not updates:
            return 0
        updated = 0
        with self.transaction() as connection:
            for direction, byte, fingerprint in updates:
                cursor = connection.execute(
                    "UPDATE punches SET trusted_direction = ?, raw_direction_byte = ? "
                    "WHERE fingerprint = ? AND (trusted_direction IS NULL "
                    "OR json_extract(raw_json, '$.source') = 'realtime_fallback') "
                    "AND device_id IN (SELECT id FROM devices WHERE protocol = ?)",
                    (direction, byte, fingerprint, C3_PROTOCOL),
                )
                updated += cursor.rowcount
        return updated

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
                ORDER BY d.site, d.created_at, d.id
                """
            ).fetchall()
        return list(rows)

    def punches_for_employee_range(
        self, device_user_ids: str | Iterable[str], start: datetime, end: datetime
    ) -> list[dict[str, object]]:
        """One person's punches; pass every ID (``device_ids_of``) so punches
        under extra IDs count too. Approved fixes are merged in (KTD4,
        ``hours.apply_corrections``); ignored rows come back marked, so the
        raw list still shows them."""
        ids = [device_user_ids] if isinstance(device_user_ids, str) else sorted(set(device_user_ids))
        with self.connect() as connection:
            return _overlaid_punches(connection, ids, start, end)

    def first_punch_at(self, device_user_ids: Iterable[str]) -> datetime | None:
        """Earliest punch across one person's IDs (``device_ids_of``); None
        when they never punched. Bounds the history pager and picker."""
        ids = sorted(set(device_user_ids))
        if not ids:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MIN(p.event_timestamp) AS first FROM punches p "
                "WHERE p.device_user_id IN (SELECT value FROM json_each(?)) "
                f"AND {_ACTIVE_PUNCH_SQL}",
                (json.dumps(ids),),
            ).fetchone()
        value = row["first"] if row else None
        if value is None:
            return None
        # MIN() drops the column's declared type, so timestamps come back as text.
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))

    def punches_for_device_users_range(
        self, device_user_ids: Iterable[str], start: datetime, end: datetime
    ) -> list[dict[str, object]]:
        """All punches for a set of people over a range in one query, so Team
        and exports cost the same whatever the people x days product (U9).
        Approved fixes are merged in as for ``punches_for_employee_range``;
        an added punch carries its owner's main ID."""
        ids = sorted(set(device_user_ids))
        if not ids:
            return []
        with self.connect() as connection:
            rows = _overlaid_punches(connection, ids, start, end)
        return sorted(rows, key=lambda r: (r["device_user_id"], r["event_timestamp"]))

    def raw_punches_for_device_users_range(
        self, device_user_ids: Iterable[str], start: datetime, end: datetime
    ) -> list[dict[str, object]]:
        """Stored device punches for these IDs in [start, end), with their
        ``raw_json`` and ``raw_direction_byte`` and no overlay: the morning
        job's unconfirmed-C3-guess check (KTD9) reads these. Hours never do."""
        ids = sorted(set(device_user_ids))
        if not ids:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT device_user_id, fingerprint, event_timestamp, trusted_direction,
                    raw_direction_byte, raw_state, raw_json, site, device_id
                FROM punches p
                WHERE device_user_id IN (SELECT value FROM json_each(?))
                  AND event_timestamp >= ? AND event_timestamp < ?
                  AND {_ACTIVE_PUNCH_SQL}
                ORDER BY device_user_id, event_timestamp
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
            # A new main ID may own punches Frappe skipped (employee not found).
            connection.execute(
                "DELETE FROM frappe_sync_log WHERE status = 'skipped' AND punch_id IN "
                "(SELECT id FROM punches WHERE device_user_id = ?)",
                (device_user_id,),
            )

    def extra_device_ids(self, employee_id: int) -> list[str]:
        """Extra device user IDs for one employee (v6), oldest first."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT device_user_id FROM employee_device_ids WHERE employee_id = ? "
                "ORDER BY created_at, device_user_id",
                (employee_id,),
            ).fetchall()
        return [str(row["device_user_id"]) for row in rows]

    def add_extra_device_id(self, employee_id: int, device_user_id: str) -> None:
        """Attach an extra device user ID. Raises IntegrityError when the ID is
        already anyone's main or extra ID (PRIMARY KEY plus v6 triggers).

        Punches under the ID that Frappe skipped (employee not found) are
        re-queued: they now push under the owner's main ID."""
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO employee_device_ids (device_user_id, employee_id, created_at) "
                "VALUES (?, ?, ?)",
                (device_user_id, employee_id, _now()),
            )
            connection.execute(
                "DELETE FROM frappe_sync_log WHERE status = 'skipped' AND punch_id IN "
                "(SELECT id FROM punches WHERE device_user_id = ?)",
                (device_user_id,),
            )

    def remove_extra_device_id(self, employee_id: int, device_user_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM employee_device_ids WHERE employee_id = ? AND device_user_id = ?",
                (employee_id, device_user_id),
            )
        return cursor.rowcount > 0

    def device_user_id_owner(self, device_user_id: str) -> dict[str, object] | None:
        """The employee holding this ID as main or extra ID, or None."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT e.* FROM employees e WHERE e.device_user_id = ?
                UNION ALL
                SELECT e.* FROM employee_device_ids x JOIN employees e ON e.id = x.employee_id
                WHERE x.device_user_id = ?
                LIMIT 1
                """,
                (device_user_id, device_user_id),
            ).fetchone()
        return row

    def archive_employee(self, employee_id: int) -> None:
        """Archive one person (KTD6), guarded in the UPDATE itself: not the
        last active admin, not someone with active direct reports. Ends every
        session they hold. Self and HR/admin rules live in access.can_archive."""
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE employees SET archived_at = ?, updated_at = ?
                WHERE id = ? AND archived_at IS NULL
                  AND (role <> 'admin' OR (
                      SELECT COUNT(*) FROM employees WHERE role = 'admin' AND archived_at IS NULL) > 1)
                  AND NOT EXISTS (
                      SELECT 1 FROM employees r WHERE r.manager_id = ? AND r.archived_at IS NULL)
                """,
                (_now(), _now(), employee_id, employee_id),
            )
            if cursor.rowcount:
                connection.execute("DELETE FROM sessions WHERE employee_id = ?", (employee_id,))
                return
            row = connection.execute(
                "SELECT role, archived_at FROM employees WHERE id = ?", (employee_id,)
            ).fetchone()
            if row is None:
                raise RuleViolation("No such employee.")
            if row["archived_at"] is not None:
                raise RuleViolation("Already archived.")
            reports = connection.execute(
                "SELECT COUNT(*) AS n FROM employees WHERE manager_id = ? AND archived_at IS NULL",
                (employee_id,),
            ).fetchone()["n"]
            if reports:
                raise RuleViolation(reports_message(reports))
            raise RuleViolation("The last admin cannot be archived.")

    def restore_employee(self, employee_id: int) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE employees SET archived_at = NULL, updated_at = ? "
                "WHERE id = ? AND archived_at IS NOT NULL",
                (_now(), employee_id),
            )
        return cursor.rowcount > 0

    def active_report_count(self, employee_id: int) -> int:
        with self.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) AS n FROM employees WHERE manager_id = ? AND archived_at IS NULL",
                (employee_id,),
            ).fetchone()["n"]

    def employees(self, include_archived: bool = False) -> list[dict[str, object]]:
        """Active people by default; archived ones only when asked (KTD6)."""
        where = "" if include_archived else " WHERE archived_at IS NULL"
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM employees{where} ORDER BY name"
            ).fetchall()
            return _attach_device_ids(connection, list(rows))

    def archived_employees(self) -> list[dict[str, object]]:
        """Archived people, most recently archived first, with their last punch."""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT e.*, (
                    SELECT MAX(p.event_timestamp) FROM punches p
                    WHERE (p.device_user_id = e.device_user_id OR p.device_user_id IN (
                        SELECT x.device_user_id FROM employee_device_ids x WHERE x.employee_id = e.id))
                      AND {_ACTIVE_PUNCH_SQL}
                ) AS last_punch
                FROM employees e WHERE e.archived_at IS NOT NULL
                ORDER BY e.archived_at DESC, e.name
                """
            ).fetchall()
            people = _attach_device_ids(connection, list(rows))
        for person in people:
            if isinstance(person.get("last_punch"), str):
                person["last_punch"] = datetime.fromisoformat(person["last_punch"])
        return people

    def archived_employees_with_punches(self, start: datetime, end: datetime) -> list[dict[str, object]]:
        """Archived people with any punch in [start, end), so exports and the
        hours API keep their past hours (archiving never drops data)."""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT e.* FROM employees e
                WHERE e.archived_at IS NOT NULL AND EXISTS (
                    SELECT 1 FROM punches p
                    WHERE p.event_timestamp >= ? AND p.event_timestamp < ?
                      AND (p.device_user_id = e.device_user_id OR p.device_user_id IN (
                          SELECT x.device_user_id FROM employee_device_ids x WHERE x.employee_id = e.id))
                      AND {_ACTIVE_PUNCH_SQL}
                )
                ORDER BY e.name
                """,
                (start, end),
            ).fetchall()
            return _attach_device_ids(connection, list(rows))

    def employee_by_email(self, email: str, include_archived: bool = False) -> dict[str, object] | None:
        archived = "" if include_archived else " AND archived_at IS NULL"
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT * FROM employees WHERE lower(email) = lower(?){archived}",
                (email,),
            ).fetchone()
            return (_attach_device_ids(connection, [row]) or [None])[0]

    def employee_by_id(self, employee_id: int, include_archived: bool = False) -> dict[str, object] | None:
        """Viewing paths treat archived people as not found; only archive,
        restore and the Archive tab pass include_archived."""
        archived = "" if include_archived else " AND archived_at IS NULL"
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT * FROM employees WHERE id = ?{archived}", (employee_id,)
            ).fetchone()
            return (_attach_device_ids(connection, [row]) or [None])[0]

    def set_employee_role(self, employee_id: int, role: str) -> None:
        """Guarded in one statement (KTD7): the admin count is evaluated by the
        UPDATE itself on the primary, so two concurrent demotions cannot both
        succeed and a stale read cannot let one through."""
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE employees SET role = ?, updated_at = ?
                WHERE id = ? AND (
                    role <> 'admin' OR ? = 'admin'
                    OR (SELECT COUNT(*) FROM employees WHERE role = 'admin' AND archived_at IS NULL) > 1
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
        """The holder of a main or extra device user ID (archived included)."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT e.* FROM employees e WHERE e.device_user_id = ?
                UNION ALL
                SELECT e.* FROM employee_device_ids x JOIN employees e ON e.id = x.employee_id
                WHERE x.device_user_id = ?
                LIMIT 1
                """,
                (device_user_id, device_user_id),
            ).fetchone()
            return (_attach_device_ids(connection, [row]) or [None])[0]

    def has_admin(self) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM employees WHERE role = 'admin' AND archived_at IS NULL LIMIT 1"
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
                f"""
                SELECT p.device_user_id, COUNT(*) AS punch_count
                FROM punches p
                LEFT JOIN employees e ON e.device_user_id = p.device_user_id
                WHERE e.id IS NULL AND p.device_user_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM employee_device_ids x WHERE x.device_user_id = p.device_user_id
                    )
                    AND p.device_user_id <> ''
                    AND {_ACTIVE_PUNCH_SQL}
                GROUP BY p.device_user_id
                ORDER BY p.device_user_id
                """,
            ).fetchall()
        return list(rows)

    def device_id_summary(self) -> list[dict[str, object]]:
        """One row per device user ID seen in unarchived punches, for the ID
        issues page. The device name comes from the latest punch, archived or
        not (only the outer rows are filtered, KTD2)."""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT p.device_user_id, COUNT(*) AS punch_count,
                    MIN(p.event_timestamp) AS first_seen, MAX(p.event_timestamp) AS last_seen,
                    (SELECT l.employee_name FROM punches l
                        WHERE l.device_user_id = p.device_user_id
                        ORDER BY l.event_timestamp DESC, l.id DESC LIMIT 1) AS device_name,
                    GROUP_CONCAT(DISTINCT p.site) AS sites
                FROM punches p
                WHERE p.device_user_id IS NOT NULL AND p.device_user_id <> ''
                  AND {_ACTIVE_PUNCH_SQL}
                GROUP BY p.device_user_id
                """,
            ).fetchall()
        summary = []
        for row in rows:
            item = dict(row)
            # MIN()/MAX() drop the column's declared type, so timestamps come back as text.
            for column in ("first_seen", "last_seen"):
                if isinstance(item[column], str):
                    item[column] = datetime.fromisoformat(item[column])
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
                  AND e.archived_at IS NULL
                """,
                (token_hash, _now()),
            ).fetchone()
            return (_attach_device_ids(connection, [row]) or [None])[0]

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
        """Punches with no frappe_sync_log row, oldest first. Door-denied C3
        rows (KTD5) stay local: payroll cannot see their flag. A C3 realtime
        row read as in/out 15 carries a guessed direction, so it waits until a
        history read confirms the real byte (Frappe keeps the log type it is
        first sent), or FRAPPE_GUESS_HOLD has passed."""
        sql = (
            f"SELECT p.id, p.device_id, {_MAIN_ID_SQL} AS device_user_id, "
            "p.event_timestamp, p.trusted_direction "
            "FROM punches p LEFT JOIN frappe_sync_log f ON f.punch_id = p.id "
            "WHERE f.punch_id IS NULL "
            "AND COALESCE(p.raw_state, '') NOT LIKE '%door denied:%' "
            f"AND NOT {_FRAPPE_HELD_SQL} AND {_ACTIVE_PUNCH_SQL}"
        )
        params: list[object] = [C3_UNDECODED_BYTE, _now() - FRAPPE_GUESS_HOLD]
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

    def frappe_oldest_held_punch(self) -> datetime | None:
        """The earliest unsent punch held back as a direction guess, so the
        shift sync never moves past it (frappe_sync.update_shift_types)."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MIN(p.event_timestamp) AS event_timestamp FROM punches p "
                "LEFT JOIN frappe_sync_log f ON f.punch_id = p.id "
                f"WHERE f.punch_id IS NULL AND {_FRAPPE_HELD_SQL} AND {_ACTIVE_PUNCH_SQL}",
                (C3_UNDECODED_BYTE, _now() - FRAPPE_GUESS_HOLD),
            ).fetchone()
        # Aliased to the column name so it is read back as a datetime.
        return row["event_timestamp"] if row else None

    def frappe_sync_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM frappe_sync_log GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def min_device_last_success(self) -> datetime | None:
        """Oldest successful poll across active devices; None if any never succeeded.
        Retired devices are not polled, so they never hold the watermark back."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT last_success FROM devices WHERE retired_at IS NULL"
            ).fetchall()
        values = [row["last_success"] for row in rows]
        if not values or any(v is None for v in values):
            return None
        return min(values)

    def unreachable_devices(self) -> list[str]:
        """Active devices whose last poll failed (status 'error'); the
        collector writes it on the first failure, unlike last_success."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM devices WHERE retired_at IS NULL AND status = 'error' ORDER BY id"
            ).fetchall()
        return [row["id"] for row in rows]

    def unreachable_device_details(self) -> list[dict]:
        """The devices behind unreachable_devices(), for the health signal and
        the Devices nav badge: id, display name and last good poll."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, site || ' ' || model AS name, last_success FROM devices "
                "WHERE retired_at IS NULL AND status = 'error' ORDER BY site, id"
            ).fetchall()
        return list(rows)

    def checkins_since(self, *, since: datetime | None, limit: int) -> list[dict]:
        sql = (
            f"SELECT p.id, p.device_id, {_MAIN_ID_SQL} AS device_user_id, "
            "p.event_timestamp, p.trusted_direction "
            "FROM punches p WHERE p.event_timestamp IS NOT NULL "
            "AND p.device_user_id IS NOT NULL AND p.device_user_id != '' "
            f"AND {_ACTIVE_PUNCH_SQL}"
        )
        params: list[object] = []
        if since is not None:
            sql += " AND p.event_timestamp > ?"
            params.append(since)
        sql += " ORDER BY p.event_timestamp, p.id LIMIT ?"
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


def _corrections_for(
    connection: _Connection, employee_ids: Iterable[int], start: datetime, end: datetime
) -> list[dict[str, object]]:
    """Database.corrections_for on a borrowed connection."""
    ids = sorted(set(employee_ids))
    if not ids:
        return []
    return connection.execute(
        "SELECT * FROM punch_corrections "
        "WHERE employee_id IN (SELECT value FROM json_each(?)) AND ("
        "(kind = 'add' AND event_timestamp >= ? AND event_timestamp < ?) "
        "OR (kind <> 'add' AND work_day >= ? AND work_day <= ?)) "
        "ORDER BY id",
        (json.dumps(ids), start, end, start.date() - timedelta(days=1), end.date()),
    ).fetchall()


def _overlaid_punches(
    connection: _Connection, ids: list[str], start: datetime, end: datetime
) -> list[dict[str, object]]:
    """Punches under these device user IDs in [start, end), with approved
    fixes merged (KTD4). Owners resolve like ``_attach_device_ids`` and
    ``_MAIN_ID_SQL``: a main ID is its employee's, an extra ID its owner's;
    corrections are keyed by employee, so an owner's added punches come back
    even on a day with no device punch."""
    rows = connection.execute(
        f"""
        SELECT device_user_id, fingerprint, event_timestamp, trusted_direction,
            raw_direction_byte, raw_state, site, device_id, door, reader,
            json_extract(raw_json, '$.source') AS source
        FROM punches p
        WHERE device_user_id IN (SELECT value FROM json_each(?))
          AND event_timestamp >= ? AND event_timestamp < ?
          AND {_ACTIVE_PUNCH_SQL}
        ORDER BY event_timestamp ASC
        """,
        (json.dumps(ids), start, end),
    ).fetchall()
    owners = connection.execute(
        """
        SELECT e.device_user_id AS held_id, e.id AS employee_id, e.device_user_id AS main_id
        FROM employees e WHERE e.device_user_id IN (SELECT value FROM json_each(?))
        UNION ALL
        SELECT x.device_user_id, e.id, e.device_user_id
        FROM employee_device_ids x JOIN employees e ON e.id = x.employee_id
        WHERE x.device_user_id IN (SELECT value FROM json_each(?))
        """,
        (json.dumps(ids), json.dumps(ids)),
    ).fetchall()
    owner_of = {str(o["held_id"]): o["employee_id"] for o in owners}
    main_ids = {o["employee_id"]: str(o["main_id"]) for o in owners}
    for row in rows:
        row["employee_id"] = owner_of.get(str(row["device_user_id"]))
    corrections = _corrections_for(connection, main_ids, start, end)
    if not corrections:
        # What apply_corrections returns with nothing to merge, without the
        # copy and re-sort: these rows are fresh and already in time order.
        for row in rows:
            row["correction"] = None
        return rows
    return apply_corrections(rows, corrections, main_ids)


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


def reports_message(count: int) -> str:
    people = "person reports" if count == 1 else "people report"
    return f"{count} {people} to them. Give them another manager first."


def _now() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


def _insert_device_change(
    connection: _Connection,
    device_id: str,
    actor_id: int | None,
    kind: str,
    old_value: dict | None,
    new_value: dict | None,
) -> None:
    """Append an audit row inside the caller's transaction (KTD11)."""
    if kind not in DEVICE_CHANGE_KINDS:
        raise ValueError(f"unknown device change kind: {kind}")
    connection.execute(
        "INSERT INTO device_changes (device_id, actor_employee_id, kind, old_value, "
        "new_value, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (device_id, actor_id, kind, old_value, new_value, _now()),
    )


_PUNCH_COLUMNS = (
    "fingerprint, device_id, site, device_serial, device_user_id, employee_name, "
    "event_timestamp, raw_state, door, reader, raw_direction_byte, "
    "trusted_direction, raw_json, collected_at"
)
# Rows per INSERT, so a device's full history does not go row by row.
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
