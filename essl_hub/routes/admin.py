"""Devices, settings and API keys."""

from __future__ import annotations

import ipaddress
import re
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, g, redirect, render_template, request, url_for

from .. import access, alert_settings, config, hashing
from ..db import (
    ALERT_LIVE,
    ALERT_MORNING,
    DUPLICATE_WINDOW_MAX,
    STATUS_BREAKER,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_SENT,
)
from ..door_params import (
    DOOR_REQUEST_TTL,
    PANEL_WIDE,
    PER_DOOR,
    DoorParamError,
    key_label,
    lock_count_of,
    param_for,
    validate_request,
    writable_keys,
)
from .common import (
    _ago,
    _day_start_hour,
    _login_required,
    _now_naive,
    _parse_ts,
    _require_csrf,
    _role_required,
    _target_minutes,
    _today,
    current_sync_health,
    database,
)

from .people import issue_count

bp = Blueprint("admin", __name__)


# --- Admin -------------------------------------------------------------

# The two device families the collector speaks. Keyed by the form value.
DEVICE_FAMILIES = {
    "x990": ("X990", config.X990_PROTOCOL),
    "c3-400": ("C3-400", config.C3_PROTOCOL),
}

# Collector reads device rows every poll (ESSL_COLLECTOR_INTERVAL_SECONDS, 60s), plus margin.
PICKUP_MINUTES = 2

C3_PROTOCOL = config.C3_PROTOCOL
# Quick-swipe window presets (U5); 0 counts every swipe.
WINDOW_PRESETS = (0, 5, 10, 60)
# Pre-implementation Check: the C3's directions are trusted after U3, the
# X990 has none, so a double tap there needs a small window.
WINDOW_ADVICE = {
    C3_PROTOCOL: "Recommended for this C3: 10 s.",
    config.X990_PROTOCOL: "Recommended for the X990: 5-10 s. It records no direction, so a "
    "double tap inside the window is not counted.",
}
_INTEGER = re.compile(r"-?\d+")
# Door request outcomes as the card names them (door_params result JSON).
DOOR_OUTCOME_LABELS = {
    "applied": "Applied",
    "unchanged": "Already set",
    "failed": "Failed",
    "not_attempted": "Not attempted",
}


def _validate_connection(
    raw_ip: str, raw_port: str, protocol: str, site: str, password: str | None
) -> tuple[str, int | None, str | None]:
    """Shared by add and edit so both enforce the same rules."""
    ip = raw_ip.strip()
    try:
        port: int | None = int(raw_port)
    except ValueError:
        port = None
    if not ip:
        return ip, port, "IP address is required"
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return ip, port, f"{ip} is not a valid IP address"
    if port is None or not 1 <= port <= 65535:
        return ip, port, "Port must be a whole number between 1 and 65535"
    if protocol == config.X990_PROTOCOL and password is not None:
        try:
            config.numeric_comm_password(site, password)
        except ValueError as exc:
            return ip, port, str(exc)
    return ip, port, None


def _address_clash(ip: str, port: int, exclude_id: str | None = None) -> str | None:
    """R9: two active devices never share an IP:port."""
    for other in database.devices_at_address(ip, port):
        if other["id"] != exclude_id and not other.get("retired_at"):
            return (
                f"{ip}:{port} is already used by {other['site']} {other['model']}. "
                "Use a different address, or retire that device first."
            )
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _new_device_id(site: str, model: str) -> str:
    """Immutable id from site and model; a numeric suffix on collision. Retired
    rows are never deleted, so an id is never reused."""
    base = "-".join(part for part in (_slug(site), _slug(model)) if part) or "device"
    candidate, n = base, 2
    while database.device_by_id(candidate) is not None:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _device_rows() -> list[dict]:
    # KTD11: the collector writes a device row only on change or every
    # heartbeat, so "last checked" older than two heartbeats means the
    # collector (or its host) has stopped, not that the device is idle.
    now = _now_naive()
    stale_after = timedelta(seconds=2 * config.heartbeat_write_seconds())
    interval = config.collector_interval_seconds()
    rows = []
    for device in database.devices():
        checked = device.get("last_attempt")
        updated = device.get("connection_updated_at")
        retired = bool(device.get("retired_at"))
        rows.append(
            {
                "device": device,
                "retired": retired,
                # Retired devices are not polled, so they never read as stale.
                "stale": not retired and checked is not None and now - checked > stale_after,
                "checked_ago": _ago(now - checked) if checked is not None else None,
                "pending": not retired
                and updated is not None
                and (checked is None or checked < updated),
                # Retired devices are not polled, so they get no door section.
                "door": None if retired else _door_view(device, now, interval),
            }
        )
    return rows


