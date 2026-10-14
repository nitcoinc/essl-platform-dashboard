"""Cutover from the legacy Postgres database to a local SQLite file (KTD17).

Full mode (default) builds a local SQLite file at --target from Postgres
(devices, settings, employees, punches, api_keys), checkpoints and
integrity-checks it. Move it into the data folder as essl.db (with no -wal or
-shm beside it), then run `backup adopt` so backups accept it.

Delta mode (--punches-since) copies punches captured since a timestamp into an
existing local file at --target, in batched multi-row INSERT OR IGNORE,
skipping fingerprints it already has. Run it while web and collector are
stopped.

psycopg is not an app dependency: install it only for the run
(`pip install "psycopg[binary]"`) in a throwaway container.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from essl_hub.db import SCHEMA_VERSION, Database, _adapt_datetime, _parse_timestamp  # noqa: E402

DEFAULT_TARGET = "essl-migrated.db"
DELTA_BATCH_ROWS = 200  # 14 columns per row stays far below SQLite's bind limit

# Copy order respects foreign keys (punches -> devices).
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "devices": (
        "id", "site", "model", "ip", "port", "protocol", "status", "transport",
        "serial_number", "firmware", "last_attempt", "last_success", "last_error",
        "records_seen", "comm_password", "connection_updated_at",
    ),
    "settings": ("key", "value"),
    "employees": (
        "id", "device_user_id", "name", "email", "role", "created_at", "updated_at",
    ),
    "punches": (
        "id", "fingerprint", "device_id", "site", "device_serial", "device_user_id",
        "employee_name", "event_timestamp", "raw_state", "door", "reader",
        "raw_direction_byte", "trusted_direction", "raw_json", "collected_at",
    ),
    "api_keys": (
        "id", "prefix", "key_hash", "label", "created_at", "revoked_at", "last_used_at",
    ),
}
PRIMARY_KEYS = {"devices": "id", "settings": "key"}
USER_TABLES = ("employees", "punches", "api_keys")
TIMESTAMP_COLUMNS = {
    "last_attempt", "last_success", "connection_updated_at", "created_at",
    "updated_at", "event_timestamp", "collected_at", "revoked_at", "last_used_at",
}


class SourceReader(Protocol):
    def rows(self, table: str, since: datetime | None = None) -> list[dict[str, object]]:
        """All rows of `table` (punches only: collected_at >= since when given)."""


class PostgresReader:
    def __init__(self, url: str):
        import psycopg  # lazy: only installed for the migration run
        from psycopg.rows import dict_row

        self._connection = psycopg.connect(url, row_factory=dict_row)

    def rows(self, table: str, since: datetime | None = None) -> list[dict[str, object]]:
        columns = ", ".join(TABLE_COLUMNS[table])
        order = PRIMARY_KEYS.get(table, "id")
        sql = f"SELECT {columns} FROM {table}"
        params: tuple = ()
        if since is not None:
            sql += " WHERE collected_at >= %s"
            params = (since,)
        with self._connection.cursor() as cursor:
            cursor.execute(sql + f" ORDER BY {order}", params)
            return list(cursor.fetchall())


class MigrationError(RuntimeError):
    pass


def _value(column: str, value: object) -> object:
    if column == "raw_json" and not isinstance(value, (str, bytes)):
        return json.dumps(value)
    if column in TIMESTAMP_COLUMNS:
        return _parse_timestamp(value)
    if column == "email" and isinstance(value, str):
        return value.strip().lower()
    return value


def _row_values(table: str, row: dict[str, object]) -> tuple:
    return tuple(_value(column, row.get(column)) for column in TABLE_COLUMNS[table])


def _comparable(column: str, value: object) -> object:
    value = _value(column, value)
    if isinstance(value, datetime):
        return _adapt_datetime(value)
    if column in ("port", "records_seen") and value is not None:
        return int(value)
    return value


def _insert_sql(table: str, suffix: str) -> str:
    columns = TABLE_COLUMNS[table]
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) {suffix}"
    )


def _upsert_sql(table: str) -> str:
    key = PRIMARY_KEYS[table]
    updates = ", ".join(f"{c} = excluded.{c}" for c in TABLE_COLUMNS[table] if c != key)
    return _insert_sql(table, f"ON CONFLICT ({key}) DO UPDATE SET {updates}")


def migrate_full(source: SourceReader, database: Database) -> None:
    database.create_schema()
    data = {table: source.rows(table) for table in TABLE_COLUMNS}
    with database.transaction() as connection:
        for table in USER_TABLES:
            count = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            if count:
                raise MigrationError(
                    f"target table {table} already has {count} rows; full mode needs an "
                    "empty target (use --punches-since for a delta)"
                )
        for table in ("devices", "settings"):
            connection.executemany(_upsert_sql(table), [_row_values(table, r) for r in data[table]])
        for table in USER_TABLES:
            connection.executemany(_insert_sql(table, ""), [_row_values(table, r) for r in data[table]])


def migrate_punches_since(
    source: SourceReader, database: Database, since: datetime, batch_rows: int = DELTA_BATCH_ROWS
) -> int:
    rows = source.rows("punches", since=since)
    # Target ids are independent here; let SQLite assign them.
    columns = [c for c in TABLE_COLUMNS["punches"] if c != "id"]
    placeholder = f"({', '.join('?' for _ in columns)})"
    with database.transaction() as connection:
        before = connection.execute("SELECT COUNT(*) AS n FROM punches").fetchone()["n"]
        for start in range(0, len(rows), batch_rows):
            batch = rows[start:start + batch_rows]
            params = [_value(c, row.get(c)) for row in batch for c in columns]
            connection.execute(
                f"INSERT OR IGNORE INTO punches ({', '.join(columns)}) "
                f"VALUES {', '.join(placeholder for _ in batch)}",
                params,
            )
        after = connection.execute("SELECT COUNT(*) AS n FROM punches").fetchone()["n"]
    return after - before


def finalize_file(database: Database) -> None:
    """Checkpoint the WAL into the file and refuse a corrupt or wrong-version file."""
    with database.connect() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        result = connection.execute("PRAGMA integrity_check").fetchone()
        status = next(iter(result.values()))
        if status != "ok":
            raise MigrationError(f"integrity check failed: {status}")
        version = connection.execute("PRAGMA user_version").fetchone()["user_version"]
        if version != SCHEMA_VERSION:
            raise MigrationError(f"file reports schema v{version}, expected v{SCHEMA_VERSION}")


def verify(
    source: SourceReader,
    database: Database,
    tables: Iterable[str] = tuple(TABLE_COLUMNS),
    out: Callable[[str], None] = print,
) -> list[str]:
    """Print counts and a field diff of devices/settings; return mismatch lines."""
    mismatches: list[str] = []
    with database.connect() as connection:
        for table in tables:
            source_rows = source.rows(table)
            target_rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            if table == "settings":  # schema_version and day_start_hour are ours, not Postgres's
                target_rows = [r for r in target_rows if r["key"] not in ("schema_version", "day_start_hour")]
            line = f"{table}: source={len(source_rows)} target={len(target_rows)}"
            out(line)
            if table == "punches":
                # Delta mode adds cutover punches, so the target may legitimately hold more.
                missing = {r["fingerprint"] for r in source_rows} - {
                    r["fingerprint"] for r in target_rows
                }
                if missing:
                    mismatches.append(f"punches: {len(missing)} source fingerprints missing")
            elif len(source_rows) != len(target_rows):
                mismatches.append(line)
            if table not in PRIMARY_KEYS:
                continue
            key = PRIMARY_KEYS[table]
            target_by_key = {r[key]: r for r in target_rows}
            for row in source_rows:
                target = target_by_key.get(row[key])
                if target is None:
                    mismatches.append(f"{table}[{row[key]}]: missing in target")
                    continue
                for column in TABLE_COLUMNS[table]:
                    left = _comparable(column, row.get(column))
                    right = _comparable(column, target.get(column))
                    if left != right:
                        mismatches.append(f"{table}[{row[key]}].{column}: {left!r} != {right!r}")
    for mismatch in mismatches:
        out(f"MISMATCH {mismatch}")
    out("OK: no mismatches" if not mismatches else f"FAILED: {len(mismatches)} mismatches")
    return mismatches


def main(argv: list[str] | None = None, reader_factory: Callable[[str], SourceReader] = PostgresReader) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="Postgres URL of the old database")
    parser.add_argument(
        "--target", default=DEFAULT_TARGET,
        help=f"local SQLite file to build (full) or add to (delta) (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--punches-since",
        type=_parse_timestamp,
        help="delta mode: copy only punches collected at/after this IST timestamp into --target",
    )
    args = parser.parse_args(argv)
    delta = args.punches_since is not None
    if delta and not Path(args.target).exists():
        print(f"ERROR: delta mode adds to an existing file; {args.target} does not exist", file=sys.stderr)
        return 2
    database = Database(args.target)
    source = reader_factory(args.source)
    try:
        if delta:
            added = migrate_punches_since(source, database, args.punches_since)
            print(f"delta: added {added} punches since {_adapt_datetime(args.punches_since)}")
        else:
            migrate_full(source, database)
            finalize_file(database)
        tables = ("punches",) if delta else tuple(TABLE_COLUMNS)
        mismatches = verify(source, database, tables)
    except MigrationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    finally:
        database.close()
    if mismatches:
        return 1
    if not delta:
        print(f"schema v{SCHEMA_VERSION} file ready: {Path(args.target).resolve()}")
        print("  move it to the data folder as essl.db, then run:")
        print("  docker compose run --rm backup python -m essl_hub.backup adopt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
