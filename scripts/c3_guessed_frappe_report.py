"""Read-only report: C3 live-log punches Frappe HR got as OUT on a guess.

From 2026-10-12 to schema v11 the hub read a C3 live-log in/out 15 as an
exit. Frappe keeps the log type it is first sent, and at Gachibowli 15 came
back for arrivals too, so some of those OUTs are wrong. The hub never stored
the log type it sent, so this rebuilds it from the live decode kept in
raw_json (in_out_state 15 survives a later history fill) and the send time:

- sent as OUT: synced before the 24 h hold was deployed (--hold-since,
  default the 2026-10-26 release), or after it while still unconfirmed.
- possibly sent as OUT: history has since filled the byte, but the row was
  synced at or after its swipe time plus the hold, so it likely went first.

Sends before the rule (2026-10-12 22:43) and after the v11 cutover
(settings.c3_live15_cutover_at) carried no log type and are not listed.
A row is listed only when that OUT may be wrong: the direction now is In,
or still unconfirmed. Columns: employee, device_user_id, day, time,
sent_log_type, direction_now, checkin_name. Fix each in Frappe (by its
check-in name) or on the hub with Fix punches.

    docker compose run --rm --no-deps -T -v "$PWD/scripts:/app/scripts" web \
        python scripts/c3_guessed_frappe_report.py > guessed-outs.csv

Uses ESSL_DB_PATH, opened read-only. Prints CSV to stdout and writes nothing.
"""

from __future__ import annotations

import argparse
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
# 32c132c: the rule that read live 15 as an exit.
GUESS_SINCE = datetime(2026, 10, 12, 22, 43, 17)
# 4734bef (merged 8cdc0d5): unconfirmed rows wait FRAPPE_GUESS_HOLD before
# the send. Before it, a guessed exit went out on the next sync.
HOLD_SINCE = datetime(2026, 10, 26, 21, 36, 48)
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
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def guessed_outs(rows: Iterable[dict], cutover: datetime | None, *, hold_since: datetime = HOLD_SINCE) -> list[dict]:
    """The rows whose Frappe OUT may be wrong, with the rebuilt log type."""
    out = []
    for row in rows:
        at, synced = _when(row["event_timestamp"]), _when(row["synced_at"])
        if at is None or synced is None:
            continue
        raw = row["raw_json"] if isinstance(row["raw_json"], dict) else json.loads(row["raw_json"] or "{}")
        if str(raw.get("event_type", "")).strip() not in OLD_EXIT_EVENTS:
            continue
        live_byte = raw.get("in_out_state", row["raw_direction_byte"])
        if str(live_byte) != str(C3_UNDECODED_BYTE):
            continue  # the live log decoded it; no guess was made
        if synced < GUESS_SINCE or (cutover is not None and synced >= cutover):
            continue  # sent without a log type
        unconfirmed = row["raw_direction_byte"] == C3_UNDECODED_BYTE
        if synced < hold_since or unconfirmed:
            sent = "OUT"
        elif synced >= at + FRAPPE_GUESS_HOLD:
            sent = "possibly OUT"
        else:
            continue  # history confirmed it before the send
        if not unconfirmed and row["trusted_direction"] == "exit":
            continue  # the OUT was right
        out.append({
            "employee": row["employee"],
            "device_user_id": row["device_user_id"],
            "day": at.date().isoformat(),
            "time": at.strftime("%H:%M:%S"),
            "sent_log_type": sent,
            "direction_now": "unconfirmed" if unconfirmed else {"entry": "In"}.get(row["trusted_direction"], "unconfirmed"),
            "checkin_name": row["checkin_name"] or "",
        })
    return out


def report(path: str, stream: IO[str], *, hold_since: datetime = HOLD_SINCE) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        settings = {r["key"]: r["value"] for r in connection.execute(
            "SELECT key, value FROM settings WHERE key IN (?, 'schema_version')", (CUTOVER_KEY,)).fetchall()}
        rows = [dict(r) for r in connection.execute(_SQL, (C3_PROTOCOL,)).fetchall()]
    finally:
        connection.close()
    cutover = _when(settings.get(CUTOVER_KEY))
    if cutover is None and int(settings.get("schema_version") or 0) >= 11:
        listed = []  # v11 ran on a file with no punches: nothing was ever guessed
    else:
        listed = guessed_outs(rows, cutover, hold_since=hold_since)
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(listed)
    return len(listed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hold-since", type=datetime.fromisoformat, default=HOLD_SINCE, metavar="WHEN",
                        help="when the 24 h hold reached production (default %(default)s, the release)")
    args = parser.parse_args(argv)
    count = report(db_path(), sys.stdout, hold_since=args.hold_since)
    print(f"{count} punch(es) to check in Frappe", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