def _strict_int(raw: object) -> int | None:
    """Whole number from form text; "1.5", "abc", "0,ComPwd=1" are None."""
    text = str(raw or "").strip()
    return int(text) if _INTEGER.fullmatch(text) else None


def _dict_or_none(value: object) -> dict | None:
    return value if isinstance(value, dict) else None


def _value_text(key: str, value: object, *, unit: bool = False) -> str:
    """A door value as the card shows it: the option label for a select
    ("On"), the number otherwise; "not read" for a missing value."""
    if value is None:
        return "not read"
    found = param_for(key)
    if found is not None:
        param = found[0]
        labels = dict(param.options)
        if value in labels:
            return labels[value]
        if unit and param.unit:
            return f"{value} {param.unit}"
    return str(value)


def _door_field(key: str, param, value: object) -> dict:
    return {
        "key": key,
        "label": param.label,
        "full_label": key_label(key),
        "value": value,
        "text": _value_text(key, value, unit=True),
        "param": param,
    }


def _door_close_note(params: dict, lock_count: int) -> str:
    """One line on whether the panel can issue door-close denials at all
    (Pre-implementation Check 3: no sensor, InterLock 0, AntiPassback 0)."""
    sensors = [
        str(door) for door in range(1, lock_count + 1)
        if params.get(f"Door{door}SensorType") not in (None, 0)
    ]
    interlock, antipassback = params.get("InterLock"), params.get("AntiPassback")
    if not sensors and interlock == 0 and antipassback == 0:
        return ("Last read: no door sensor, InterLock 0, AntiPassback 0. "
                "The panel issues no door-close denials, so this has no effect today.")
    sensor_text = f"door sensor on door {', '.join(sensors)}" if sensors else "no door sensor"
    return (f"Last read: {sensor_text}, InterLock {_value_text('InterLock', interlock)}, "
            f"AntiPassback {_value_text('AntiPassback', antipassback)}.")


def _door_view(device: dict, now, interval_seconds: int) -> dict | None:
    """The C3 door-settings card state (U8), following the plan's diagram:

    - ``not_read``: no read and no request; the edit form stays hidden.
    - ``pending``: a request waits for the collector; ``stale`` once it is
      older than two poll intervals (it still expires after 15 minutes).
    - ``applied``: the last request applied, or a plain read.
    - ``failed`` / ``expired``: per-key results, with Dismiss.

    None for a device that is not a C3."""
    if device.get("protocol") != C3_PROTOCOL:
        return None
    params = _dict_or_none(device.get("door_params"))
    result = _dict_or_none(device.get("door_params_result"))
    previous = _dict_or_none(device.get("door_params_previous"))
    requested_at = device.get("door_params_requested_at")
    lock_count = lock_count_of(params)

    result_state = (result or {}).get("state")
    if requested_at is not None:
        state = "pending"
    elif result_state in ("failed", "expired"):
        state = result_state
    elif result is not None or params is not None:
        state = "applied"
    else:
        state = "not_read"

    results = []
    for key, entry in ((result or {}).get("keys") or {}).items():
        entry = entry if isinstance(entry, dict) else {}
        outcome = entry.get("outcome") or ""
        results.append({
            "label": key_label(key),
            "old": _value_text(key, entry.get("old")),
            "new": _value_text(key, entry.get("new")),
            "outcome": outcome,
            "outcome_label": DOOR_OUTCOME_LABELS.get(outcome, outcome),
            "message": entry.get("message") or "",
        })

    panel, doors = [], []
    if params is not None and lock_count is not None:
        panel = [_door_field(p.name, p, params.get(p.name)) for p in PANEL_WIDE]
        for door in range(1, lock_count + 1):
            fields = [_door_field(f"Door{door}{p.name}", p, params.get(f"Door{door}{p.name}"))
                      for p in PER_DOOR]
            doors.append({
                "number": door,
                "fields": fields,
                # Display only; never written (R12).
                "verify_type": params.get(f"Door{door}VerifyType"),
            })

    return {
        "state": state,
        "result_state": result_state,
        "result_message": (result or {}).get("message") or "",
        "finished_at": (result or {}).get("finished_at"),
        "results": results,
        "requested_at": requested_at,
        "stale": state == "pending"
        and now - requested_at > timedelta(seconds=2 * interval_seconds),
        "expires_at": requested_at + DOOR_REQUEST_TTL if requested_at is not None else None,
        "params": params,
        "read_at": device.get("door_params_read_at"),
        "lock_count": lock_count,
        "reader_count": (params or {}).get("ReaderCount"),
        "panel": panel,
        "doors": doors,
        # KTD8: no form before the first read; none while a request waits.
        "can_change": lock_count is not None and state != "pending",
        "has_snapshot": bool(previous),
        "snapshot_for": device.get("door_params_previous_for"),
        "close_note": _door_close_note(params, lock_count) if lock_count else None,
    }


