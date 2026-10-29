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

from .collectors import (
    CollectionResult,
    DoorStep,
    c3_door_step,
    c3_history_status,
    collect,
)
from .config import (
    C3_PROTOCOL,
    DEVICES,
    TIMEZONE_NAME,
    DeviceConfig,
    collector_interval_seconds,
    collector_status_path,
    heartbeat_write_seconds,
    spool_path,
)
from .db import SETUP_COMMANDS, Database, DatabaseMissing, OfflineError
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
        request = row.get("door_params_request")
        device = DeviceConfig(
            id=str(row["id"]),
            site=str(row.get("site") or (base.site if base else row["id"])),
            model=str(row.get("model") or (base.model if base else "")),
            ip=str(row["ip"]),
            port=int(row["port"]),
            protocol=str(protocol),
            password_env=base.password_env if base else "",
            password=row.get("comm_password") or None,
            door_denied_since=_as_datetime(row.get("door_denied_since")),
            door_request=request if isinstance(request, dict) else None,
            door_requested_at=_as_datetime(row.get("door_params_requested_at")),
        )
        if device_id is None or device.id == device_id:
            merged.append(device)
    return merged


def _as_datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


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
class _DoorAnswer:
    requested_at: datetime
    outcome: object
    recorded: bool = False


@dataclasses.dataclass
class PollState:
    """In-memory collector state (KTD11).

    ``written`` holds the device-row state last committed, so an unchanged idle
    poll writes nothing. ``ready`` turns True once the boot work (schema,
    seed) has succeeded (KTD10)."""

    heartbeat_seconds: float = 900
    clock: Callable[[], float] = time.monotonic
    written: dict[str, _Written] = dataclasses.field(default_factory=dict)
    connection_versions: dict[str, object] = dataclasses.field(default_factory=dict)
    ready: bool = False
    last_poll: str | None = None
    # (requested_at, outcome) of the last door request answered per device:
    # it is never sent again, and a failed result write is retried from here.
    door_handled: dict[str, _DoorAnswer] = dataclasses.field(default_factory=dict)


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
) -> bool:
    """Poll one device and write only what changed (KTD11).

    Punches and the device row commit together. If that write fails for any
    reason the punches go to ``spool`` (KTD9) and the device row is retried
    next cycle; without a spool the error propagates. Returns True when this
    poll read the C3 history table, which also refreshes the door read (U4)."""
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
    # held; skip on "nothing new" (a local read) instead.
    if (
        last is not None
        and write.get("transport") != "TCP history"
        and not _has_unseen(database, events)
        and last.status == status_key
        and last.connection_version == version
        and now - last.at < state.heartbeat_seconds
    ):
        return False
    history_read = write.get("transport") == "TCP history"

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
        return history_read
    state.written[device.id] = _Written(status_key, version, now)
    return history_read


def door_once(
    database: Database,
    device: DeviceConfig,
    *,
    door_step: DoorStep = c3_door_step,
    history_read: bool = False,
    state: PollState | None = None,
    now: datetime | None = None,
) -> None:
    """C3 door parameters (U4): answer a pending request, or refresh the read
    alongside a history read. Runs after the poll committed, on its own
    connection; its failures never touch the punches.

    A door_step error (panel unreachable before any write) leaves the request
    pending. A result is recorded, then the request is cleared only if the web
    has not replaced it meanwhile (KTD6). A request answered once is never
    sent again: if recording its result failed, the saved outcome is
    recorded on the next cycle instead (never re-sent, never "expired")."""
    now = now or _ist_now()
    request = device.door_request
    requested_at = device.door_requested_at
    if request is None or requested_at is None:
        request = requested_at = None
    elif database.door_request_at(device.id) != requested_at:
        # The device list is read at cycle start; the admin has since
        # dismissed or replaced this request, so it must not reach the door.
        request = requested_at = None
    answer = state.door_handled.get(device.id) if state is not None else None
    if answer is None or answer.requested_at != requested_at:
        answer = None
    elif answer.recorded:
        # Answered and stored; only the clear is not visible yet.
        request = requested_at = answer = None
    if answer is not None:
        # Answered, but recording it failed: store that outcome, never re-send.
        outcome = answer.outcome
    else:
        if request is None and not history_read:
            return
        outcome = door_step(device, request, requested_at, now=now)
        if requested_at is not None and outcome.result is not None and state is not None:
            answer = state.door_handled[device.id] = _DoorAnswer(requested_at, outcome)
    if outcome.params is None and outcome.result is None:
        return
    database.record_door_params(
        device.id,
        params=outcome.params,
        read_at=outcome.read_at,
        result=outcome.result,
        previous=outcome.previous,
        previous_for=requested_at if outcome.previous is not None else None,
    )
    if requested_at is not None and outcome.result is not None:
        if not database.clear_door_request(device.id, requested_at):
            _logger.info("door request for %s was replaced meanwhile; the newer one stays", device.id)
        if answer is not None:
            answer.recorded = True
    if outcome.result is not None:
        _logger.info("door request for %s: %s", device.id, outcome.result.get("state"))


