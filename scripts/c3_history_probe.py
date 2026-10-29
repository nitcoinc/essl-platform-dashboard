"""Read-only probe: why does the C3 history download stop, and what would let it finish?

The collector's transaction download (essl_hub.collectors.read_c3_table) is
reset by the Gachibowli panel after about 57 s. This runs the same download,
measures it, and tests the panel's capabilities the fix would rely on. It
sends only read commands (connect, GETPARAM, GETDATA, buffer reads,
disconnect): no setting is written and no log is cleared.

Stop the collector first so the panel serves one session, run this off-hours
with someone on site, then start the collector again:

    docker compose stop collector
    docker compose run --rm --no-deps -T collector python scripts/c3_history_probe.py
    docker compose start collector

Modes, run in this order by default (each on its own connection, one attempt):
    measure        the collector's session: params, user table, then the download
    fresh-session  the download on a new connection with nothing read before it
    read-length    one buffer read longer than the panel's announced chunk
    pipeline       several buffer reads in flight before reading any reply

A reset during read-length or pipeline stops the run: that is a capability
the panel lacks, and the remaining modes are skipped.

    python scripts/c3_history_probe.py --mode measure
    python scripts/c3_history_probe.py --device gachibowli-c3-400 --read-length 32768 --pipeline 4

Uses ESSL_DB_PATH (opened read-only) for the device's address and password.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from essl_hub.collectors import (  # noqa: E402
    C3_MAX_READ_BYTES,
    finish_c3_table,
    receive_c3_buffer,
    request_c3_buffer,
    start_c3_table,
)
from essl_hub.config import C3_PROTOCOL, db_path  # noqa: E402

MODES = ("measure", "fresh-session", "read-length", "pipeline")
PROGRESS_SECONDS = 5


def download(panel: Any, *, connected_at: float, clock: Callable[[], float] = time.monotonic,
             say: Callable[[str], None] = print) -> dict[str, Any]:
    """The collector's download loop, timed. Returns what it measured,
    including where it stopped when the panel cut it off."""
    result: dict[str, Any] = {"ok": False, "offset": 0, "requests": 0}
    started = clock()
    try:
        start = start_c3_table(panel, "transaction")
        result.update(total=start.total, chunk=start.chunk, getdata_s=round(clock() - started, 2))
        if start.payload is not None:
            result.update(ok=True, total=len(start.payload), chunk=None)
            return result
        buffer = bytearray()
        rtts: list[float] = []
        last_report = clock()
        while len(buffer) < start.total:
            sent = clock()
            request_c3_buffer(panel, len(buffer), min(start.chunk, start.total - len(buffer)))
            buffer += receive_c3_buffer(panel, len(buffer))
            rtts.append(clock() - sent)
            result.update(offset=len(buffer), requests=len(rtts))
            if clock() - last_report >= PROGRESS_SECONDS:
                last_report = clock()
                say(f"  {len(buffer):,} / {start.total:,} bytes ({100 * len(buffer) / start.total:.0f}%), "
                    f"{clock() - started:.1f}s since GETDATA")
        rows = finish_c3_table(start, bytes(buffer), "transaction")
        result.update(ok=True, rows=len(rows))
        if rtts:
            result.update(rtt_avg_ms=round(1000 * sum(rtts) / len(rtts), 1), rtt_max_ms=round(1000 * max(rtts), 1))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        elapsed = clock() - started
        result.update(since_getdata_s=round(elapsed, 2), since_connect_s=round(clock() - connected_at, 2))
        if result.get("offset") and elapsed > 0:
            result["bytes_per_s"] = round(result["offset"] / elapsed)
    return result


def read_length(panel: Any, length: int, *, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """One buffer read longer than the announced chunk. Valid when the reply
    answers offset 0 and carries more than one chunk of data."""
    start = start_c3_table(panel, "transaction")
    if start.payload is not None:
        return {"ok": True, "note": "the table fits one reply; nothing to test"}
    asked = min(length, C3_MAX_READ_BYTES, start.total)
    sent = clock()
    try:
        request_c3_buffer(panel, 0, asked)
        got = len(receive_c3_buffer(panel, 0))
    except Exception as exc:
        return {"ok": False, "chunk": start.chunk, "asked": asked, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": got > start.chunk, "chunk": start.chunk, "asked": asked, "got": got,
            "rtt_ms": round(1000 * (clock() - sent), 1)}


def pipeline(panel: Any, depth: int, *, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """``depth`` chunk reads sent back to back, then their replies read. Valid
    when every reply answers its own offset, in order."""
    start = start_c3_table(panel, "transaction")
    if start.payload is not None:
        return {"ok": True, "note": "the table fits one reply; nothing to test"}
    offsets = [n * start.chunk for n in range(depth) if n * start.chunk < start.total]
    sent = clock()
    try:
        for offset in offsets:
            request_c3_buffer(panel, offset, min(start.chunk, start.total - offset))
        for offset in offsets:
            receive_c3_buffer(panel, offset)
    except Exception as exc:
        return {"ok": False, "depth": len(offsets), "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "depth": len(offsets), "rtt_ms": round(1000 * (clock() - sent), 1)}


def device_settings(path: str, device_id: str | None) -> tuple[str, str, int, str | None]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        sql = "SELECT id, ip, port, comm_password FROM devices WHERE protocol = ? AND retired_at IS NULL"
        params: list[object] = [C3_PROTOCOL]
        if device_id:
            sql += " AND id = ?"
            params.append(device_id)
        row = connection.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
    finally:
        connection.close()
    if row is None:
        raise SystemExit(f"No active C3 device{f' {device_id}' if device_id else ''} in {path}")
    return row[0], row[1], int(row[2]), row[3] or os.getenv("ESSL_C3_PASSWORD") or None


def _connect(ip: str, port: int, password: str | None):
    from c3 import C3

    panel = C3(ip, port=port)
    if not panel.connect(password):
        raise ConnectionError("the controller rejected the session")
    return panel


def run(modes: list[str], *, connect: Callable[[], Any], read_bytes: int, depth: int,
        say: Callable[[str], None] = print, pause: Callable[[float], None] = time.sleep) -> int:
    failed_capability = False
    for number, mode in enumerate(modes):
        if number:
            pause(5)  # let the panel close the previous session
        say(f"\n== {mode} ==")
        connected_at = time.monotonic()
        try:
            panel = connect()
        except Exception as exc:
            say(f"  could not connect: {type(exc).__name__}: {exc}")
            return 1
        try:
            if mode == "measure":
                panel.get_device_param(["~SerialNumber", "FirmVer", "DeviceName"])
                try:
                    users = panel.get_device_data("user")
                    say(f"  user table: {len(users)} rows ({time.monotonic() - connected_at:.1f}s since connect)")
                except Exception as exc:
                    say(f"  user table read failed ({exc}); the collector carries on, so does this")
                result = download(panel, connected_at=connected_at, say=say)
            elif mode == "fresh-session":
                result = download(panel, connected_at=connected_at, say=say)
            elif mode == "read-length":
                result = read_length(panel, read_bytes)
            else:
                result = pipeline(panel, depth)
        finally:
            try:
                panel.disconnect()
            except Exception:
                pass
        for key, value in result.items():
            say(f"  {key}: {value}")
        if mode in ("read-length", "pipeline") and not result.get("ok"):
            failed_capability = True
            if "error" in result:
                say("  the panel refused or reset; stopping here")
                break
    return 1 if failed_capability else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", help="C3 device id (default: the first active C3)")
    parser.add_argument("--mode", choices=MODES, action="append", help="run only these modes (repeatable)")
    parser.add_argument("--read-length", type=int, default=C3_MAX_READ_BYTES, metavar="N",
                        help=f"bytes for the read-length test (capped at {C3_MAX_READ_BYTES})")
    parser.add_argument("--pipeline", type=int, default=4, metavar="N", help="reads in flight for the pipeline test")
    args = parser.parse_args(argv)
    device_id, ip, port, password = device_settings(db_path(), args.device)
    print(f"Probing {device_id} at {ip}:{port} (read-only). Is the collector stopped?")
    return run(args.mode or list(MODES), connect=lambda: _connect(ip, port, password),
               read_bytes=args.read_length, depth=max(1, args.pipeline))


if __name__ == "__main__":
    raise SystemExit(main())