def _render_devices(status: int = 200, **overrides):
    context = {
        "employee": g.employee,
        "devices": _device_rows(),
        "issue_count": issue_count(),
        "can_edit": g.employee["role"] == "admin" and bool(g.csrf_token),
        "csrf_token": g.csrf_token,
        "saved": request.args.get("saved"),
        "collector_interval_seconds": config.collector_interval_seconds(),
        "stale_minutes": 2 * config.heartbeat_write_seconds() // 60,
        "device_error": None,
        "device_error_id": None,
        "device_form": None,
        "add_error": None,
        "add_form": None,
        "reenable_id": None,
        "families": DEVICE_FAMILIES,
        "pickup_minutes": PICKUP_MINUTES,
        "added": request.args.get("added"),
        "retired": request.args.get("retired"),
        "enabled": request.args.get("enabled"),
        "policy_saved": request.args.get("policy"),
        "policy_error": None,
        "policy_error_id": None,
        "policy_form": None,
        "window_presets": WINDOW_PRESETS,
        "window_max": DUPLICATE_WINDOW_MAX,
        "window_advice": WINDOW_ADVICE,
        "door_filed": request.args.get("door"),
        "door_dismissed": request.args.get("dismissed"),
        "door_error": None,
        "door_error_id": None,
        "poll_minutes": max(1, round(config.collector_interval_seconds() / 60)),
    }
    context.update(overrides)
    return render_template("devices.html", **context), status


def _render_settings(status: int = 200, **overrides):
    context = {
        "employee": g.employee,
        "target_minutes": _target_minutes(database),
        "api_keys": database.api_keys(),
        "new_api_key": None,
        "key_error": None,
        "csrf_token": g.csrf_token,
        "target_saved": request.args.get("saved") == "target",
        "day_start_hour": _day_start_hour(database),
        "day_start_saved": request.args.get("saved") == "day-start",
        "day_start_error": None,
        "alerts": _alerts_panel(),
        "alerts_saved": request.args.get("saved") == "alerts",
        "alerts_form": None,
        "alerts_errors": {},
        "alert_mode_labels": ALERT_MODE_LABELS,
    }
    context.update(overrides)
    return render_template("settings.html", **context), status


# --- Attendance alerts panel (U9, R15) ---------------------------------

ALERT_MODE_LABELS = {
    alert_settings.MODE_OFF: "Off",
    alert_settings.MODE_SHADOW: "Shadow — log only",
    alert_settings.MODE_LIVE: "Live — send emails",
}
# Would-have-sent counts cover this many work days, today included (KTD11).
ALERT_COUNT_DAYS = 7
# How an alert row reads in the counts table, in display order.
ALERT_BUCKETS = ("would send", STATUS_SENT, STATUS_QUEUED, STATUS_FAILED, "not emailed", "held")


def _alert_bucket(mode: str, status: str) -> str:
    """"held" is a candidate the circuit breaker stopped (KTD9); shadow rows
    are what live mode would have sent; live rows follow their outbox row,
    and a live row with none (no email address) was "not emailed"."""
    if status == STATUS_BREAKER:
        return "held"
    if mode == alert_settings.MODE_SHADOW:
        return "would send"
    return status if status in (STATUS_SENT, STATUS_QUEUED, STATUS_FAILED) else "not emailed"


