"""One-off: fill the direction of stored C3 punches the live log left NULL.

The live log cannot decode a C3 exit (see collectors._c3_trusted_direction),
so punches stored before that fix carry no direction. The panel's history
table carries the real byte. This downloads each C3 panel's history and fills
trusted_direction on stored NULL rows whose fingerprint matches a history row.
It never guesses: a row with no history match stays NULL.

    python scripts/backfill_c3_directions.py           # dry run: prints the counts
    python scripts/backfill_c3_directions.py --apply   # writes them

Uses ESSL_DB_PATH (the local file) and the device passwords like the apps. Safe to
re-run: only rows that are still NULL are touched.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from essl_hub.collector_main import merge_device_configs  # noqa: E402
from essl_hub.collectors import collect  # noqa: E402
from essl_hub.config import DEVICES  # noqa: E402
from essl_hub.db import Database  # noqa: E402


def planned_repairs(stored: list[dict], history: list[dict]) -> list[dict]:
    """History events whose fingerprint matches a stored NULL-direction row
    and which carry a direction themselves."""
    null_fingerprints = {r["fingerprint"] for r in stored if r.get("trusted_direction") is None}
    return [
        event for event in history
        if event["fingerprint"] in null_fingerprints and event.get("trusted_direction") in ("entry", "exit")
    ]


def download_history(device) -> list[dict]:
    previous = os.environ.get("ESSL_C3_MODE")
    os.environ["ESSL_C3_MODE"] = "history"  # fail loudly rather than fall back to the live log
    try:
        return list(collect(device).events)
    finally:
        if previous is None:
            os.environ.pop("ESSL_C3_MODE", None)
        else:
            os.environ["ESSL_C3_MODE"] = previous


def main(argv: list[str] | None = None, *, database: Database | None = None, downloader=download_history) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args(argv)

    own = database is None
    if own:
        database = Database(os.getenv("ESSL_DB_PATH", "data/essl.db"), must_exist=True)
    # Every non-retired C3 row in the DB, not just the seeded ones.
    rows = database.device_connection_info()
    c3_devices = [d for d in merge_device_configs(DEVICES, rows) if d.protocol == "c3-pullsdk"]
    ids = [d.id for d in c3_devices]
    marks = ",".join("?" * len(ids))
    with database.connect() as connection:
        stored = [dict(r) for r in connection.execute(
            f"SELECT fingerprint, trusted_direction FROM punches "
            f"WHERE trusted_direction IS NULL AND device_id IN ({marks})", ids,
        ).fetchall()] if ids else []
    history: list[dict] = []
    for device in c3_devices:
        history.extend(downloader(device))
    repairs = planned_repairs(stored, history)
    exits = sum(1 for r in repairs if r["trusted_direction"] == "exit")
    print(
        f"{len(stored)} C3 punches have no direction; {len(repairs)} match history "
        f"({exits} exit, {len(repairs) - exits} entry); {len(stored) - len(repairs)} stay unset"
    )
    if args.apply and repairs:
        updated = database.fill_punch_directions(repairs)  # one transaction
        print(f"applied: {updated} updated")
    if own:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
