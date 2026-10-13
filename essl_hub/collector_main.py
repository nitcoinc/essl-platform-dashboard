from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
import time
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from .collectors import CollectionResult, collect
from .config import (
    DEVICES,
    TIMEZONE_NAME,
    DeviceConfig,
    collector_interval_seconds,
    collector_status_path,
    heartbeat_write_seconds,
    spool_path,
    turso_sync_seconds,
)
from .db import Database
from .spool import Spool
from . import frappe_sync

_logger = logging.getLogger(__name__)


def merge_device_configs(
    static: tuple[DeviceConfig, ...] | list[DeviceConfig],
    rows: list[dict[str, object]],
    device_id: str | None = None,
) -> list[DeviceConfig]:
    """Build the poll list from the non-retired database rows (the DB is
    authoritative). ``static`` only supplies ``password_env`` (and any field a
    row lacks) for the seeded devices; a static device with no row is not polled."""
    by_id = {device.id: device for device in static}
    merged: list[DeviceConfig] = []
    for row in rows:
        if row.get("retired_at"):
            continue
        base = by_id.get(row["id"])
        protocol = row.get("protocol") or (base.protocol if base else None)
        if not protocol:
            continue
        device = DeviceConfig(
            id=str(row["id"]),
            site=str(row.get("site") or (base.site if base else row["id"])),
            model=str(row.get("model") or (base.model if base else "")),
            ip=str(row["ip"]),
            port=int(row["port"]),
            protocol=str(protocol),
            password_env=base.password_env if base else "",
            password=row.get("comm_password") or None,
        )
        if device_id is None or device.id == device_id:
            merged.append(device)
    return merged


def static_devices(device_id: str | None = None) -> list[DeviceConfig]:
    """Seed list polled until the first database read succeeds."""
    return [d for d in DEVICES if device_id is None or d.id == device_id]


def refresh_devices(
    database: Database,
    previous: list[DeviceConfig],
    device_id: str | None = None,
    state: "PollState | None" = None,
) -> list[DeviceConfig]:
    """Re-read connection info, reusing ``previous`` if the read fails (R16).

    When ``state`` is given, each row's ``connection_updated_at`` is recorded so
    the next poll of an edited device writes its row ("applied")."""
    try:
        rows = database.device_connection_info()
    except Exception as exc:
        _logger.warning(
            "device connection info refresh failed; reusing the previous device list: %s",
            exc,
        )
        return previous
    if state is not None:
        for row in rows:
            state.connection_versions[row["id"]] = row.get("connection_updated_at")
    return merge_device_configs(DEVICES, rows, device_id=device_id)


@dataclasses.dataclass
class _Written:
    status: tuple
    connection_version: object
    at: float


@dataclasses.dataclass
class PollState:
    """In-memory collector state (KTD11).

    ``written`` holds the device-row state last committed, so an unchanged idle
    poll writes nothing. ``ready`` turns True once the Turso boot work (initial
    sync, schema, seed) has succeeded (KTD10)."""

    heartbeat_seconds: float = 900
    clock: Callable[[], float] = time.monotonic
    written: dict[str, _Written] = dataclasses.field(default_factory=dict)
    connection_versions: dict[str, object] = dataclasses.field(default_factory=dict)
    ready: bool = False
    last_poll: str | None = None


def _has_unseen(database: Database, events) -> bool:
    if not events:
        return False
    try:
        return bool(database.unseen_punches(events))
    except Exception:  # noqa: BLE001 - unsure means write, as before
        return True


def collect_once(
    database: Database,
    device: DeviceConfig,
    *,
    collector: Callable[[DeviceConfig], CollectionResult] = collect,
    state: PollState | None = None,
    spool: Spool | None = None,
) -> None:
    """Poll one device and write only what changed (KTD11).

    Punches and the device row commit together. If that write fails for any
    reason the punches go to ``spool`` (KTD9) and the device row is retried
    next cycle; without a spool the error propagates."""
    if state is None:
        state = PollState()
    try:
        result = collector(device)
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        events: list[dict] = []
        status_key: tuple = ("error", message, None, None, None)
        write = {"status": "error", "error": message}
    else:
        events = list(result.events)
        status_key = (
            "connected",
            None,
            result.transport,
            result.serial_number,
            result.firmware,
        )
        write = {
            "status": "connected",
            "transport": result.transport,
            "serial_number": result.serial_number,
            "firmware": result.firmware,
            "records_seen": len(result.events),
        }

    now = state.clock()
    version = state.connection_versions.get(device.id)
    last = state.written.get(device.id)
    # Devices return their whole history every poll, so "no events" never
    # held; skip on "nothing new" (a local replica read) instead.
    if (
        last is not None
        and write.get("transport") != "TCP history"
        and not _has_unseen(database, events)
        and last.status == status_key
        and last.connection_version == version
        and now - last.at < state.heartbeat_seconds
    ):
        return

    if events and write.get("transport") == "TCP history":
        # R5: history carries the true exit byte; repair rows the live log
        # stored with no direction before the unseen filter drops them.
        try:
            repaired = database.fill_punch_directions(events)
        except Exception as exc:
            _logger.warning("direction repair for %s skipped: %s", device.id, exc)
        else:
            if repaired:
                _logger.info("filled direction on %d stored punch(es) for %s", repaired, device.id)

    try:
        database.record_poll(device.id, punches=events, **write)
    except Exception as exc:
        if spool is None:
            raise
        if events:
            spool.append(device.id, events)
            _logger.warning(
                "writing %d punch(es) for %s failed; spooled for replay: %s",
                len(events),
                device.id,
                exc,
            )
        else:
            # KTD13: device status is best-effort; the next cycle rewrites it.
            _logger.warning("device status write for %s skipped: %s", device.id, exc)
        return
    state.written[device.id] = _Written(status_key, version, now)