def _alert_count_rows(since) -> list[dict]:
    """One row per site with (count, label) pairs for each alert kind."""
    sites: dict[str, dict[str, dict[str, int]]] = {}
    for row in database.alert_counts(since):
        site = row.get("site") or "Unknown site"
        cell = sites.setdefault(site, {}).setdefault(row["kind"], {})
        bucket = _alert_bucket(row["mode"], row["status"])
        cell[bucket] = cell.get(bucket, 0) + int(row["n"])
    return [
        {
            "site": site,
            **{
                kind: [(kinds.get(kind, {})[b], b) for b in ALERT_BUCKETS if kinds.get(kind, {}).get(b)]
                for kind in (ALERT_LIVE, ALERT_MORNING)
            },
        }
        for site, kinds in sorted(sites.items())
    ]


def _baselines_rebuilt_at() -> datetime | None:
    """The newest snapshot's time; the service's own run marker when there is
    no snapshot (nobody to baseline yet)."""
    return database.latest_baseline_at() or _parse_ts(database.setting(alert_settings.BASELINES_AT_KEY))


def _alerts_panel() -> dict:
    since = _today() - timedelta(days=ALERT_COUNT_DAYS - 1)
    people = database.employees()
    active_ids = {person["id"] for person in people}
    baselines = database.latest_baselines()
    rebuilt_at = _baselines_rebuilt_at()
    return {
        "settings": alert_settings.read(database),
        "counts": _alert_count_rows(since),
        "count_days": ALERT_COUNT_DAYS,
        "people": len(people),
        # A live limit needs enough clean workdays (R2); metrics is null until then.
        "live_ready": sum(
            1 for employee_id, row in baselines.items()
            if employee_id in active_ids and row.get("metrics") is not None
        ),
        "rebuilt_at": rebuilt_at,
    }


def _live_mode_problem() -> str | None:
    """Why live mode cannot be switched on now, or None (U9 live-mode guard):
    emails need a link back to the dashboard and someone to approve fixes."""
    try:
        url = config.public_url()
    except RuntimeError as exc:
        return str(exc)
    if url is None:
        return "Set ESSL_PUBLIC_URL first so emails can link back to the dashboard."
    if not database.active_hr_users():
        return "Add at least one HR user first."
    return None
@bp.get("/admin")
@_login_required
def admin_view() -> Response:
    # The old single admin console was split into Devices and Settings.
    return redirect(url_for("admin.devices_view"))


@bp.get("/devices")
@_role_required("admin")
def devices_view():
    return _render_devices()


@bp.get("/devices/ids")
@_role_required(*access.EDITOR_ROLES)
def id_issues_view():
    # Moved under People so HR can reach it without Devices access.
    return redirect(url_for("people.id_issues_view"), code=301)


@bp.post("/devices/<device_id>")
@_role_required("admin")
def devices_update(device_id: str):
    _require_csrf()
    device = database.device_by_id(device_id)
    if device is None:
        abort(404)

    raw_ip = request.form.get("ip") or ""
    raw_port = request.form.get("port") or ""
    raw_password = request.form.get("comm_password") or ""
    clear_password = request.form.get("clear_comm_password") is not None

    if clear_password:
        resolved_password: str | None = None
    elif raw_password.strip():
        resolved_password = raw_password.strip()
    else:
        resolved_password = device["comm_password"]

    ip, port, error = _validate_connection(
        raw_ip, raw_port, device["protocol"], device["site"], resolved_password
    )
    if error is None:
        error = _address_clash(ip, port, exclude_id=device_id)

    if error is not None:
        return _render_devices(
            device_error=error,
            device_error_id=device_id,
            device_form={
                "ip": raw_ip,
                "port": raw_port,
                "comm_password": raw_password,
                "clear_comm_password": clear_password,
            },
        )

    database.update_device(
        device_id, ip=ip, port=port, comm_password=resolved_password
    )
    return redirect(url_for("admin.devices_view", saved=device_id))


