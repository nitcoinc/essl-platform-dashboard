"""Devices, settings and API keys."""

from __future__ import annotations

import ipaddress
import re
import secrets
from datetime import timedelta

from flask import Blueprint, Response, abort, g, redirect, render_template, request, url_for

from .. import access, config, hashing
from .common import (
    _ago,
    _day_start_hour,
    _login_required,
    _now_naive,
    _require_csrf,
    _role_required,
    _target_minutes,
    current_sync_health,
    database,
)

from .people import issue_count

bp = Blueprint("admin", __name__)


# --- Admin -------------------------------------------------------------

# The two device families the collector speaks. Keyed by the form value.
DEVICE_FAMILIES = {
    "x990": ("X990", "zk-attendance"),
    "c3-400": ("C3-400", "c3-pullsdk"),
}

# Collector replica sync (TURSO_COLLECTOR_SYNC_SECONDS, 300s) plus one poll.
PICKUP_MINUTES = 6


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
    if protocol == "zk-attendance" and password is not None:
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
            }
        )
    return rows


def _render_devices(status: int = 200, **overrides):
    context = {
        "employee": g.employee,
        "devices": _device_rows(),
        "unmapped": database.unmapped_device_user_ids(),
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
    }
    context.update(overrides)
    return render_template("settings.html", **context), status
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
