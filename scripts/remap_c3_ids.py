"""One-off: rewrite stored C3 punches to the mapped user ID and stable fingerprint.

Also deletes stored C3 rows that are not attendance swipes (denied swipes,
alarms), which the collector skips; interval-too-short swipes (event 20)
are attendance since KTD11 and are kept, and duplicates of one swipe
stored by both the realtime and the history path.

C3 punches collected before the PIN mapping carry the bare PIN (9) and a
fingerprint over the realtime payload. This recomputes both from raw_json with
the same rules the collector now uses, so later history reads dedupe.

    python scripts/remap_c3_ids.py           # dry run: prints the changes
    python scripts/remap_c3_ids.py --apply   # writes them

Uses ESSL_DB_PATH (the local file) like the apps. Safe to re-run: unchanged rows are skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from essl_hub.collectors import _c3_is_punch, _normalized_event, c3_identity, c3_user_id  # noqa: E402
from essl_hub.config import DEVICES  # noqa: E402
from essl_hub.db import Database  # noqa: E402


def _raw(row: dict) -> dict:
    return row["raw_json"] if isinstance(row["raw_json"], dict) else json.loads(row["raw_json"] or "{}")


def not_punches(rows: list[dict]) -> list[int]:
    return [r["id"] for r in rows if not _c3_is_punch(str(_raw(r).get("event_type", _raw(r).get("EventType", "0"))))]


def planned_changes(rows: list[dict]) -> list[tuple[int, str, str]]:
    devices = {d.id: d for d in DEVICES}
    changes = []
    for row in rows:
        raw = _raw(row)
        pin = str(raw.get("pin", raw.get("Pin", "")) or "")
        card = str(raw.get("card_no", raw.get("Cardno", "")) or "")
        user_id = c3_user_id(pin, card)
        identity = c3_identity(
            pin,
            card,
            str(raw.get("timestamp") or row["event_timestamp"]),
            str(raw.get("door", raw.get("DoorID", "")) or ""),
            str(raw.get("event_type", raw.get("EventType", "")) or ""),
        )
        fingerprint = _normalized_event(
            devices[row["device_id"]], raw=raw, serial_number="", user_id=user_id,
            employee_name="", timestamp="", raw_state="", identity=identity,
        )["fingerprint"]
        if user_id != row["device_user_id"] or fingerprint != row["fingerprint"]:
            changes.append((row["id"], user_id, fingerprint))
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args(argv)

    c3_ids = [d.id for d in DEVICES if d.protocol == "c3-pullsdk"]
    database = Database(os.getenv("ESSL_DB_PATH", "data/essl.db"), must_exist=True)
    marks = ",".join("?" * len(c3_ids))
    with database.connect() as connection:
        rows = [dict(r) for r in connection.execute(
            f"SELECT id, device_id, device_user_id, fingerprint, event_timestamp, raw_json "
            f"FROM punches WHERE device_id IN ({marks})", c3_ids,
        ).fetchall()]
    drop = set(not_punches(rows))
    rows = [r for r in rows if r["id"] not in drop]
    changes = planned_changes(rows)
    # One swipe stored by both the realtime and history paths: keep the first.
    final = {r["id"]: r["fingerprint"] for r in rows}
    final.update({row_id: fingerprint for row_id, _, fingerprint in changes})
    seen: set[str] = set()
    for row_id in sorted(final):
        if final[row_id] in seen:
            drop.add(row_id)
        seen.add(final[row_id])
    changes = [c for c in changes if c[0] not in drop]
    old = {r["id"]: r["device_user_id"] for r in rows}
    for row_id, user_id, _ in changes[:20]:
        print(f"punch {row_id}: {old[row_id]} -> {user_id}")
    print(f"{len(changes)} of {len(rows)} C3 punches need rewriting; {len(drop)} non-punch or duplicate rows to delete")
    if args.apply and (changes or drop):
        with database.transaction() as connection:
            for row_id in drop:
                connection.execute("DELETE FROM punches WHERE id = ?", (row_id,))
            for row_id, user_id, fingerprint in changes:
                connection.execute(
                    "UPDATE punches SET device_user_id = ?, fingerprint = ? WHERE id = ?",
                    (user_id, fingerprint, row_id),
                )
        print("applied")
    database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