@bp.post("/devices/add")
@_role_required("admin")
def devices_add():
    _require_csrf()
    site = (request.form.get("site") or "").strip()
    family = request.form.get("family") or ""
    raw_ip = request.form.get("ip") or ""
    raw_port = request.form.get("port") or ""
    raw_password = request.form.get("comm_password") or ""
    password = raw_password.strip() or None
    form = {"site": site, "family": family, "ip": raw_ip, "port": raw_port,
            "comm_password": raw_password}

    error = None
    ip, port = raw_ip.strip(), None
    if not site:
        error = "Site name is required"
    elif family not in DEVICE_FAMILIES:
        error = "Choose a device family"
    else:
        model, protocol = DEVICE_FAMILIES[family]
        ip, port, error = _validate_connection(raw_ip, raw_port, protocol, site, password)

    reenable_id = None
    if error is None:
        matches = database.devices_at_address(ip, port)
        active = [d for d in matches if not d.get("retired_at")]
        if active:
            error = _address_clash(ip, port)
        elif matches:
            retired = matches[-1]
            reenable_id = retired["id"]
            error = (
                f"{ip}:{port} belongs to the retired device {retired['site']} "
                f"{retired['model']}. Re-enable it to keep its history under one id."
            )

    if error is not None:
        return _render_devices(add_error=error, add_form=form, reenable_id=reenable_id)

    device_id = _new_device_id(site, model)
    database.add_device(
        device_id=device_id, site=site, model=model, ip=ip, port=port,
        protocol=protocol, comm_password=password,
    )
    return redirect(url_for("admin.devices_view", added=device_id))


@bp.post("/devices/<device_id>/retire")
@_role_required("admin")
def devices_retire(device_id: str):
    _require_csrf()
    device = database.device_by_id(device_id)
    if device is None:
        abort(404)
    if not device.get("retired_at"):
        database.set_device_retired(device_id, True)
    return redirect(url_for("admin.devices_view", retired=device_id))


@bp.post("/devices/<device_id>/enable")
@_role_required("admin")
def devices_enable(device_id: str):
    _require_csrf()
    device = database.device_by_id(device_id)
    if device is None:
        abort(404)
    if device.get("retired_at"):
        error = _address_clash(str(device["ip"]), int(device["port"]), exclude_id=device_id)
        if error is not None:
            return _render_devices(
                device_error=error,
                device_error_id=device_id,
                device_form={"ip": device["ip"], "port": device["port"],
                             "comm_password": "", "clear_comm_password": False},
            )
        database.set_device_retired(device_id, False)
    return redirect(url_for("admin.devices_view", enabled=device_id))


def _device_anchor(device_id: str) -> str:
    return f"device-{device_id}-name"


@bp.post("/devices/<device_id>/policy")
@_role_required("admin")
def devices_policy(device_id: str):
    """U5: quick-swipe window (every device) and, on a C3, Wait for door close."""
    _require_csrf()
    device = database.device_by_id(device_id)
    if device is None:
        abort(404)
    raw = request.form.get("window_seconds") or ""
    # Unchecked boxes are not posted; the X990 has no door-close setting.
    wait = (
        request.form.get("wait_for_door_close") is not None
        if device["protocol"] == C3_PROTOCOL
        else True
    )
    window = _strict_int(raw)
    if window is None or not 0 <= window <= DUPLICATE_WINDOW_MAX:
        return _render_devices(
            status=400,
            policy_error=f"Enter whole seconds from 0 to {DUPLICATE_WINDOW_MAX}.",
            policy_error_id=device_id,
            policy_form={"window_seconds": raw, "wait_for_door_close": wait},
        )
    database.set_device_policy(
        device_id, window_seconds=window, wait_for_door_close=wait, actor_id=g.employee["id"]
    )
    return redirect(url_for("admin.devices_view", policy=device_id, _anchor=_device_anchor(device_id)))


def _door_device(device_id: str) -> dict:
    """Door routes act on a polled C3 only; anything else is not found."""
    device = database.device_by_id(device_id)
    if device is None or device.get("protocol") != C3_PROTOCOL or device.get("retired_at"):
        abort(404)
    return device


def _door_error(device_id: str, message: str, status: int):
    return _render_devices(status=status, door_error=message, door_error_id=device_id)


def _door_pending_error(device: dict):
    """One request at a time: a new one waits until the pending one is
    applied, expires or is dismissed."""
    if device.get("door_params_requested_at") is None:
        return None
    return _door_error(
        device["id"],
        "A door request is already waiting for the collector. Wait for it, or dismiss it.",
        409,
    )


