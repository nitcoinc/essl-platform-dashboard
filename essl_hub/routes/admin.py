"""Devices, settings and API keys."""

from __future__ import annotations

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
        rows.append(
            {
                "device": device,
                "stale": checked is not None and now - checked > stale_after,
                "checked_ago": _ago(now - checked) if checked is not None else None,
                "pending": updated is not None and (checked is None or checked < updated),
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
    ip = raw_ip.strip()
    raw_port = request.form.get("port") or ""
    raw_password = request.form.get("comm_password") or ""
    clear_password = request.form.get("clear_comm_password") is not None

    try:
        port: int | None = int(raw_port)
    except ValueError:
        port = None

    error = None
    if not ip:
        error = "IP address is required"
    elif port is None or not 1 <= port <= 65535:
        error = "Port must be a whole number between 1 and 65535"

    if clear_password:
        resolved_password: str | None = None
    elif raw_password.strip():
        resolved_password = raw_password.strip()
    else:
        resolved_password = device["comm_password"]

    if (
        error is None
        and device["protocol"] == "zk-attendance"
        and resolved_password is not None
    ):
        try:
            config.numeric_comm_password(device["site"], resolved_password)
        except ValueError as exc:
            error = str(exc)

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