def _ist_now() -> datetime:
    # Naive IST (the web compares in IST); containers run in UTC.
    return datetime.now(ZoneInfo(TIMEZONE_NAME)).replace(tzinfo=None, microsecond=0)


def _transient(exc: BaseException) -> bool:
    """Busy lock, unavailable or missing file: retry later, never dead-letter
    (KTD5, KTD6)."""
    return isinstance(exc, (OfflineError, DatabaseMissing))


def replay_spool(database: Database, spool: Spool) -> int:
    """Replay spooled batches oldest-first (KTD9); returns batches written.

    A permanent failure counts toward the dead-letter limit and replay moves
    on; a transient one (busy, missing file) stops replay uncounted."""
    written = 0
    for entry in spool.pending():
        try:
            database.insert_punches(entry.punches)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            if _transient(exc):
                _logger.warning("spool replay paused, database unavailable: %s", error)
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
    """Boot work (schema, seed), retried each cycle until it succeeds (KTD10).

    The collector never creates the database (KTD5): while the file is missing
    it logs the setup commands and waits; punches meanwhile go to the spool."""
    if state.ready:
        return True
    try:
        database.initialize(DEVICES)
        database.warn_device_drift(DEVICES)
    except DatabaseMissing as exc:
        _logger.warning(
            "database not set up (%s); waiting for one of: %s", exc, "; ".join(SETUP_COMMANDS)
        )
        return False
    except Exception as exc:
        _logger.warning("collector database setup failed; retrying next cycle: %s", exc)
        return False
    state.ready = True
    _logger.info("collector database ready")
    return True


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
        "spool_depth": spool.depth(),
        "spool_oldest_at": spool.oldest_at(),
        "dead_count": spool.dead_count(),
        "offline": not state.ready,
        # R4: per C3 device, when its history download last worked or since
        # when it has been failing (the Devices page shows it).
        "c3_history": c3_history_status(),
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
    door_step: DoorStep | None = c3_door_step,
) -> list[DeviceConfig]:
    """One collector cycle: boot work, replay, poll, door step, Frappe, status file."""
    ready = ensure_ready(database, state)
    devices = refresh_devices(database, devices, device_id=device_id, state=state)
    if ready:
        replay_spool(database, spool)
    for device in devices:
        history_read = False
        try:
            history_read = collect_once(database, device, collector=collector, state=state, spool=spool)
        except Exception:
            _logger.warning("poll cycle for %s failed; retrying next interval", device.id, exc_info=True)
        # Door writes need the database to record and clear them, so the step
        # waits for it; it never runs while the boot work is still failing.
        if ready and door_step is not None and device.protocol == C3_PROTOCOL:
            try:
                door_once(
                    database, device, door_step=door_step, history_read=history_read, state=state
                )
            except Exception as exc:
                _logger.warning("door settings step for %s failed: %s", device.id, exc)
    state.last_poll = _ist_now().isoformat(sep=" ")
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
    door_step: DoorStep | None = c3_door_step,
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
                door_step=door_step,
            )
        except Exception:
            # R16: nothing in a cycle may end the loop.
            _logger.warning("collector cycle failed; retrying next interval", exc_info=True)
        time.sleep(interval_seconds)


def main() -> None:
    interval = collector_interval_seconds()
    device_id = os.getenv("ESSL_COLLECTOR_DEVICE_ID") or None
    # KTD10: the spool and polling never wait on the database. It opens
    # lazily, local only, and is never created here (KTD5); run_cycle
    # retries the boot work each cycle until the file exists.
    spool = Spool(spool_path())
    database = Database(must_exist=True)
    if database.is_fetched_copy():
        raise SystemExit("this database is a fetched copy of a production snapshot; "
                         "the collector will not poll devices or push to Frappe from it")
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