def _door_confirm(device: dict, request_map: dict[str, int], *, restore: bool):
    current = _dict_or_none(device.get("door_params")) or {}
    changes = [
        {
            "label": key_label(key),
            "old": _value_text(key, current.get(key)),
            "new": _value_text(key, value),
        }
        for key, value in request_map.items()
    ]
    endpoint = "admin.devices_door_restore" if restore else "admin.devices_door"
    return render_template(
        "device_door_confirm.html",
        employee=g.employee,
        csrf_token=g.csrf_token,
        device=device,
        changes=changes,
        # Restore recomputes from the snapshot on confirm, so it posts no values.
        fields={} if restore else request_map,
        action=url_for(endpoint, device_id=device["id"]),
        restore=restore,
        snapshot_for=device.get("door_params_previous_for"),
        poll_minutes=max(1, round(config.collector_interval_seconds() / 60)),
    )


@bp.post("/devices/<device_id>/door")
@_role_required("admin")
def devices_door(device_id: str):
    """U8: changed door settings go to a confirm page; ``confirm=1`` files
    them for the collector. Every posted key and value goes through
    validate_request (KTD8), so a non-whitelisted key is refused, not dropped."""
    _require_csrf()
    device = _door_device(device_id)
    current = _dict_or_none(device.get("door_params")) or {}
    lock_count = lock_count_of(current)
    if lock_count is None:
        return _door_error(device_id, "Read the door settings from the device first.", 400)
    busy = _door_pending_error(device)
    if busy is not None:
        return busy

    allowed = set(writable_keys(lock_count))
    changes: dict[str, object] = {}
    for key in request.form:
        if key in ("csrf", "confirm"):
            continue
        values = request.form.getlist(key)
        raw = values[0].strip() if len(values) == 1 else ",".join(values)
        if key in allowed and raw == "":
            continue  # left blank: unchanged
        parsed = _strict_int(raw)
        value = parsed if parsed is not None else raw
        if key in allowed and value == current.get(key):
            continue
        changes[key] = value
    try:
        clean = validate_request(changes, lock_count)
    except DoorParamError as exc:
        return _door_error(device_id, str(exc), 400)
    if not clean:
        return _door_error(device_id, "Nothing changed.", 200)

    if request.form.get("confirm") != "1":
        return _door_confirm(device, clean, restore=False)
    database.file_door_request(device_id, clean, kind="door_write", actor_id=g.employee["id"])
    return redirect(url_for("admin.devices_view", door=device_id, _anchor=_device_anchor(device_id)))


@bp.post("/devices/<device_id>/door/read")
@_role_required("admin")
def devices_door_read(device_id: str):
    _require_csrf()
    device = _door_device(device_id)
    busy = _door_pending_error(device)
    if busy is not None:
        return busy
    database.file_door_request(device_id, {}, kind="door_read", actor_id=g.employee["id"])
    return redirect(url_for("admin.devices_view", door=device_id, _anchor=_device_anchor(device_id)))


@bp.post("/devices/<device_id>/door/restore")
@_role_required("admin")
def devices_door_restore(device_id: str):
    """KTD9: put back the values read before the last write, for the
    whitelisted keys that now differ. Same confirm page as a change."""
    _require_csrf()
    device = _door_device(device_id)
    previous = _dict_or_none(device.get("door_params_previous"))
    if not previous:
        abort(400)
    busy = _door_pending_error(device)
    if busy is not None:
        return busy
    current = _dict_or_none(device.get("door_params")) or {}
    lock_count = lock_count_of(current)
    if lock_count is None:
        return _door_error(device_id, "Read the door settings from the device first.", 400)
    restore = {
        key: previous[key]
        for key in writable_keys(lock_count)
        if key in previous and previous[key] != current.get(key)
    }
    try:
        clean = validate_request(restore, lock_count)
    except DoorParamError as exc:
        return _door_error(device_id, str(exc), 400)
    if not clean:
        return _door_error(device_id, "The door settings already match the saved values.", 200)

    if request.form.get("confirm") != "1":
        return _door_confirm(device, clean, restore=True)
    database.file_door_request(device_id, clean, kind="door_restore", actor_id=g.employee["id"])
    return redirect(url_for("admin.devices_view", door=device_id, _anchor=_device_anchor(device_id)))