def _turso_healthy(database: Database) -> bool:
    return not getattr(database, "offline", False) and getattr(database, "last_sync_error", None) is None


def replay_spool(database: Database, spool: Spool) -> int:
    """Replay spooled batches oldest-first (KTD9); returns batches written.

    A failure while Turso looks healthy counts toward the dead-letter limit
    and replay moves on; a failure while offline stops replay uncounted."""
    written = 0
    for entry in spool.pending():
        try:
            database.insert_punches(entry.punches)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            if not _turso_healthy(database):
                _logger.warning("spool replay paused while Turso is unreachable: %s", error)
                break
            if spool.fail(entry.id, error):
                _logger.error(
                    "spooled batch %d for %s moved to dead after repeated failures: %s",
                    entry.id,
                    entry.device_id,
                    error,
                )
            else:
                _logger.warning("spooled batch %d replay failed: %s", entry.id, error)
            continue
        spool.ack(entry.id)
        written += 1
    return written


def ensure_ready(database: Database, state: PollState) -> bool:
    """Best-effort Turso boot work, retried each cycle until it succeeds (KTD10).

    ``Database.sync()`` probes before touching libsql, so a dead network costs
    seconds and never raises."""
    if state.ready:
        return True
    if not database.sync():
        return False
    try:
        database.initialize(DEVICES)
        database.warn_device_drift(DEVICES)
    except Exception as exc:
        _logger.warning("collector database setup failed; retrying next cycle: %s", exc)
        return False
    state.ready = True
    _logger.info("collector database ready")
    return True


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return str(value)


def write_status(path: str, status: dict) -> None:
    """Write ``status`` as JSON atomically: temp file in the same directory,
    then rename (KTD12)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".collector-status-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(status, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def collector_status(database: Database, spool: Spool, state: PollState) -> dict:
    return {
        "last_poll": state.last_poll,
        "last_sync_ok_at": _iso(getattr(database, "last_sync_ok_at", None)),
        "last_sync_error": getattr(database, "last_sync_error", None),
        "replica_rebuilt_at": _iso(getattr(database, "last_rebuild_at", None)),
        "spool_depth": spool.depth(),
        "spool_oldest_at": spool.oldest_at(),
        "dead_count": spool.dead_count(),
        "offline": bool(getattr(database, "offline", False)) or not state.ready,
    }


def run_cycle(
    database: Database,
    spool: Spool,
    devices: list[DeviceConfig],
    *,
    device_id: str | None = None,
    collector: Callable[[DeviceConfig], CollectionResult] = collect,
    state: PollState,
    frappe: "frappe_sync.SyncScheduler | None" = None,
    status_path: str | None = None,
) -> list[DeviceConfig]:
    """One collector cycle: boot work, replay, poll, Frappe, status file."""
    ready = ensure_ready(database, state)
    devices = refresh_devices(database, devices, device_id=device_id, state=state)
    if ready:
        replay_spool(database, spool)
    for device in devices:
        try:
            collect_once(database, device, collector=collector, state=state, spool=spool)
        except Exception:
            _logger.warning("poll cycle for %s failed; retrying next interval", device.id, exc_info=True)
    # Naive IST (the web compares in IST); containers run in UTC.
    state.last_poll = datetime.now(ZoneInfo(TIMEZONE_NAME)).replace(tzinfo=None, microsecond=0).isoformat(sep=" ")
    if frappe is not None and ready:
        frappe.maybe_run()
    if status_path is not None:
        try:
            write_status(status_path, collector_status(database, spool, state))
        except Exception:
            _logger.warning("could not write collector status file", exc_info=True)
    return devices


def run_forever(
    database: Database,
    *,
    interval_seconds: int,
    spool: Spool,
    device_id: str | None = None,
    collector: Callable[[DeviceConfig], CollectionResult] = collect,
    state: PollState | None = None,
    frappe: "frappe_sync.SyncScheduler | None" = None,
    status_path: str | None = None,
) -> None:
    if state is None:
        state = PollState(heartbeat_seconds=heartbeat_write_seconds())
    devices = static_devices(device_id)
    while True:
        try:
            devices = run_cycle(
                database,
                spool,
                devices,
                device_id=device_id,
                collector=collector,
                state=state,
                frappe=frappe,
                status_path=status_path,
            )
        except Exception:
            # R16: nothing in a cycle may end the loop.
            _logger.warning("collector cycle failed; retrying next interval", exc_info=True)
        time.sleep(interval_seconds)


def main() -> None:
    interval = collector_interval_seconds()
    device_id = os.getenv("ESSL_COLLECTOR_DEVICE_ID") or None
    # KTD10: the spool and polling never wait on Turso. Database() opens
    # lazily; run_cycle retries the boot work each cycle.
    spool = Spool(spool_path())
    database = Database()
    database.start_sync_loop(turso_sync_seconds(300))
    frappe = None
    frappe_config = frappe_sync.config_from_env()
    if frappe_config is not None:
        frappe = frappe_sync.SyncScheduler(frappe_sync.FrappeSync(database, frappe_config))
        _logger.info("Frappe HR push enabled: %s", frappe_config.url)
    run_forever(
        database,
        interval_seconds=interval,
        spool=spool,
        device_id=device_id,
        frappe=frappe,
        status_path=collector_status_path(),
    )


if __name__ == "__main__":
    main()
