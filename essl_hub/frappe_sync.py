"""Push punches into Frappe HR (HRMS v15/v16) as Employee Checkins.

Mirrors frappe/biometric-attendance-sync-tool: each punch is POSTed to
``hrms.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field``
(matched on Employee.attendance_device_id by default), and after a clean run
each configured Shift Type gets ``last_sync_of_checkin`` so Auto Attendance
knows checkins up to that moment are complete.

Per-punch state lives in ``frappe_sync_log``: a row means "done" (synced or
permanently skipped). Retryable failures (network, 5xx, auth, rate limit)
write nothing, so the punch is retried next cycle.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .db import TIMESTAMP_FORMAT, Database, OfflineError

_logger = logging.getLogger(__name__)

CHECKIN_METHOD = (
    "/api/method/hrms.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field"
)
LOG_TYPES = {"entry": "IN", "exit": "OUT"}

# Frappe error substrings the official sync tool treats as final for a log.
EMPLOYEE_NOT_FOUND = "No Employee found for the given employee field value"
EMPLOYEE_INACTIVE = "Transactions cannot be created for an Inactive Employee"
DUPLICATE_CHECKIN = "This employee already has a log with the same timestamp"

# HTTP statuses worth retrying next cycle; other 4xx are permanent for the log.
_RETRY_STATUSES = {401, 403, 408, 409, 417, 429}

STATUS_SETTING = "frappe_last_sync"
STATUS_HEARTBEAT_SECONDS = 900


@dataclass(frozen=True)
class FrappeConfig:
    url: str
    api_key: str
    api_secret: str
    interval_seconds: int = 60
    batch_size: int = 200
    employee_fieldname: str = "attendance_device_id"
    shift_types: tuple[str, ...] = ()
    since: datetime | None = None
    timeout: float = 15.0

    @property
    def auth_header(self) -> str:
        return f"token {self.api_key}:{self.api_secret}"


def config_from_env(env: dict[str, str] | None = None) -> FrappeConfig | None:
    """None unless FRAPPE_URL, FRAPPE_API_KEY and FRAPPE_API_SECRET are all set."""
    env = os.environ if env is None else env
    url = (env.get("FRAPPE_URL") or "").strip().rstrip("/")
    key = (env.get("FRAPPE_API_KEY") or "").strip()
    secret = (env.get("FRAPPE_API_SECRET") or "").strip()
    if not (url and key and secret):
        return None
    since_raw = (env.get("FRAPPE_SYNC_SINCE") or "").strip()
    since = datetime.fromisoformat(since_raw) if since_raw else None
    if since is not None and since.tzinfo is not None:
        raise RuntimeError("FRAPPE_SYNC_SINCE must be a local time without offset")
    shift_types = tuple(
        s.strip() for s in (env.get("FRAPPE_SHIFT_TYPES") or "").split(",") if s.strip()
    )
    return FrappeConfig(
        url=url,
        api_key=key,
        api_secret=secret,
        interval_seconds=int(env.get("FRAPPE_SYNC_INTERVAL_SECONDS") or 60),
        batch_size=max(1, int(env.get("FRAPPE_SYNC_BATCH_SIZE") or 200)),
        employee_fieldname=(env.get("FRAPPE_EMPLOYEE_FIELDNAME") or "attendance_device_id").strip(),
        shift_types=shift_types,
        since=since,
    )


class RetryLater(Exception):
    """Transient failure: stop this run, retry the same punch next cycle."""


@dataclass
class HttpResult:
    status: int
    body: str


# (method, url, headers, body bytes, timeout) -> HttpResult; raises OSError on network failure.
Transport = Callable[[str, str, dict[str, str], bytes, float], HttpResult]


def urllib_transport(method: str, url: str, headers: dict, body: bytes, timeout: float) -> HttpResult:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResult(response.status, response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, exc.read().decode("utf-8", "replace"))


def _error_text(body: str) -> str:
    """Frappe puts the message in exception/_server_messages; fall back to raw body."""
    try:
        payload = json.loads(body)
    except ValueError:
        return body[:500]
    parts = [str(payload.get("exception") or "")]
    messages = payload.get("_server_messages")
    if messages:
        try:
            for item in json.loads(messages):
                parts.append(str(json.loads(item).get("message", item)))
        except (ValueError, AttributeError, TypeError):
            parts.append(str(messages))
    text = " | ".join(p for p in parts if p)
    return (text or body)[:500]


@dataclass
class SyncReport:
    synced: int = 0
    skipped: int = 0
    retry_error: str | None = None
    shift_types_updated: list[str] = field(default_factory=list)


class FrappeSync:
    def __init__(
        self,
        database: Database,
        config: FrappeConfig,
        *,
        transport: Transport = urllib_transport,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.database = database
        self.config = config
        self.transport = transport
        self.clock = clock
        self._status_shape: tuple | None = None
        self._status_at = 0.0

    def _request(self, method: str, path: str, payload: dict) -> HttpResult:
        headers = {
            "Authorization": self.config.auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        url = self.config.url + urllib.parse.quote(path, safe="/:")
        try:
            return self.transport(
                method, url, headers, json.dumps(payload).encode(), self.config.timeout
            )
        except OSError as exc:  # URLError, timeouts, connection refused
            raise RetryLater(f"network: {exc}") from exc

    def push_punch(self, punch: dict) -> str:
        """Push one punch; return the recorded status. Raises RetryLater."""
        user_id = (punch.get("device_user_id") or "").strip()
        timestamp = punch.get("event_timestamp")
        if not user_id or timestamp is None:
            self.database.record_frappe_sync(
                punch["id"], status="skipped", error="missing device user id or timestamp"
            )
            return "skipped"
        payload: dict[str, object] = {
            "employee_field_value": user_id,
            "timestamp": timestamp.strftime(TIMESTAMP_FORMAT),
            "device_id": punch.get("device_id"),
            "employee_fieldname": self.config.employee_fieldname,
        }
        log_type = LOG_TYPES.get(punch.get("trusted_direction") or "")
        if log_type:
            payload["log_type"] = log_type
        result = self._request("POST", CHECKIN_METHOD, payload)
        if result.status == 200:
            try:
                name = json.loads(result.body)["message"]["name"]
            except (ValueError, KeyError, TypeError):
                name = None
            self.database.record_frappe_sync(punch["id"], status="synced", checkin_name=name)
            return "synced"
        error = _error_text(result.body)
        if DUPLICATE_CHECKIN in error:
            self.database.record_frappe_sync(punch["id"], status="synced", error="duplicate in Frappe")
            return "synced"
        if EMPLOYEE_NOT_FOUND in error or EMPLOYEE_INACTIVE in error:
            self.database.record_frappe_sync(punch["id"], status="skipped", error=error)
            return "skipped"
        if result.status >= 500 or result.status in _RETRY_STATUSES:
            raise RetryLater(f"HTTP {result.status}: {error}")
        # Remaining 4xx (validation errors): permanent for this log.
        self.database.record_frappe_sync(
            punch["id"], status="failed", error=f"HTTP {result.status}: {error}"
        )
        return "failed"

    def update_shift_types(self, report: SyncReport) -> None:
        """Set Shift Type.last_sync_of_checkin to the oldest successful device
        poll, only moving forward, like the official sync tool."""
        if not self.config.shift_types:
            return
        sync_at = self.database.min_device_last_success()
        if sync_at is None:
            return
        value = sync_at.strftime(TIMESTAMP_FORMAT)
        for shift in self.config.shift_types:
            key = f"frappe_shift_sync:{shift}"
            previous = self.database.setting(key)
            if previous is not None and previous >= value:
                continue
            result = self._request(
                "PUT", f"/api/resource/Shift Type/{shift}", {"last_sync_of_checkin": value}
            )
            if result.status == 200:
                self.database.set_setting(key, value)
                report.shift_types_updated.append(shift)
            else:
                _logger.warning(
                    "Shift Type %s last_sync_of_checkin update failed: HTTP %s %s",
                    shift, result.status, _error_text(result.body),
                )

    def run_once(self) -> SyncReport:
        report = SyncReport()
        if getattr(self.database, "offline", False):
            # KTD13: no sync-log or status writes while Turso is unreachable;
            # the next healthy run picks up every pending punch.
            report.retry_error = "Turso is unreachable"
            _logger.warning("Frappe sync skipped: Turso is unreachable")
            return report
        try:
            while True:
                batch = self.database.frappe_pending_punches(
                    since=self.config.since, limit=self.config.batch_size
                )
                for punch in batch:
                    if self.push_punch(punch) == "synced":
                        report.synced += 1
                    else:
                        report.skipped += 1
                if len(batch) < self.config.batch_size:
                    break
            # Only advance shift sync when every punch is pushed.
            self.update_shift_types(report)
        except OfflineError as exc:
            report.retry_error = str(exc)
            _logger.warning("Frappe sync stopped: %s", exc)
            return report
        except RetryLater as exc:
            report.retry_error = str(exc)
            _logger.warning("Frappe sync paused, retrying next cycle: %s", exc)
        self._save_status(report)
        return report

    def _save_status(self, report: SyncReport) -> None:
        status = {
            "at": self.clock().strftime(TIMESTAMP_FORMAT),
            "ok": report.retry_error is None,
            "synced": report.synced,
            "skipped": report.skipped,
            "error": report.retry_error,
        }
        # Every write is replicated to each Turso replica; an unchanged
        # status is only refreshed once per heartbeat.
        shape = (status["ok"], status["synced"], status["skipped"], status["error"])
        now = time.monotonic()
        if shape == self._status_shape and now - self._status_at < STATUS_HEARTBEAT_SECONDS:
            return
        try:
            self.database.set_setting(STATUS_SETTING, json.dumps(status))
            self._status_shape, self._status_at = shape, now
        except Exception:
            _logger.warning("could not record Frappe sync status", exc_info=True)


class SyncScheduler:
    """Runs FrappeSync at most every ``interval_seconds`` from the collector loop."""

    def __init__(self, sync: FrappeSync, *, clock: Callable[[], float] = time.monotonic):
        self.sync = sync
        self.clock = clock
        self._last: float | None = None

    def maybe_run(self) -> SyncReport | None:
        now = self.clock()
        if self._last is not None and now - self._last < self.sync.config.interval_seconds:
            return None
        self._last = now
        try:
            return self.sync.run_once()
        except Exception:
            _logger.warning("Frappe sync run failed; retrying next interval", exc_info=True)
            return None