@bp.post("/devices/<device_id>/door/dismiss")
@_role_required("admin")
def devices_door_dismiss(device_id: str):
    """Clear a failed or expired result (or a request the collector never
    picked up); files nothing."""
    _require_csrf()
    _door_device(device_id)
    database.dismiss_door_request(device_id, actor_id=g.employee["id"])
    return redirect(url_for("admin.devices_view", dismissed=device_id, _anchor=_device_anchor(device_id)))
@bp.get("/health")
@_login_required
def health_view():
    # 404, not 403: the page's existence is admin-only knowledge (R4).
    if g.employee["role"] != "admin":
        abort(404)
    health, exports = current_sync_health(database)
    return render_template(
        "health.html", employee=g.employee, health=health, exports=list(reversed(exports))[:14])


@bp.get("/settings")
@_role_required("admin")
def settings_view():
    return _render_settings()


@bp.post("/settings/target")
@_role_required("admin")
def settings_set_target() -> Response:
    _require_csrf()
    hours = request.form.get("hours", 0, type=int)
    minutes = request.form.get("minutes", 0, type=int)
    total = max(1, min(24 * 60, hours * 60 + minutes))
    database.set_setting("daily_target_minutes", str(total))
    return redirect(url_for("admin.settings_view", saved="target"))


@bp.post("/settings/day-start")
@_role_required("admin")
def settings_set_day_start():
    _require_csrf()
    raw = (request.form.get("hour") or "").strip()
    if not raw.isdigit() or not 0 <= int(raw) <= 23:
        return _render_settings(
            status=400,
            day_start_error="Enter a whole hour from 0 to 23.",
        )
    database.set_setting("day_start_hour", str(int(raw)))
    return redirect(url_for("admin.settings_view", saved="day-start"))


@bp.post("/settings/alerts")
@_role_required("admin")
def settings_set_alerts():
    _require_csrf()
    form = {
        name: (request.form.get(name) or "").strip()
        for name in ("mode", "start_hour", "end_hour", "morning_hour")
    }
    errors: dict[str, str] = {}
    checks = (
        ("mode", alert_settings.validate_mode),
        ("start_hour", lambda raw: alert_settings.validate_hour(raw, "Alert start hour")),
        ("end_hour", lambda raw: alert_settings.validate_hour(raw, "Alert end hour")),
        ("morning_hour", lambda raw: alert_settings.validate_hour(raw, "Morning email hour")),
    )
    values: dict[str, object] = {}
    for name, check in checks:
        try:
            values[name] = check(form[name])
        except alert_settings.SettingsError as exc:
            errors[name] = str(exc)
    if "start_hour" in values and "end_hour" in values:
        try:
            alert_settings.validate_hours_range(values["start_hour"], values["end_hour"])
        except alert_settings.SettingsError as exc:
            errors["end_hour"] = str(exc)
    if not errors and values["mode"] == alert_settings.MODE_LIVE:
        problem = _live_mode_problem()
        if problem is not None:
            errors["mode"] = problem
    if errors:
        return _render_settings(status=400, alerts_form=form, alerts_errors=errors)
    alert_settings.write(
        database,
        alert_settings.parse(
            values["mode"], values["start_hour"], values["end_hour"], values["morning_hour"]
        ),
    )
    return redirect(url_for("admin.settings_view", saved="alerts"))


@bp.post("/settings/keys")
@_role_required("admin")
def settings_create_key():
    _require_csrf()
    label = (request.form.get("label") or "").strip() or "HRMS key"
    if database.active_api_key_count() >= 2:
        return _render_settings(
            status=409,
            key_error="Two keys are already active. Revoke one before creating another.",
        )
    return _render_settings(new_api_key=_generate_api_key(label=label))


@bp.post("/settings/keys/<prefix>/revoke")
@_role_required("admin")
def settings_revoke_key(prefix: str) -> Response:
    _require_csrf()
    database.revoke_api_key(prefix)
    return redirect(url_for("admin.settings_view"))
def _generate_api_key(*, label: str) -> str:
    raw_key = "ak_live_" + secrets.token_hex(24)
    prefix = raw_key[:12]
    key_hash = hashing.sha256_hex(raw_key)
    database.create_api_key(prefix=prefix, key_hash=key_hash, label=label)
    return raw_key
