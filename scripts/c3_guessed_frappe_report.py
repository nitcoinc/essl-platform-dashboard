"""Read-only report: C3 live-log punches Frappe HR got as OUT on a guess.

Before schema v11 the hub read a C3 live-log in/out 15 as an exit. A row
the history read had not confirmed within FRAPPE_GUESS_HOLD (24 h) went to
Frappe with log type OUT, and Frappe keeps the type it is first sent. At
Gachibowli 15 came back for arrivals too, so some of those OUTs are wrong.
The hub never stored the log type it sent, so this rebuilds it:

- sent as OUT: still in/out 15, a swipe event (0-3, 20), synced before the
  v11 cutover (settings.c3_live15_cutover_at).
- possibly sent as OUT: history has since filled the byte, but the row was
  synced at or after its swipe time plus the hold, so it likely went before
  history confirmed it.

A row is listed only when that OUT may be wrong: the direction now is entry,
or still unconfirmed. Fix each in Frappe (the check-in name is given) or on
the hub with Fix punches.

    python scripts/c3_guessed_frappe_report.py > guessed-outs.csv

Uses ESSL_DB_PATH, opened read-only. Prints CSV to stdout and writes nothing.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import IO, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from essl_hub.config import C3_PROTOCOL, db_path  # noqa: E402
from essl_hub.db import C3_UNDECODED_BYTE, FRAPPE_GUESS_HOLD  # noqa: E402

# Swipe events the retired rule read as an exit on in/out 15.
OLD_EXIT_EVENTS = {"0", "1", "2", "3", "20"}
CUTOVER_KEY = "c3_live15_cutover_at"
COLUMNS = ("employee", "device_user_id", "day", "time", "sent_log_type", "direction_now", "checkin_name")

_SQL = """
SELECT p.device_user_id, p.event_timestamp, p.trusted_direction, p.raw_direction_byte, p.raw_json,
       f.synced_at, f.checkin_name,
       COALESCE(e.name, owner.name, p.employee_name, '') AS employee
FROM punches p
JOIN frappe_sync_log f ON f.punch_id = p.id AND f.status = 'synced'
JOIN devices d ON d.id = p.device_id AND d.protocol = ?
LEFT JOIN employees e ON e.device_user_id = p.device_user_id
LEFT JOIN employee_device_ids x ON x.device_user_id = p.device_user_id
LEFT JOIN employees owner ON owner.id = x.employee_id
WHERE json_extract(p.raw_json, '$.source') = 'realtime_fallback'
ORDER BY p.event_timestamp, p.id
"""


def _when(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def guessed_outs(rows: Iterable[dict], cutover: datetime | None) -> list[dict]:
    """The rows whose Frappe OUT may be wrong, with the rebuilt log type."""
    out = []
    for row in rows:
        at, synced = _when(row["event_timestamp"]), _when(row["synced_at"])
        if at is None or synced is None:
            continue
        raw = row["raw_json"] if isinstance(row["raw_json"], dict) else json.loads(row["raw_json"] or "{}")
        if str(raw.get("event_type", "")).strip() not in OLD_EXIT_EVENTS:
            continue
        before_cutover = cutover is None or synced < cutover
        if not before_cutover:
            continue  # after v11 an unconfirmed row goes without a log type
        if row["raw_direction_byte"] == C3_UNDECODED_BYTE:
            sent = "OUT"
        elif synced >= at + FRAPPE_GUESS_HOLD:
            sent = "possibly OUT"
        else:
            continue  # history confirmed it before the send
        if row["trusted_direction"] == "exit":
            continue  # the OUT was right
        out.append({
            "employee": row["employee"],
            "device_user_id": row["device_user_id"],
            "day": at.date().isoformat(),
            "time": at.strftime("%H:%M:%S"),
            "sent_log_type": sent,
            "direction_now": {"entry": "In"}.get(row["trusted_direction"], "unconfirmed"),
            "checkin_name": row["checkin_name"] or "",
        })
    return out


def report(path: str, stream: IO[str]) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        found = connection.execute("SELECT value FROM settings WHERE key = ?", (CUTOVER_KEY,)).fetchone()
        cutover = _when(found["value"]) if found else None
        rows = [dict(r) for r in connection.execute(_SQL, (C3_PROTOCOL,)).fetchall()]
    finally:
        connection.close()
    listed = guessed_outs(rows, cutover)
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(listed)
    return len(listed)


def main() -> int:
    count = report(db_path(), sys.stdout)
    print(f"{count} punch(es) to check in Frappe", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
