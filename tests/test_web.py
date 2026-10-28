from __future__ import annotations

import io

import openpyxl
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from zoneinfo import ZoneInfo

# The application computes days and expiries in Asia/Kolkata (`essl_hub.auth._now`),
# so a test must never use the system clock: on a UTC CI runner the local date can
# be a day behind the app's, which silently moves seeded punches to the wrong day
# and expires session/challenge rows the app still considers live.
_IST = ZoneInfo("Asia/Kolkata")


def _ist_now() -> datetime:
    return datetime.now(_IST).replace(tzinfo=None)


def _ist_today():
    # The app's "today" is the work day containing now (default start 06:00).
    return (_ist_now() - timedelta(hours=6)).date()


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".packages"))
sys.path.insert(0, str(ROOT))

from essl_hub import roster
from essl_hub.config import DEVICES
from essl_hub.db import OfflineError
from essl_hub.hours import apply_corrections
from essl_hub.web import create_app


# Device columns added in schema v7, with their database defaults.
_V7_DEVICE_DEFAULTS = {
    "duplicate_window_seconds": 60,
    "door_denied_since": None,
    "door_params": None,
    "door_params_read_at": None,
    "door_params_previous": None,
    "door_params_previous_for": None,
    "door_params_request": None,
    "door_params_requested_at": None,
    "door_params_result": None,
    "policy_updated_at": None,
}


class FakeDatabase:
    """In-memory stand-in for essl_hub.db.Database, implementing the same
    interface, so the whole web layer can be exercised without Postgres."""

    def __init__(self) -> None:
        self.devices_by_id: dict[str, dict] = {}
        self.punches: list[dict] = []
        self.employees_by_id: dict[int, dict] = {}
        self.extra_ids: dict[str, int] = {}  # extra device user ID -> employee id
        self._next_employee_id = 1
        self.settings: dict[str, str] = {}
        self.login_codes: list[dict] = []
        self._next_login_code_id = 1
        self.sessions: dict[str, dict] = {}
        self.api_keys_list: list[dict] = []
        self.device_change_rows: list[dict] = []
        # Missed-punch alerts and fix requests (v8).
        self.report_links: list[dict] = []
        self.outbox: list[dict] = []
        self.alerts: list[dict] = []
        self.baselines: dict[int, dict] = {}
        self.fix_requests: dict[int, dict] = {}
        self.fix_lines: dict[int, dict] = {}
        self.corrections: list[dict] = []
        self.fix_events: list[dict] = []
        # Punch archive rules (v9).
        self.archive_rules: list[dict] = []
        # `offline` makes every write raise OfflineError, the way an
        # unavailable database does.
        self.offline = False

    def _check_online(self) -> None:
        if self.offline:
            raise OfflineError("database unavailable")

    # devices ------------------------------------------------------------

    def initialize(self, devices) -> None:
        self._check_online()
        for device in devices:
            self.devices_by_id[device.id] = {
                "id": device.id,
                "site": device.site,
                "model": device.model,
                "ip": device.ip,
                "port": device.port,
                "protocol": device.protocol,
                "status": "never_synced",
                "transport": None,
                "serial_number": None,
                "firmware": None,
                "last_attempt": None,
                "last_success": None,
                "last_error": None,
                "records_seen": 0,
                "comm_password": None,
                "connection_updated_at": None,
                "created_at": None,
                "retired_at": None,
                **_V7_DEVICE_DEFAULTS,
            }
        self.settings.setdefault("daily_target_minutes", "480")

    def devices(self) -> list[dict]:
        result = []
        for device in self.devices_by_id.values():
            stored = sum(1 for p in self.punches if p["device_id"] == device["id"])
            result.append({**device, "stored_punches": stored})
        return result

    def device_by_id(self, device_id: str):
        return self.devices_by_id.get(device_id)

    def device_id_summary(self):
        rows: dict[str, dict] = {}
        for p in self.punches:
            uid = p.get("device_user_id")
            if not uid or self._archived(p):
                continue
            row = rows.setdefault(uid, {"device_user_id": uid, "punch_count": 0, "first_seen": None, "last_seen": None, "device_name": "", "sites": set()})
            row["punch_count"] += 1
            row["first_seen"] = min(filter(None, [row["first_seen"], p["event_timestamp"]]))
            # Device name is the latest punch's, like the SQL.
            if row["last_seen"] is None or p["event_timestamp"] >= row["last_seen"]:
                row["device_name"] = p.get("employee_name") or ""
            row["last_seen"] = max(filter(None, [row["last_seen"], p["event_timestamp"]]))
            if p.get("site"):
                row["sites"].add(p["site"])
        return [{**r, "sites": ",".join(sorted(r["sites"]))} for r in rows.values()]

    def min_device_last_success(self):
        values = [d.get("last_success") for d in self.devices_by_id.values()]
        if not values or any(v is None for v in values):
            return None
        return min(values)

    def unreachable_device_details(self) -> list[dict]:
        rows = [d for d in self.devices_by_id.values() if d["retired_at"] is None and d["status"] == "error"]
        return [{"id": d["id"], "name": f"{d['site']} {d['model']}", "last_success": d["last_success"]}
                for d in sorted(rows, key=lambda d: (d["site"], d["id"]))]

    def update_device(self, device_id: str, *, ip: str, port: int, comm_password):
        device = self.devices_by_id[device_id]
        device["ip"] = ip
        device["port"] = port
        device["comm_password"] = comm_password
        device["connection_updated_at"] = _ist_now()

    def add_device(self, *, device_id, site, model, ip, port, protocol, comm_password) -> None:
        self._check_online()
        if device_id in self.devices_by_id:
            raise ValueError("duplicate device id")
        now = _ist_now()
        self.devices_by_id[device_id] = {
            "id": device_id, "site": site, "model": model, "ip": ip, "port": port,
            "protocol": protocol, "status": "never_synced", "transport": None,
            "serial_number": None, "firmware": None, "last_attempt": None,
            "last_success": None, "last_error": None, "records_seen": 0,
            "comm_password": comm_password, "connection_updated_at": now,
            "created_at": now, "retired_at": None, **_V7_DEVICE_DEFAULTS,
        }

    def set_device_retired(self, device_id: str, retired: bool) -> None:
        self._check_online()
        self.devices_by_id[device_id]["retired_at"] = _ist_now() if retired else None

    def devices_at_address(self, ip: str, port: int) -> list[dict]:
        return [d for d in self.devices_by_id.values() if d["ip"] == ip and d["port"] == port]

    # swipe policy and door parameters (v7) -------------------------------

    def _change(self, device_id, actor_id, kind, old_value, new_value) -> None:
        self.device_change_rows.append({
            "id": len(self.device_change_rows) + 1, "device_id": device_id,
            "actor_employee_id": actor_id, "kind": kind, "old_value": old_value,
            "new_value": new_value, "created_at": _ist_now(),
        })

    def set_device_policy(self, device_id, *, window_seconds, wait_for_door_close, actor_id=None) -> bool:
        self._check_online()
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, int) \
                or not 0 <= window_seconds <= 300:
            raise ValueError("window must be 0-300 seconds")
        device = self.devices_by_id.get(device_id)
        if device is None:
            return False
        if device["protocol"] == "zk-attendance" and window_seconds < 30:
            raise ValueError("window must be at least 30 seconds on this device")
        old = {"duplicate_window_seconds": device["duplicate_window_seconds"],
               "wait_for_door_close": device["door_denied_since"] is None}
        now = _ist_now()
        device["duplicate_window_seconds"] = window_seconds
        device["door_denied_since"] = None if wait_for_door_close else (device["door_denied_since"] or now)
        device["policy_updated_at"] = now
        self._change(device_id, actor_id, "policy", old,
                     {"duplicate_window_seconds": window_seconds,
                      "wait_for_door_close": bool(wait_for_door_close)})
        return True

    def file_door_request(self, device_id, request, *, kind="door_write", requested_at=None, actor_id=None) -> bool:
        self._check_online()
        device = self.devices_by_id.get(device_id)
        if device is None:
            return False
        current = device["door_params"] or {}
        device["door_params_request"] = dict(request)
        device["door_params_requested_at"] = requested_at or _ist_now()
        self._change(device_id, actor_id, kind, {k: current.get(k) for k in request}, dict(request))
        return True

    def dismiss_door_request(self, device_id, *, actor_id=None) -> bool:
        self._check_online()
        device = self.devices_by_id.get(device_id)
        if device is None:
            return False
        old = {"request": device["door_params_request"],
               "requested_at": device["door_params_requested_at"],
               "result": device["door_params_result"]}
        device["door_params_request"] = None
        device["door_params_requested_at"] = None
        device["door_params_result"] = None
        self._change(device_id, actor_id, "door_dismiss", old, None)
        return True

    def record_door_params(self, device_id, *, params, read_at, result=None, previous=None, previous_for=None) -> None:
        device = self.devices_by_id[device_id]
        device["door_params"] = dict(params)
        device["door_params_read_at"] = read_at
        if result is not None:
            device["door_params_result"] = dict(result)
        if previous is not None and previous_for is not None \
                and device["door_params_previous_for"] != previous_for:
            device["door_params_previous"] = dict(previous)
            device["door_params_previous_for"] = previous_for

    def clear_door_request(self, device_id, requested_at) -> bool:
        device = self.devices_by_id[device_id]
        if device["door_params_requested_at"] != requested_at:
            return False
        device["door_params_request"] = None
        device["door_params_requested_at"] = None
        return True

    def device_windows(self) -> dict[str, int]:
        return {d["id"]: d["duplicate_window_seconds"] for d in self.devices_by_id.values()}

    def record_device_change(self, device_id, *, kind, old_value, new_value, actor_id=None) -> None:
        self._check_online()
        self._change(device_id, actor_id, kind, old_value, new_value)

    def device_changes(self, device_id, limit=20) -> list[dict]:
        rows = [r for r in self.device_change_rows if r["device_id"] == device_id]
        return [
            {**r, "actor_name": (self.employees_by_id.get(r["actor_employee_id"]) or {}).get("name")}
            for r in reversed(rows)
        ][:limit]

    # punches --------------------------------------------------------------

    def insert_punches(self, punches) -> int:
        inserted = 0
        for punch in punches:
            if any(p["fingerprint"] == punch["fingerprint"] for p in self.punches):
                continue
            self.punches.append(dict(punch))
            inserted += 1
        return inserted

    def _overlaid_punches(self, wanted, start: datetime, end: datetime) -> list[dict]:
        """Database._overlaid_punches: owners via main and extra IDs, then the
        same pure merge of approved fixes (KTD4)."""
        owner_of = {e["device_user_id"]: e["id"] for e in self.employees_by_id.values()}
        owner_of.update(self.extra_ids)
        main_ids = {owner_of[uid]: self.employees_by_id[owner_of[uid]]["device_user_id"]
                    for uid in wanted if uid in owner_of}
        rows = [
            {
                "device_user_id": p.get("device_user_id"),
                "employee_id": owner_of.get(p.get("device_user_id")),
                "fingerprint": p.get("fingerprint"),
                "event_timestamp": p["event_timestamp"],
                "trusted_direction": p.get("trusted_direction"),
                "raw_direction_byte": p.get("raw_direction_byte"),
                "raw_state": p.get("raw_state", ""),
                "site": p.get("site"),
                "device_id": p.get("device_id"),
            }
            for p in self.punches
            if p.get("device_user_id") in wanted and start <= p["event_timestamp"] < end
            and not self._archived(p)
        ]
        rows.sort(key=lambda p: p["event_timestamp"])
        return apply_corrections(rows, self.corrections_for(main_ids, start, end), main_ids)

    def punches_for_employee_range(self, device_user_ids, start: datetime, end: datetime):
        wanted = {device_user_ids} if isinstance(device_user_ids, str) else set(device_user_ids)
        return self._overlaid_punches(wanted, start, end)

    def first_punch_at(self, device_user_ids):
        wanted = set(device_user_ids)
        stamps = [p["event_timestamp"] for p in self.punches
                  if p.get("device_user_id") in wanted and not self._archived(p)]
        return min(stamps) if stamps else None

    def punches_for_device_users_range(self, device_user_ids, start: datetime, end: datetime):
        rows = self._overlaid_punches(set(device_user_ids), start, end)
        return sorted(rows, key=lambda p: (p["device_user_id"], p["event_timestamp"]))

    def unmapped_device_user_ids(self):
        mapped = {e["device_user_id"] for e in self.employees_by_id.values()} | set(self.extra_ids)
        counts: dict[str, int] = {}
        for p in self.punches:
            uid = p.get("device_user_id")
            if uid and uid not in mapped and not self._archived(p):
                counts[uid] = counts.get(uid, 0) + 1
        return [{"device_user_id": uid, "punch_count": n} for uid, n in sorted(counts.items())]

    # punch archives (v9) ---------------------------------------------------

    @staticmethod
    def _covers(rule: dict, p: dict) -> bool:
        return (p.get("device_user_id") == rule["device_user_id"]
                and (rule["from_ts"] is None or p["event_timestamp"] >= rule["from_ts"])
                and p["event_timestamp"] < rule["to_ts"])

    def _archived(self, p: dict) -> bool:
        """Database._ACTIVE_PUNCH_SQL, negated: an active rule covers it."""
        return any(r["restored_at"] is None and self._covers(r, p) for r in self.archive_rules)

    def _archive_range(self, from_ts, to_ts):
        from essl_hub.db import RuleViolation

        now = _ist_now().replace(microsecond=0)
        to_ts = now if to_ts is None else min(to_ts.replace(microsecond=0), now)
        if from_ts is not None and from_ts >= to_ts:
            raise RuleViolation("The from date must be before the to date.")
        return from_ts, to_ts

    def archive_preview(self, device_user_id, from_ts, to_ts) -> dict:
        from_ts, to_ts = self._archive_range(from_ts, to_ts)
        rule = {"device_user_id": device_user_id, "from_ts": from_ts, "to_ts": to_ts}
        stamps = sorted(p["event_timestamp"] for p in self.punches
                        if self._covers(rule, p) and not self._archived(p))
        return {"device_user_id": device_user_id, "from_ts": from_ts, "to_ts": to_ts, "count": len(stamps),
                "first": stamps[0] if stamps else None, "last": stamps[-1] if stamps else None}

    def archive_punches(self, device_user_id, *, from_ts, to_ts, reason=None, actor_id=None) -> int:
        self._check_online()
        from_ts, to_ts = self._archive_range(from_ts, to_ts)
        rule = {"id": len(self.archive_rules) + 1, "device_user_id": device_user_id, "from_ts": from_ts,
                "to_ts": to_ts, "reason": (reason or "").strip()[:200] or None, "created_by": actor_id,
                "created_at": _ist_now(), "restored_by": None, "restored_at": None}
        self.archive_rules.append(rule)
        return rule["id"]

    def restore_punch_archive(self, archive_id, *, actor_id=None) -> bool:
        self._check_online()
        rule = next((r for r in self.archive_rules if r["id"] == archive_id), None)
        if rule is None or rule["restored_at"] is not None:
            return False
        rule.update(restored_at=_ist_now(), restored_by=actor_id)
        return True

    def _archive_view(self, rule: dict) -> dict:
        active = rule["restored_at"] is None
        return {**rule, "created_by_name": self._name(rule["created_by"]),
                "restored_by_name": self._name(rule["restored_by"]),
                "hidden_count": sum(self._covers(rule, p) for p in self.punches) if active else None}

    def punch_archive(self, archive_id):
        rule = next((r for r in self.archive_rules if r["id"] == archive_id), None)
        return self._archive_view(rule) if rule else None

    def punch_archives(self, include_restored: bool = True) -> list[dict]:
        rules = [r for r in self.archive_rules if include_restored or r["restored_at"] is None]
        rules.sort(key=lambda r: (r["restored_at"] is not None, r["restored_at"] or r["created_at"], r["id"]),
                   reverse=True)
        rules.sort(key=lambda r: r["restored_at"] is not None)
        return [self._archive_view(r) for r in rules]

    def active_archive_for(self, device_user_id):
        rules = [r for r in self.archive_rules if r["device_user_id"] == device_user_id and r["restored_at"] is None]
        if not rules:
            return None
        hidden = sum(1 for p in self.punches if p.get("device_user_id") == device_user_id and self._archived(p))
        return {"archives": len(rules), "hidden": hidden}

    # employees ------------------------------------------------------------

    def upsert_employee(self, *, device_user_id: str, name: str, email: str) -> str:
        existing = next(
            (e for e in self.employees_by_id.values() if e["device_user_id"] == device_user_id),
            None,
        )
        if existing:
            existing["name"] = name
            existing["email"] = email
            return "updated"
        employee = {
            "id": self._next_employee_id,
            "device_user_id": device_user_id,
            "name": name,
            "email": email,
            "role": "employee",
        }
        self.employees_by_id[employee["id"]] = employee
        self._next_employee_id += 1
        return "added"

    def update_employee(self, employee_id: int, *, name: str, email: str, device_user_id: str) -> None:
        self.employees_by_id[employee_id].update(name=name, email=email, device_user_id=device_user_id)
        self._ids(self.employees_by_id[employee_id])

    def _ids(self, employee):
        if employee is not None:
            employee["device_ids"] = [employee["device_user_id"]] + sorted(
                uid for uid, owner in self.extra_ids.items() if owner == employee["id"]
            )
        return employee

    def extra_device_ids(self, employee_id: int) -> list[str]:
        return sorted(uid for uid, owner in self.extra_ids.items() if owner == employee_id)

    def add_extra_device_id(self, employee_id: int, device_user_id: str) -> None:
        from essl_hub.db import IntegrityError

        if self.employee_by_device_user_id(device_user_id) is not None:
            raise IntegrityError("device user ID in use")
        self.extra_ids[device_user_id] = employee_id

    def remove_extra_device_id(self, employee_id: int, device_user_id: str) -> bool:
        if self.extra_ids.get(device_user_id) != employee_id:
            return False
        del self.extra_ids[device_user_id]
        return True

    def device_user_id_owner(self, device_user_id: str):
        return self.employee_by_device_user_id(device_user_id)

    def employees(self, include_archived: bool = False):
        return sorted(
            (self._ids(e) for e in self.employees_by_id.values()
             if include_archived or not e.get("archived_at")),
            key=lambda e: e["name"],
        )

    def archived_employees_with_punches(self, start, end):
        return []

    def employee_by_email(self, email: str, include_archived: bool = False):
        return self._ids(next(
            (
                e
                for e in self.employees_by_id.values()
                if e["email"].lower() == email.strip().lower()
                and (include_archived or not e.get("archived_at"))
            ),
            None,
        ))

    def employee_by_id(self, employee_id: int, include_archived: bool = False):
        employee = self.employees_by_id.get(employee_id)
        if employee is not None and employee.get("archived_at") and not include_archived:
            return None
        return self._ids(employee)

    def set_employee_role(self, employee_id: int, role: str) -> None:
        self._check_online()
        from essl_hub.db import RuleViolation

        current = self.employees_by_id[employee_id]
        admins = sum(e["role"] == "admin" for e in self.employees_by_id.values())
        if current["role"] == "admin" and role != "admin" and admins <= 1:
            raise RuleViolation("The last admin cannot be demoted.")
        current["role"] = role

    def set_employee_manager(self, employee_id: int, manager_id) -> None:
        self.employees_by_id[employee_id]["manager_id"] = manager_id

    def subtree_ids(self, root_id: int) -> list[int]:
        found = [root_id]
        for current in found:
            found += [
                e["id"] for e in self.employees_by_id.values()
                if e.get("manager_id") == current and e["id"] not in found
            ]
        return found

    def manager_chain(self, employee_id: int) -> list[int]:
        chain = [employee_id]
        while (m := self.employees_by_id[chain[-1]].get("manager_id")) and m not in chain:
            chain.append(m)
        return chain

    def employee_by_device_user_id(self, device_user_id: str):
        if device_user_id in self.extra_ids:
            return self._ids(self.employees_by_id.get(self.extra_ids[device_user_id]))
        return self._ids(next(
            (
                e
                for e in self.employees_by_id.values()
                if e["device_user_id"] == device_user_id
            ),
            None,
        ))

    def has_admin(self) -> bool:
        return any(e["role"] == "admin" for e in self.employees_by_id.values())

    def create_first_admin(self, *, name: str, email: str, device_user_id: str) -> None:
        existing = self.employee_by_email(email)
        if existing is not None:
            existing["role"] = "admin"
            return
        employee = {
            "id": self._next_employee_id,
            "device_user_id": device_user_id,
            "name": name,
            "email": email,
            "role": "admin",
        }
        self.employees_by_id[employee["id"]] = employee
        self._next_employee_id += 1

    # bootstrap admin ------------------------------------------------------

    def ensure_bootstrap_admin(self, email) -> None:
        self._check_online()
        if not email:
            return
        if any(e["role"] == "admin" for e in self.employees_by_id.values()):
            return
        for employee in self.employees_by_id.values():
            if employee["email"].lower() == email.strip().lower():
                employee["role"] = "admin"

    # login codes / sessions ------------------------------------------------

    def count_recent_login_codes(self, *, email: str, since: datetime) -> int:
        return sum(
            1
            for row in self.login_codes
            if row["email"].lower() == email.lower() and row["created_at"] >= since
        )

    def count_recent_login_codes_by_ip(self, *, ip: str, since: datetime) -> int:
        return sum(1 for row in self.login_codes if row["requested_ip"] == ip and row["created_at"] >= since)

    def create_login_code(self, *, email, code_hash, pending_token_hash, requested_ip, expires_at):
        self._check_online()
        self.login_codes.append(
            {
                "id": self._next_login_code_id,
                "email": email,
                "code_hash": code_hash,
                "pending_token_hash": pending_token_hash,
                "requested_ip": requested_ip,
                "expires_at": expires_at,
                "attempts": 0,
                "consumed_at": None,
                "created_at": _ist_now(),
            }
        )
        self._next_login_code_id += 1

    def active_login_code(self, *, pending_token_hash: str):
        candidates = [
            row
            for row in self.login_codes
            if row["pending_token_hash"] == pending_token_hash and row["consumed_at"] is None
        ]
        return candidates[-1] if candidates else None

    def increment_login_code_attempts(self, login_code_id: int) -> None:
        for row in self.login_codes:
            if row["id"] == login_code_id:
                row["attempts"] += 1

    def consume_login_code(self, login_code_id: int) -> None:
        for row in self.login_codes:
            if row["id"] == login_code_id:
                row["consumed_at"] = _ist_now()

    def create_session(self, *, employee_id, token_hash, csrf_token, expires_at):
        self.sessions[token_hash] = {
            "employee_id": employee_id,
            "csrf_token": csrf_token,
            "expires_at": expires_at,
            "revoked": False,
        }

    def session_employee(self, token_hash: str):
        session = self.sessions.get(token_hash)
        if session is None or session["revoked"] or session["expires_at"] < _ist_now():
            return None
        employee = self.employees_by_id.get(session["employee_id"])
        if employee is None:
            return None
        return {**self._ids(employee), "csrf_token": session["csrf_token"]}

    def revoke_session(self, token_hash: str) -> None:
        if token_hash in self.sessions:
            self.sessions[token_hash]["revoked"] = True

    # api keys --------------------------------------------------------------

    def create_api_key(self, *, prefix: str, key_hash: str, label: str) -> None:
        self.api_keys_list.append(
            {
                "prefix": prefix,
                "key_hash": key_hash,
                "label": label,
                "created_at": _ist_now(),
                "revoked_at": None,
                "last_used_at": None,
            }
        )

    def active_api_key_count(self) -> int:
        return sum(1 for k in self.api_keys_list if k["revoked_at"] is None)

    def api_key_by_hash(self, key_hash: str):
        return next(
            (k for k in self.api_keys_list if k["key_hash"] == key_hash and k["revoked_at"] is None),
            None,
        )

    def touch_api_key(self, key_hash: str) -> None:
        for k in self.api_keys_list:
            if k["key_hash"] == key_hash:
                k["last_used_at"] = _ist_now()

    def revoke_api_key(self, prefix: str) -> None:
        for k in self.api_keys_list:
            if k["prefix"] == prefix:
                k["revoked_at"] = _ist_now()

    def api_keys(self):
        return sorted(self.api_keys_list, key=lambda k: k["created_at"], reverse=True)

    # alerts, outbox and report links (v8) -----------------------------------

    def _with_role(self, role: str) -> list[dict]:
        return sorted(
            (self._ids(e) for e in self.employees_by_id.values()
             if e["role"] == role and not e.get("archived_at")),
            key=lambda e: e["name"],
        )

    def active_hr_users(self) -> list[dict]:
        return self._with_role("hr")

    def active_admins(self) -> list[dict]:
        return self._with_role("admin")

    def latest_baselines(self) -> dict[int, dict]:
        return dict(self.baselines)

    def latest_baseline_at(self):
        """Database.latest_baseline_at: the newest snapshot's computed_at
        (``baselines`` holds only the newest snapshot)."""
        stamps = [row["computed_at"] for row in self.baselines.values() if row.get("computed_at")]
        return max(stamps) if stamps else None

    def alert_counts(self, since) -> list[dict]:
        counts: dict[tuple, int] = {}
        for alert in self.alerts:
            if alert["work_day"] >= since:
                key = (alert.get("site"), alert["kind"], alert["mode"], alert["status"])
                counts[key] = counts.get(key, 0) + 1
        return [
            {"site": site, "kind": kind, "mode": mode, "status": status, "n": n}
            for (site, kind, mode, status), n in sorted(counts.items(), key=lambda i: str(i[0]))
        ]

    def enqueue_mail(self, recipients, subject, text_body, html_body=None) -> int:
        self._check_online()
        unique: dict[str, str] = {}
        for address in recipients:
            address = (address or "").strip()
            if address:
                unique.setdefault(address.lower(), address)
        if not unique:
            raise ValueError("an email needs at least one recipient")
        row = {
            "id": len(self.outbox) + 1, "recipients": list(unique.values()), "subject": subject,
            "text_body": text_body, "html_body": html_body, "status": "pending", "attempts": 0,
            "last_error": None, "next_attempt_at": _ist_now(), "created_at": _ist_now(),
            "sent_at": None,
        }
        self.outbox.append(row)
        return row["id"]

    def create_report_link(self, employee_id, work_day, *, expires_at) -> str:
        import secrets
        from essl_hub.hashing import sha256_hex

        self._check_online()
        token = secrets.token_urlsafe(32)
        self.report_links.append({
            "id": len(self.report_links) + 1, "token_hash": sha256_hex(token),
            "employee_id": employee_id, "work_day": work_day, "expires_at": expires_at,
            "created_at": _ist_now(),
        })
        return token

    def report_link(self, token):
        from essl_hub.hashing import sha256_hex

        if not token:
            return None
        digest = sha256_hex(token)
        return next(
            (dict(l) for l in self.report_links
             if l["token_hash"] == digest and l["expires_at"] > _ist_now()),
            None,
        )

    # fix requests and corrections (v8) ----------------------------------------

    def _fix_event(self, request_id, line_id, actor_id, kind, old_value, new_value) -> None:
        self.fix_events.append({
            "id": len(self.fix_events) + 1, "request_id": request_id, "line_id": line_id,
            "actor_employee_id": actor_id, "kind": kind, "old_value": old_value,
            "new_value": new_value, "created_at": _ist_now(),
        })

    def _name(self, employee_id):
        return (self.employees_by_id.get(employee_id) or {}).get("name")

    def _request_lines(self, request_id) -> list[dict]:
        return [dict(l, decided_by_name=self._name(l["decided_by"]))
                for l in sorted(self.fix_lines.values(), key=lambda l: l["id"])
                if l["request_id"] == request_id]

    def pending_fix_request(self, employee_id, work_day):
        return next(
            (dict(r) for r in self.fix_requests.values()
             if (r["employee_id"], r["work_day"], r["status"]) == (employee_id, work_day, "pending")),
            None,
        )

    def latest_fix_request(self, employee_id, work_day):
        rows = [r for r in self.fix_requests.values()
                if (r["employee_id"], r["work_day"]) == (employee_id, work_day)]
        return dict(max(rows, key=lambda r: r["id"])) if rows else None

    def file_fix_lines(self, employee_id, work_day, lines, *, source, punch_set_hash=None):
        from essl_hub import db as db_module
        from essl_hub import db_fixes

        self._check_online()
        if source not in db_module.FIX_SOURCES:
            raise ValueError(f"unknown fix request source: {source}")
        prepared = [db_fixes._fix_line_values(line) for line in lines]
        if not prepared:
            raise ValueError("a fix request needs at least one line")
        latest = self.latest_fix_request(employee_id, work_day)
        if latest is not None and latest["status"] in db_module.FIX_CLOSED_STATUSES | {"pending"}:
            request_id = latest["id"]
            held = self._request_lines(request_id)
            existing = len(held)
            keys = {db_fixes._fix_line_key(line) for line in held}
            prepared = [values for values in prepared if db_fixes._fix_line_key(values) not in keys]
            if not prepared:
                return {"request_id": request_id, "first_lines": False}
        else:
            request_id, existing = len(self.fix_requests) + 1, 0
        if existing + len(prepared) > db_module.FIX_MAX_LINES:
            raise db_module.FixLineLimit(f"A request holds at most {db_module.FIX_MAX_LINES} lines.")
        now = _ist_now()
        if latest is not None and latest["id"] == request_id:
            event = "lines_added"
            if latest["status"] != "pending":
                self.fix_requests[request_id].update(
                    status="pending", source=source, punch_set_hash=punch_set_hash,
                    decided_at=None, decided_by=None, created_at=now)
                self._fix_event(request_id, None, employee_id, "reopened",
                                {"status": latest["status"]}, {"status": "pending"})
        else:
            event = "filed"
            self.fix_requests[request_id] = {
                "id": request_id, "employee_id": employee_id, "work_day": work_day,
                "status": "pending", "punch_set_hash": punch_set_hash, "source": source,
                "previous_request_id": latest["id"] if latest else None, "decision_note": None,
                "created_at": now, "decided_at": None, "decided_by": None,
            }
        line_ids = []
        for kind, target, event_time, direction, note in prepared:
            line_id = len(self.fix_lines) + 1
            self.fix_lines[line_id] = {
                "id": line_id, "request_id": request_id, "kind": kind,
                "target_fingerprint": target, "event_time": event_time, "direction": direction,
                "note": note, "status": "pending", "hr_event_time": None, "hr_direction": None,
                "decided_by": None, "decided_at": None, "created_at": now,
            }
            line_ids.append(line_id)
        self._fix_event(request_id, None, employee_id, event, None, {"lines": line_ids})
        return {"request_id": request_id, "first_lines": existing == 0}

    def close_fix_day(self, employee_id, work_day, status, *, source) -> int:
        from essl_hub import db as db_module

        self._check_online()
        if status not in db_module.FIX_CLOSED_STATUSES:
            raise ValueError(f"not a closing status: {status}")
        latest = self.latest_fix_request(employee_id, work_day)
        if latest is not None and latest["status"] == "pending":
            raise db_module.RuleViolation("A fix request for this day is waiting for HR.")
        now = _ist_now()
        if latest is not None and latest["status"] in db_module.FIX_CLOSED_STATUSES:
            request_id = latest["id"]
            self.fix_requests[request_id].update(status=status, source=source, decided_at=now)
        else:
            request_id = len(self.fix_requests) + 1
            self.fix_requests[request_id] = {
                "id": request_id, "employee_id": employee_id, "work_day": work_day,
                "status": status, "punch_set_hash": None, "source": source,
                "previous_request_id": latest["id"] if latest else None, "decision_note": None,
                "created_at": now, "decided_at": now, "decided_by": None,
            }
        self._fix_event(request_id, None, employee_id, "closed", None, {"status": status})
        return request_id

    def fix_request(self, request_id):
        row = self.fix_requests.get(request_id)
        if row is None:
            return None
        employee = self.employees_by_id.get(row["employee_id"]) or {}
        return {
            **row, "employee_name": employee.get("name"), "employee_email": employee.get("email"),
            "employee_manager_id": employee.get("manager_id"),
            "decided_by_name": self._name(row["decided_by"]),
            "lines": self._request_lines(request_id),
        }

    def fix_request_events(self, request_id) -> list[dict]:
        return [dict(e, actor_name=self._name(e["actor_employee_id"]))
                for e in self.fix_events if e["request_id"] == request_id]

    def fix_requests_for_review(self, recent: int = 20) -> list[dict]:
        def view(row):
            return {**row, "employee_name": self._name(row["employee_id"]),
                    "decided_by_name": self._name(row["decided_by"]),
                    "line_count": len(self._request_lines(row["id"]))}

        rows = sorted(self.fix_requests.values(), key=lambda r: r["id"])
        pending = [view(r) for r in rows if r["status"] == "pending"]
        decided = [view(r) for r in reversed(rows) if r["status"] in ("approved", "partial", "rejected")]
        return pending + decided[:recent]

    def pending_fix_request_count(self) -> int:
        return sum(r["status"] == "pending" for r in self.fix_requests.values())

    def decide_fix_lines(self, request_id, decisions, *, actor_id, note=None):
        from essl_hub import db as db_module

        self._check_online()
        decisions = list(decisions)
        note = (note or "").strip() or None
        for decision in decisions:
            if decision.get("status") not in ("approved", "rejected"):
                raise ValueError(f"bad line decision: {decision.get('status')}")
            if decision.get("direction") not in (None, *db_module.DIRECTIONS):
                raise ValueError(f"bad direction: {decision.get('direction')}")
        if note is None and any(d["status"] == "rejected" for d in decisions):
            raise ValueError("A note is required when rejecting.")
        if note is not None and len(note) > db_module.FIX_NOTE_MAX_CHARS:
            raise ValueError(f"note is longer than {db_module.FIX_NOTE_MAX_CHARS} characters")
        request = self.fix_requests.get(request_id)
        if request is None:
            raise db_module.RuleViolation("No such fix request.")
        if actor_id == request["employee_id"]:
            raise db_module.RuleViolation("Nobody decides their own fix request.")
        now = _ist_now()
        decided, skipped, corrections, events = [], [], [], []
        updates = {}
        for decision in decisions:
            line = self.fix_lines.get(decision["line_id"])
            if line is None or line["request_id"] != request_id:
                raise ValueError(f"line {decision['line_id']} is not on request {request_id}")
            if line["status"] != "pending":
                skipped.append({"line_id": line["id"], "status": line["status"],
                                "decided_by": line["decided_by"],
                                "decided_by_name": self._name(line["decided_by"])})
                continue
            adds = line["kind"] in ("add_in", "add_out")
            hr_time = decision.get("event_time") if adds else None
            hr_direction = decision.get("direction") if line["kind"] != "ignore" else None
            correction = None
            if decision["status"] == "approved":
                if adds:
                    correction = {"kind": "add", "fingerprint": None,
                                  "event_timestamp": hr_time or line["event_time"],
                                  "direction": hr_direction or line["direction"]}
                elif line["kind"] == "ignore":
                    correction = {"kind": "ignore", "fingerprint": line["target_fingerprint"],
                                  "event_timestamp": None, "direction": None}
                else:
                    direction = hr_direction or line["direction"]
                    if direction is None:
                        stored = next((p.get("trusted_direction") for p in self.punches
                                       if p["fingerprint"] == line["target_fingerprint"]), None)
                        direction = {"entry": "exit", "exit": "entry"}.get(stored)
                    if direction is None:
                        raise ValueError(f"line {line['id']} needs a direction to flip to")
                    correction = {"kind": "set_direction", "fingerprint": line["target_fingerprint"],
                                  "event_timestamp": None, "direction": direction}
                actor = self.employees_by_id[actor_id]
                corrections.append({
                    **correction, "employee_id": request["employee_id"],
                    "work_day": request["work_day"], "request_line_id": line["id"], "created_at": now,
                    "created_by": actor_id, "created_by_name": actor["name"],
                    "created_by_role": actor["role"], "path": "request", "reason": note,
                })
            updates[line["id"]] = {"status": decision["status"], "hr_event_time": hr_time,
                                   "hr_direction": hr_direction, "decided_by": actor_id,
                                   "decided_at": now}
            events.append((line["id"], "line_approved" if decision["status"] == "approved"
                           else "line_rejected",
                           {"status": "pending", "kind": line["kind"],
                            "target_fingerprint": line["target_fingerprint"],
                            "event_time": line["event_time"], "direction": line["direction"]},
                           {"status": decision["status"], "correction": correction}))
            decided.append(line["id"])
        # All checks passed: apply together, like the real transaction.
        for line_id, values in updates.items():
            self.fix_lines[line_id].update(values)
        for correction in corrections:
            self.corrections.append({"id": len(self.corrections) + 1, **correction})
        for line_id, kind, old_value, new_value in events:
            self._fix_event(request_id, line_id, actor_id, kind, old_value, {**new_value, "note": note})
        if note is not None and decided:
            request["decision_note"] = note
        statuses = {l["status"] for l in self._request_lines(request_id)}
        if decided and statuses and "pending" not in statuses and request["status"] == "pending":
            final = ("approved" if statuses == {"approved"}
                     else "rejected" if statuses == {"rejected"} else "partial")
            request.update(status=final, decided_at=now, decided_by=actor_id)
            self._fix_event(request_id, None, actor_id, "decided", {"status": "pending"},
                            {"status": final, "note": note})
        return {"status": request["status"], "decided": decided, "skipped": skipped}

    def record_direct_corrections(self, employee_id, work_day, edits, *, actor_id, reason):
        from essl_hub import db as db_module

        self._check_online()
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("A reason is required.")
        if len(reason) > db_module.FIX_NOTE_MAX_CHARS:
            raise ValueError(f"reason is longer than {db_module.FIX_NOTE_MAX_CHARS} characters")
        edits = list(edits)
        if not edits:
            raise ValueError("There is nothing to change.")
        actor = self.employees_by_id.get(actor_id)
        if actor is None or actor.get("archived_at") or actor["role"] != "admin":
            raise db_module.RuleViolation("Only an admin can fix punches directly.")
        if actor_id == employee_id:
            raise db_module.RuleViolation("Nobody fixes their own punches directly; file a fix request instead.")
        target = self.employees_by_id.get(employee_id)
        if target is None or target.get("archived_at"):
            raise db_module.RuleViolation("No such active employee.")
        now = _ist_now()
        for edit in edits:
            self.corrections.append({
                "id": len(self.corrections) + 1, "employee_id": employee_id, "work_day": work_day,
                "kind": edit["kind"], "fingerprint": edit.get("fingerprint"),
                "event_timestamp": edit.get("event_timestamp"), "direction": edit.get("direction"),
                "request_line_id": None, "created_at": now, "created_by": actor_id,
                "created_by_name": actor["name"], "created_by_role": actor["role"],
                "path": "direct", "reason": reason,
            })
        return len(edits)

    def corrections_for(self, employee_ids, start: datetime, end: datetime) -> list[dict]:
        wanted = set(employee_ids)
        first_day, last_day = start.date() - timedelta(days=1), end.date()
        return [
            dict(c) for c in self.corrections
            if c["employee_id"] in wanted and (
                (c["kind"] == "add" and start <= c["event_timestamp"] < end)
                or (c["kind"] != "add" and first_day <= c["work_day"] <= last_day))
        ]

    def fix_request_days(self, employee_ids, start, end) -> dict:
        wanted = set(employee_ids)
        days: dict = {}
        for row in sorted(self.fix_requests.values(), key=lambda r: r["id"]):
            if row["employee_id"] in wanted and start <= row["work_day"] <= end:
                days.setdefault((row["employee_id"], row["work_day"]), []).append(row["status"])
        return days

    # settings ---------------------------------------------------------------

    def setting(self, key: str, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value


class FakeFixRequestsTest(unittest.TestCase):
    """The FakeDatabase fix-request mirror behaves like essl_hub.db (U1)."""

    def setUp(self) -> None:
        self.db = FakeDatabase()
        for uid, name, role in (("101", "Asha", "employee"), ("102", "Hari", "hr")):
            self.db.upsert_employee(device_user_id=uid, name=name, email=f"{name.lower()}@x.com")
            self.db.set_employee_role(self.db.employee_by_device_user_id(uid)["id"], role)
        self.day = datetime(2026, 10, 26).date()

    def test_file_decide_and_read_back(self) -> None:
        from essl_hub.db import FixLineLimit

        line = {"kind": "add_in", "event_time": datetime(2026, 10, 26, 13, 40)}
        first = self.db.file_fix_lines(1, self.day, [line], source="link")
        self.assertEqual(first, {"request_id": 1, "first_lines": True})
        # A double-submitted line is skipped, as in essl_hub.db.
        self.assertFalse(self.db.file_fix_lines(1, self.day, [line], source="link")["first_lines"])
        self.assertEqual(len(self.db.fix_request(1)["lines"]), 1)
        later = {"kind": "add_in", "event_time": datetime(2026, 10, 26, 13, 45)}
        self.assertFalse(self.db.file_fix_lines(1, self.day, [later], source="session")["first_lines"])
        with self.assertRaises(FixLineLimit):
            self.db.file_fix_lines(1, self.day, [
                {"kind": "add_out", "event_time": datetime(2026, 10, 26, 18, minute)} for minute in range(9)
            ], source="link")
        lines = self.db.fix_request(1)["lines"]
        result = self.db.decide_fix_lines(1, [
            {"line_id": lines[0]["id"], "status": "approved"},
            {"line_id": lines[1]["id"], "status": "rejected"},
        ], actor_id=2, note="Duplicate")
        self.assertEqual(result["status"], "partial")
        again = self.db.decide_fix_lines(1, [{"line_id": lines[0]["id"], "status": "approved"}],
                                         actor_id=2)
        self.assertEqual(again["skipped"][0]["decided_by_name"], "Hari")
        corrections = self.db.corrections_for([1], datetime(2026, 10, 26, 6), datetime(2026, 10, 27, 6))
        self.assertEqual([(c["kind"], c["direction"]) for c in corrections], [("add", "entry")])
        self.assertEqual(self.db.fix_request_days([1], self.day, self.day), {(1, self.day): ["partial"]})
        token = self.db.create_report_link(1, self.day, expires_at=_ist_now() + timedelta(days=1))
        self.assertEqual(self.db.report_link(token)["employee_id"], 1)
        self.assertEqual([p["name"] for p in self.db.active_hr_users()], ["Hari"])


class FakeArchiveTest(unittest.TestCase):
    """The fake's archive rules hide punches like Database._ACTIVE_PUNCH_SQL."""

    def test_archive_hides_and_restore_shows_again(self) -> None:
        db = FakeDatabase()
        at = datetime(2026, 10, 1, 9, 0)
        db.insert_punches([{"fingerprint": f"f{n}", "device_id": "d", "device_user_id": "9999",
                            "event_timestamp": at + timedelta(days=n)} for n in range(3)])
        archive_id = db.archive_punches("9999", from_ts=at + timedelta(days=1), to_ts=None)
        self.assertEqual(db.device_id_summary()[0]["punch_count"], 1)
        self.assertEqual(db.unmapped_device_user_ids(), [{"device_user_id": "9999", "punch_count": 1}])
        self.assertEqual(db.active_archive_for("9999"), {"archives": 1, "hidden": 2})
        self.assertEqual(db.punch_archives()[0]["hidden_count"], 2)
        self.assertTrue(db.restore_punch_archive(archive_id))
        self.assertFalse(db.restore_punch_archive(archive_id))
        self.assertEqual(db.first_punch_at(["9999"]), at)
        self.assertEqual(db.device_id_summary()[0]["punch_count"], 3)


def _make_app():
    database = FakeDatabase()
    app = create_app(database=database)
    app.testing = True
    return app, database


def _assert_auth_layout(test: unittest.TestCase, html: str, heading: str) -> None:
    """U3: every auth page shares the NITCO split layout."""
    test.assertIn('class="auth-panel"', html)
    test.assertIn('alt="NITCO"', html)
    test.assertIn("brand/wordmark-white.png", html)
    test.assertIn("Attendance Hub</p>", html)
    test.assertNotIn("auth-brand", html)
    test.assertNotIn("brand-mark", html)
    test.assertNotIn("auth-bg", html)
    test.assertNotIn('"/brand/', html)
    test.assertEqual(html.count("<main"), 1)
    test.assertIn(f"<h1>{heading}</h1>", html)
    # The brand panel sits before main; the heading precedes any form.
    test.assertLess(html.index('class="auth-panel"'), html.index("<main"))
    if "<form" in html:
        test.assertLess(html.index("<h1>"), html.index("<form"))


class TurnstileSigninTest(unittest.TestCase):
    ENV = {"TURNSTILE_SITE_KEY": "site-key-1", "TURNSTILE_SECRET_KEY": "secret-1"}

    def setUp(self) -> None:
        with patch.dict(os.environ, self.ENV):
            self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com")
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.sent = []
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda email, code: self.sent.append(email)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def test_widget_and_script_render_with_site_key(self) -> None:
        page = self.client.get("/signin").text
        self.assertIn('data-sitekey="site-key-1"', page)
        self.assertIn("challenges.cloudflare.com/turnstile/v0/api.js", page)
        self.assertNotIn("secret-1", page)

    def test_widget_renders_inside_the_form_column(self) -> None:
        page = self.client.get("/signin").text
        main = page[page.index("<main"):page.index("</main>")]
        form = main[main.index("<form"):main.index("</form>")]
        self.assertIn('class="cf-turnstile auth-turnstile"', form)
        self.assertNotIn("cf-turnstile", page[:page.index("<main")])

    def test_signin_renders_brand_panel_and_email_form(self) -> None:
        page = self.client.get("/signin").text
        _assert_auth_layout(self, page, "Sign in")
        self.assertIn('name="email"', page)

    def test_code_page_uses_the_same_layout(self) -> None:
        with patch("essl_hub.turnstile.verify", return_value=True):
            page = self.client.post(
                "/signin", data={"email": "ravi.kumar@example.com", "cf-turnstile-response": "tok"}
            ).text
        _assert_auth_layout(self, page, "Check your email")
        self.assertIn('name="code"', page)

    def test_failed_check_sends_nothing(self) -> None:
        with patch("essl_hub.turnstile.verify", return_value=False) as verify:
            response = self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirm you are human", response.text)
        self.assertEqual(self.database.login_codes, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(verify.call_args.args[1], "")  # no widget token posted

    def test_passed_check_sends_code(self) -> None:
        with patch("essl_hub.turnstile.verify", return_value=True) as verify:
            response = self.client.post(
                "/signin",
                data={"email": "ravi.kumar@example.com", "cf-turnstile-response": "tok"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sent, ["ravi.kumar@example.com"])
        self.assertEqual(verify.call_args.args[1], "tok")


class TurnstileOffTest(unittest.TestCase):
    def test_no_widget_without_keys(self) -> None:
        with patch.dict(os.environ, {"TURNSTILE_SITE_KEY": "", "TURNSTILE_SECRET_KEY": ""}):
            app, _ = _make_app()
        self.assertNotIn("cf-turnstile", app.test_client().get("/signin").text)


class WebFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com")
        self.client = self.app.test_client()

    def _capture_mailer(self):
        from essl_hub import web as web_module

        captured = {}

        def fake_mailer(email, code):
            captured["email"] = email
            captured["code"] = code

        from essl_hub import mail as mail_module

        original = mail_module.send_login_code
        mail_module.send_login_code = fake_mailer
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        return captured

    def _signin_from(self, behind_tunnel: str | None) -> str | None:
        self._capture_mailer()
        env = {} if behind_tunnel is None else {"ESSL_BEHIND_TUNNEL": behind_tunnel}
        with patch.dict(os.environ, env):
            if behind_tunnel is None:
                os.environ.pop("ESSL_BEHIND_TUNNEL", None)
            self.client.post(
                "/signin",
                data={"email": "ravi.kumar@example.com"},
                headers={"CF-Connecting-IP": "203.0.113.9"},
                environ_base={"REMOTE_ADDR": "172.18.0.5"},
            )
        return self.database.login_codes[-1]["requested_ip"]

    def test_signin_rate_limits_on_cf_connecting_ip_behind_tunnel(self) -> None:
        self.assertEqual(self._signin_from("1"), "203.0.113.9")

    def test_signin_ignores_cf_connecting_ip_without_tunnel_flag(self) -> None:
        self.assertEqual(self._signin_from(None), "172.18.0.5")

    def test_health_check(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True})

    def test_root_redirects_to_signin_when_signed_out(self) -> None:
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/signin", response.headers["Location"])

    def test_full_sign_in_flow_lands_on_today(self) -> None:
        captured = self._capture_mailer()
        response = self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("code", response.text.lower())
        self.assertEqual(captured["email"], "ravi.kumar@example.com")

        verify = self.client.post(
            "/signin/verify", data={"code": captured["code"]}, follow_redirects=False
        )
        self.assertEqual(verify.status_code, 302)
        self.assertTrue(verify.headers["Location"].endswith("/day"))

        week = self.client.get("/week")
        self.assertEqual(week.status_code, 200)
        self.assertIn("My week", week.text)

    def test_unknown_email_gets_identical_response_and_sends_nothing(self) -> None:
        # Covers AE4.
        captured = self._capture_mailer()
        known = self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        captured.clear()
        unknown = self.client.post("/signin", data={"email": "ghost@example.com"})
        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual({}, captured)

    def test_mail_send_failure_gives_the_same_response_as_success(self) -> None:
        # Covers R11: a Resend failure must not change the response shape,
        # since that would itself reveal the email is on the roster.
        from essl_hub import mail as mail_module
        from essl_hub.mail import MailError

        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: (_ for _ in ()).throw(MailError("down"))
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

        failing = self.client.post("/signin", data={"email": "ravi.kumar@example.com"})

        mail_module.send_login_code = lambda e, c: None
        succeeding = self.client.post("/signin", data={"email": "ravi.kumar@example.com"})

        self.assertEqual(failing.status_code, succeeding.status_code)
        self.assertEqual(failing.text, succeeding.text)

    def test_employee_cannot_view_hr_route(self) -> None:
        self._capture_mailer()
        self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        code = self.database.login_codes[-1]
        # sign in properly via the captured code path
        captured = self._capture_mailer()
        self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        # /hr now redirects to /team, which sends an employee to their own day.
        response = self.client.get("/hr", follow_redirects=True)
        self.assertEqual(response.request.path, "/day")

    def test_hr_navigation_offers_employee_data_but_not_the_admin_console(self) -> None:
        self.database.upsert_employee(device_user_id="900", name="Priya HR", email="priya@example.com")
        priya = self.database.employee_by_email("priya@example.com")
        self.database.set_employee_role(priya["id"], "hr")
        captured = self._capture_mailer()
        self.client.post("/signin", data={"email": "priya@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        page = self.client.get("/team")
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/team"', page.text)
        self.assertIn("<span>Employees</span>", page.text)
        self.assertNotIn('href="/admin"', page.text)

    def test_hr_cannot_view_admin_route(self) -> None:
        self.database.upsert_employee(device_user_id="900", name="Priya HR", email="priya@example.com")
        priya = self.database.employee_by_email("priya@example.com")
        self.database.set_employee_role(priya["id"], "hr")
        captured = self._capture_mailer()
        self.client.post("/signin", data={"email": "priya@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        self.assertEqual(self.client.get("/settings").status_code, 403)
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/devices"))
        # the old admin-only role route is gone; roles moved to /people
        role_response = self.client.post("/admin/employees/1/role", data={"role": "employee"})
        self.assertEqual(role_response.status_code, 404)


def _punch_row(fingerprint: str, at: datetime, direction: str, uid: str = "118") -> dict:
    return {
        "fingerprint": fingerprint,
        "device_id": DEVICES[1].id,
        "site": "Gachibowli",
        "device_user_id": uid,
        "event_timestamp": at,
        "trusted_direction": direction,
    }


def _fixed_now(at: datetime):
    """Pin the routes' clock (not auth's) so work-day rules are deterministic."""
    return patch.multiple("essl_hub.routes.common", _now_naive=lambda: at), patch(
        "essl_hub.routes.me._now_naive", lambda: at
    )


class HoursRenderingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com")
        self.client = self.app.test_client()

    def _sign_in(self) -> dict:
        from essl_hub import mail as mail_module

        captured = {}

        def fake_mailer(email, code):
            captured["code"] = code

        original = mail_module.send_login_code
        mail_module.send_login_code = fake_mailer
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "ravi.kumar@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        return captured

    def test_week_view_renders_with_a_normal_day_of_punches(self) -> None:
        self._sign_in()
        today = _ist_today()
        # a closed day earlier this week
        monday = today - timedelta(days=today.weekday())
        self.database.insert_punches(
            [
                {
                    "fingerprint": "fp-1",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "118",
                    "event_timestamp": datetime.combine(monday, datetime.min.time()) + timedelta(hours=9),
                    "trusted_direction": "entry",
                },
                {
                    "fingerprint": "fp-2",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "118",
                    "event_timestamp": datetime.combine(monday, datetime.min.time()) + timedelta(hours=18),
                    "trusted_direction": "exit",
                },
            ]
        )
        response = self.client.get("/week")
        self.assertEqual(response.status_code, 200)
        self.assertIn("9h 00m", response.text)

    def test_short_day_renders_the_exact_short_by_label(self) -> None:
        # Pins the shared status-chip macro's parameterised short-label path.
        self._sign_in()
        today = _ist_today()
        monday = today - timedelta(days=today.weekday())
        start = datetime.combine(monday, datetime.min.time())
        self.database.insert_punches(
            [
                {
                    "fingerprint": "fp-short-in",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "118",
                    "event_timestamp": start + timedelta(hours=9),
                    "trusted_direction": "entry",
                },
                {
                    "fingerprint": "fp-short-out",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "118",
                    "event_timestamp": start + timedelta(hours=15),
                    "trusted_direction": "exit",
                },
            ]
        )
        response = self.client.get("/week")
        self.assertIn("Short by 2h 00m", response.text)

    def test_incomplete_day_shows_exact_label_and_zero_effective(self) -> None:
        self._sign_in()
        today = _ist_today()
        # A past weekday: today would still be "open", and on Monday the
        # day before is a weekend.
        yesterday = today - timedelta(days=1)
        while yesterday.weekday() >= 5:
            yesterday -= timedelta(days=1)
        start_of_day = datetime.combine(yesterday, datetime.min.time())
        self.database.insert_punches(
            [
                {
                    "fingerprint": "fp-3",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "118",
                    "event_timestamp": start_of_day + timedelta(hours=9),
                    "trusted_direction": "entry",
                }
            ]
        )
        response = self.client.get(f"/day?date={yesterday.isoformat()}")
        self.assertIn("Missing exit punch", response.text)
        self.assertIn("No exit recorded", response.text)

    def test_past_day_has_no_break_row_before_missing_exit(self) -> None:
        # U10/KTD9: the trailing entry's gap is not a break.
        self._sign_in()
        day = _ist_today() - timedelta(days=1)
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        base = datetime.combine(day, datetime.min.time())
        self.database.insert_punches([
            {"fingerprint": f"fp-u10-{h}", "device_id": DEVICES[1].id, "site": "Gachibowli",
             "device_user_id": "118", "event_timestamp": base + timedelta(hours=h), "trusted_direction": d}
            for h, d in ((9, "entry"), (13, "exit"), (14, "entry"))
        ])
        response = self.client.get(f"/day?date={day.isoformat()}")
        self.assertIn("No exit recorded", response.text)
        self.assertNotIn('class="bar-seg is-break', response.text)
        self.assertIn("Breaks</dt><dd><span class=\"num\">0:00:00</span> · none", response.text)

    def _at(self, at: datetime):
        common, me = _fixed_now(at)
        common.start()
        me.start()
        self.addCleanup(common.stop)
        self.addCleanup(me.stop)

    def test_at_three_am_today_is_still_the_previous_work_day_and_open(self) -> None:
        self._sign_in()
        day = datetime(2026, 9, 9)  # a Wednesday
        self.database.insert_punches([_punch_row("fp-n1", day + timedelta(hours=22, minutes=10), "entry")])
        self._at(day + timedelta(days=1, hours=3))
        response = self.client.get("/day")
        self.assertIn("<h1>Today</h1>", response.text)
        self.assertIn("Wednesday 09 September 2026", response.text)
        self.assertIn("work day from 06:00", response.text)
        self.assertIn('class="bar-seg is-inside is-open"', response.text)
        self.assertIn('<span class="bar-mark-label">now</span>', response.text)  # bar ends at now

    def test_day_view_reads_one_clock_instant_per_request(self) -> None:
        # U10: a clock that ticks on every read must still yield one stretch
        # value measured from the first (and only) read.
        self._sign_in()
        day = datetime(2026, 9, 9)
        self.database.insert_punches([_punch_row("fp-s1", day + timedelta(hours=9), "entry")])
        first = day + timedelta(hours=11, seconds=5)
        ticks = iter(first + timedelta(seconds=n) for n in range(100))
        clock = lambda: next(ticks)  # noqa: E731
        with patch("essl_hub.routes.common._now_naive", clock), patch("essl_hub.routes.me._now_naive", clock):
            response = self.client.get("/day")
        self.assertIn('data-timer-seconds="7205"', response.text)

    def test_night_shift_day_view_marks_post_midnight_exit(self) -> None:
        self._sign_in()
        day = datetime(2026, 9, 9)
        self.database.insert_punches([
            _punch_row("fp-n1", day + timedelta(hours=22, minutes=10), "entry"),
            _punch_row("fp-n2", day + timedelta(days=1, hours=3, minutes=40), "exit"),
        ])
        self._at(datetime(2026, 9, 12, 12))
        response = self.client.get("/day?date=2026-09-09")
        self.assertIn("5h 30m", response.text)
        self.assertEqual(response.text.count("+1</sup>"), 1)
        next_day = self.client.get("/day?date=2026-09-10")
        self.assertNotIn("5h 30m", next_day.text)

    def test_flagged_day_counts_pairs_and_shows_needs_review(self) -> None:
        self._sign_in()
        day = datetime(2026, 9, 9)
        self.database.insert_punches([
            _punch_row("fp-r1", day + timedelta(hours=9), "entry"),
            _punch_row("fp-r2", day + timedelta(hours=14, minutes=33), "entry"),
            _punch_row("fp-r3", day + timedelta(hours=18), "exit"),
        ])
        self._at(datetime(2026, 9, 12, 12))
        response = self.client.get("/day?date=2026-09-09")
        self.assertIn("Needs review.", response.text)
        self.assertIn("No exit after 09:00", response.text)
        self.assertIn("3h 27m", response.text)
        self.assertNotIn("Missing exit punch", response.text)

    def test_week_at_monday_three_am_still_shows_previous_week(self) -> None:
        self._sign_in()
        self._at(datetime(2026, 9, 14, 3))  # Monday
        response = self.client.get("/week")
        self.assertIn("Mon 07 Sep to Fri 11 Sep 2026", response.text)
        self.assertIn("work days from 06:00", response.text)

    def test_day_start_zero_restores_midnight_days(self) -> None:
        self._sign_in()
        self.database.set_setting("day_start_hour", "0")
        day = datetime(2026, 9, 9)
        self.database.insert_punches([
            _punch_row("fp-n1", day + timedelta(hours=22, minutes=10), "entry"),
            _punch_row("fp-n2", day + timedelta(days=1, hours=3, minutes=40), "exit"),
        ])
        self._at(datetime(2026, 9, 10, 3))
        response = self.client.get("/day")
        self.assertIn("Thursday 10 September 2026", response.text)
        self.assertIn("work day from 00:00", response.text)
        past = self.client.get("/day?date=2026-09-09")
        self.assertIn("Missing exit punch", past.text)

    def test_employee_navigation_has_neither_hr_nor_admin_links(self) -> None:
        self._sign_in()
        page = self.client.get("/week")
        self.assertNotIn('href="/team"', page.text)
        self.assertNotIn('href="/admin"', page.text)

    def test_hr_export_csv_contains_headers(self) -> None:
        self.database.upsert_employee(device_user_id="900", name="Priya HR", email="priya@example.com")
        priya = self.database.employee_by_email("priya@example.com")
        self.database.set_employee_role(priya["id"], "hr")
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "priya@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        response = self.client.get("/hr/export.csv?from=2026-10-01&to=2026-10-02")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/export.csv?from=2026-10-01&to=2026-10-02"))
        response = self.client.get(response.headers["Location"])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.text.startswith("\ufeffEmp ID,Name,"))
        self.assertIn("Effective (h)", response.text)


class LiveTodayTest(unittest.TestCase):
    """U11: the live #day-live region, its states, polling and partials."""

    DAY = datetime(2026, 9, 9)  # a Wednesday

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com")
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _sign_in(self, email: str = "ravi.kumar@example.com") -> None:
        self.client.post("/signin", data={"email": email})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _at(self, at: datetime) -> None:
        common, me = _fixed_now(at)
        common.start()
        me.start()
        self.addCleanup(common.stop)
        self.addCleanup(me.stop)

    def _punches(self, *rows) -> None:
        self.database.insert_punches(
            [_punch_row(f"lv-{i}", self.DAY + offset, direction) for i, (offset, direction) in enumerate(rows)]
        )

    def _today(self, at: timedelta, *rows, headers=None) -> str:
        self._sign_in()
        self._punches(*rows)
        self._at(self.DAY + at)
        return self.client.get("/day", headers=headers or {}).text

    @staticmethod
    def _attr(page: str, name: str) -> str:
        return re.search(rf'{name}="([^"]*)"', page).group(1)

    def test_live_render_carries_one_instant_for_ring_stretch_and_epoch(self) -> None:
        page = self._today(timedelta(hours=11, seconds=5), (timedelta(hours=9), "entry"))
        self.assertEqual(self._attr(page, "data-base-seconds"), "7205")
        self.assertEqual(self._attr(page, "data-target-seconds"), "28800")
        self.assertEqual(self._attr(page, "data-live"), "true")
        self.assertEqual(self._attr(page, "data-state"), "inside")
        expected = int((self.DAY + timedelta(hours=11, seconds=5)).replace(tzinfo=_IST).timestamp() * 1000)
        self.assertEqual(self._attr(page, "data-server-epoch-ms"), str(expected))
        # ring digits, this stretch, the break-view Inside figure and gross (inside
        # since first in) all agree to the second
        self.assertEqual(page.count('data-tick="7205"'), 4)
        self.assertIn(">2:00:05<", page)

    def test_inside_under_target(self) -> None:
        page = self._today(timedelta(hours=11, seconds=5), (timedelta(hours=9), "entry"))
        self.assertIn('<span class="live-dot" aria-hidden="true"></span>Inside</h2>', page)
        self.assertIn('data-left>6h 00m left<', page)

    def test_inside_met(self) -> None:
        page = self._today(timedelta(hours=17, minutes=12), (timedelta(hours=9), "entry"))
        self.assertIn("Inside · Target met</h2>", page)
        self.assertIn("data-left>+12m over<", page)
        self.assertEqual(self._attr(page, "data-state"), "inside")

    def test_out_under_target_is_frozen(self) -> None:
        page = self._today(
            timedelta(hours=18),
            (timedelta(hours=10), "entry"),
            (timedelta(hours=17, minutes=43), "exit"),
        )
        self.assertIn("Out since 17:43:00</h2>", page)
        self.assertIn("data-left>17m left<", page)
        self.assertEqual(self._attr(page, "data-live"), "false")
        self.assertEqual(self._attr(page, "data-state"), "out")
        self.assertIn("data-poll=", page)

    def test_out_met(self) -> None:
        page = self._today(
            timedelta(hours=18),
            (timedelta(hours=9), "entry"),
            (timedelta(hours=17, minutes=12), "exit"),
        )
        self.assertIn("Out · Target met</h2>", page)
        self.assertIn("data-left>+12m over<", page)
        self.assertEqual(self._attr(page, "data-state"), "met")

    def test_review_flag_adds_needs_review_line(self) -> None:
        page = self._today(
            timedelta(hours=15),
            (timedelta(hours=8), "entry"),
            (timedelta(hours=9), "entry"),
            (timedelta(hours=12), "exit"),
            (timedelta(hours=14, minutes=33), "entry"),
        )
        self.assertIn("Inside</h2>", page)
        self.assertIn('<p class="review-line">Needs review: no exit after 08:00</p>', page)
        self.assertIn("data-left>", page)

    def test_today_with_no_complete_pair_shows_incomplete_and_empty_ring(self) -> None:
        page = self._today(timedelta(hours=12), (timedelta(hours=10), "exit"))
        self.assertIn("Incomplete: no exit yet</h2>", page)
        self.assertNotIn("data-left", page)
        self.assertNotIn('class="figure-value', page)
        self.assertNotIn("No punches yet today", page)
        self.assertIn("data-poll=", page)

    def test_no_punches_still_polls_and_notes_previous_day_entry(self) -> None:
        self._sign_in()
        self.database.insert_punches([_punch_row("lv-prev", self.DAY - timedelta(hours=2), "entry")])
        self._at(self.DAY + timedelta(hours=7))
        page = self.client.get("/day").text
        self.assertIn("No punches yet today", page)
        self.assertIn('id="day-live"', page)
        self.assertIn('data-poll="/day?employee=1&amp;date=2026-09-09"', page)
        self.assertIn('Last punch: entry at <span class="num">22:00</span> on the previous work day', page)

    def test_past_day_is_static(self) -> None:
        self._sign_in()
        self._punches((timedelta(hours=9), "entry"), (timedelta(hours=17), "exit"))
        self._at(datetime(2026, 9, 12, 12))
        page = self.client.get("/day?date=2026-09-09").text
        self.assertIn("Target met at 17:00</h2>", page)
        self.assertNotIn("data-poll=", page)
        self.assertNotIn("data-left", page)
        self.assertEqual(self._attr(page, "data-live"), "false")

    def test_htmx_request_gets_only_the_region_and_restore_gets_the_page(self) -> None:
        partial = self._today(
            timedelta(hours=11), (timedelta(hours=9), "entry"), headers={"HX-Request": "true"}
        )
        self.assertTrue(partial.lstrip().startswith("<div id=\"day-live\""))
        self.assertNotIn("<html", partial)
        self.assertIn('class="day-bar" role="img"', partial)
        self.assertNotIn("live-status", partial)
        restore = self.client.get("/day", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
        self.assertIn("<html", restore.text)
        self.assertIn("HX-Request", restore.headers.get("Vary", ""))

    def test_poll_for_a_rolled_over_date_refreshes_the_page(self) -> None:
        self._sign_in()
        self._at(self.DAY + timedelta(days=1, hours=6, seconds=1))
        response = self.client.get("/day?date=2026-09-09", headers={"HX-Request": "true"})
        self.assertEqual(response.headers.get("HX-Refresh"), "true")
        self.assertEqual(response.text, "")

    def test_unauthenticated_partial_gets_hx_redirect_not_signin_html(self) -> None:
        response = self.client.get("/day", headers={"HX-Request": "true"})
        self.assertEqual(response.headers.get("HX-Redirect"), "/signin")
        self.assertNotIn("<form", response.text)
        self.assertEqual(self.client.get("/day").status_code, 302)

    def test_manager_poll_for_a_report_returns_the_reports_region(self) -> None:
        self.database.upsert_employee(device_user_id="900", name="Boss", email="boss@example.com")
        boss = self.database.employee_by_email("boss@example.com")
        self.database.set_employee_role(boss["id"], "manager")
        ravi = self.database.employee_by_email("ravi.kumar@example.com")
        self.database.set_employee_manager(ravi["id"], boss["id"])
        self._sign_in("boss@example.com")
        self._punches((timedelta(hours=9), "entry"))
        self._at(self.DAY + timedelta(hours=10))
        page = self.client.get(
            f"/day?employee={ravi['id']}&date=2026-09-09", headers={"HX-Request": "true"}
        ).text
        self.assertEqual(self._attr(page, "data-base-seconds"), "3600")
        self.assertIn(f'data-poll="/day?employee={ravi["id"]}&amp;date=2026-09-09"', page)

    def test_ring_digits_are_silent_and_status_region_is_outside_and_empty(self) -> None:
        page = self._today(timedelta(hours=11), (timedelta(hours=9), "entry"))
        self.assertIn('class="figure-value is-live" aria-hidden="true" data-tick=', page)
        self.assertIn('<span class="visually-hidden" data-tick-hours="7200">2h 00m</span>', page)
        self.assertNotIn('role="timer"', page)
        self.assertNotIn('aria-live="polite" data-timer', page)
        status = '<div id="live-status" class="visually-hidden" role="status" aria-live="polite"></div>'
        self.assertIn(status, page)
        region_end = page.index('<script src="/static/timer.js"')
        self.assertGreater(page.index(status), region_end)
        self.assertIn('role="img" aria-label="2h 00m effective, 25% of the 8h 00m target"', page)

    def test_log_times_have_seconds_and_stretches_sum_to_the_ring(self) -> None:
        page = self._today(
            timedelta(hours=15, minutes=7, seconds=9),
            (timedelta(hours=9, seconds=12), "entry"),
            (timedelta(hours=12, minutes=30, seconds=41), "exit"),
            (timedelta(hours=13, minutes=2, seconds=3), "entry"),
        )
        self.assertIn('punch-time num">09:00:12</span>', page)
        self.assertIn('punch-time num">12:30:41</span>', page)
        inside = re.search(r'<dt>Inside</dt><dd><span class="num" data-tick="(\d+)"', page).group(1)
        self.assertEqual(inside, self._attr(page, "data-base-seconds"))
        self.assertIn('title="Break 1 · 12:30:41', page)
        self.assertNotIn('class="break-list"', page)

    def test_three_break_day_lists_numbered_breaks_and_total(self) -> None:
        page = self._today(
            timedelta(hours=18),
            (timedelta(hours=9), "entry"), (timedelta(hours=11), "exit"),
            (timedelta(hours=11, minutes=20), "entry"), (timedelta(hours=13), "exit"),
            (timedelta(hours=13, minutes=40), "entry"), (timedelta(hours=15), "exit"),
            (timedelta(hours=15, minutes=20), "entry"), (timedelta(hours=16, minutes=32), "exit"),
        )
        self.assertIn('<dt>Breaks</dt><dd><span class="num">1:20:00</span> · <span class="num">3</span>', page)
        self.assertIn('role="img" aria-label="Inside 6:12, 3 breaks totalling 1:20: break 1 11:00 to 11:20', page)
        for n in (1, 2, 3):
            self.assertIn(f'title="Break {n} · ', page)
        self.assertNotIn("Breaks total", page)
        self.assertNotIn("log-gap", page)

    def test_unclear_gap_reads_unclear_with_the_swipe_time(self) -> None:
        page = self._today(
            timedelta(hours=19),
            (timedelta(hours=9), "entry"), (timedelta(hours=12), "exit"),
            (timedelta(hours=13), "exit"), (timedelta(hours=14), "entry"), (timedelta(hours=18), "exit"),
        )
        self.assertIn('<div class="is-unclear"><dt>Unclear</dt>', page)
        self.assertIn('class="bar-seg is-unclear"', page)
        self.assertIn('title="Unclear · ', page)
        self.assertNotIn('class="break-list"', page)

    def test_week_table_keeps_hours_and_minutes(self) -> None:
        self._sign_in()
        self._punches((timedelta(hours=9), "entry"), (timedelta(hours=16, minutes=42, seconds=30), "exit"))
        self._at(datetime(2026, 9, 11, 12))
        page = self.client.get("/week").text
        self.assertIn("7h 42m", page)
        self.assertNotIn("7:42:30", page)


class _QuickViewBase(unittest.TestCase):
    """Shared setup for the quick-view tests: Ravi plus a pinned clock."""

    DAY = datetime(2026, 9, 9)

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.ravi = self._person("118", "Ravi Kumar")
        common, me = _fixed_now(self.DAY + timedelta(hours=11))
        common.start()
        me.start()
        self.addCleanup(common.stop)
        self.addCleanup(me.stop)

    def _person(self, uid: str, name: str, role: str = "employee") -> dict:
        email = f"{uid}@example.com"
        self.database.upsert_employee(device_user_id=uid, name=name, email=email)
        person = self.database.employee_by_email(email)
        if role != "employee":
            self.database.set_employee_role(person["id"], role)
        return self.database.employee_by_email(email)

    def _sign_in(self, person: dict) -> None:
        self.client.post("/signin", data={"email": person["email"]})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _quick(self, employee_id: int, query: str = ""):
        return self.client.get(f"/quick/{employee_id}{query}", headers={"HX-Request": "true"})


class QuickViewTest(_QuickViewBase):
    """Quick-view drawer fragment: same Today context as /day, same access."""

    def test_hr_gets_fragment_with_name_live_region_and_links(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        response = self._quick(self.ravi["id"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("HX-Request", response.headers.get("Vary", ""))
        page = response.text
        self.assertNotIn("<html", page)
        self.assertIn('id="quick-view-title"', page)
        self.assertIn(">Ravi Kumar</h2>", page)
        self.assertIn('id="day-live"', page)
        self.assertIn(f'href="/day?employee={self.ravi["id"]}"', page)
        self.assertIn(f'href="/week?employee={self.ravi["id"]}"', page)

    def test_person_inside_gets_live_region_polling_their_day(self) -> None:
        self.database.insert_punches([_punch_row("qv-1", self.DAY + timedelta(hours=9), "entry")])
        self._sign_in(self._person("1", "Hema HR", "hr"))
        page = self._quick(self.ravi["id"]).text
        self.assertIn('data-live="true"', page)
        self.assertIn(f'data-poll="/day?employee={self.ravi["id"]}&amp;date=2026-09-09"', page)

    def test_drawer_renders_the_break_view_with_punches_closed(self) -> None:
        # U11: same partial as Today; raw punches always listed.
        rows = [(9, 0, "entry"), (12, 40, "exit"), (13, 10, "entry")]
        self.database.insert_punches([
            _punch_row(f"qv-b{i}", self.DAY + timedelta(hours=h, minutes=m), d) for i, (h, m, d) in enumerate(rows)
        ])
        self._sign_in(self._person("1", "Hema HR", "hr"))
        page = self._quick(self.ravi["id"]).text
        self.assertIn('class="day-bar" role="img" aria-label="Inside', page)
        self.assertIn('<span class="bar-text is-wide">1 · 30m</span>', page)
        self.assertNotIn('<details class="raw-punches"', page)
        self.assertIn('<h3 class="raw-punches-title">All punches (3)</h3>', page)

    def test_edit_details_only_from_people_for_editors(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        self.assertIn("Edit details", self._quick(self.ravi["id"], "?from=people").text)
        self.assertNotIn("Edit details", self._quick(self.ravi["id"]).text)
        self.assertIn('class="button button-secondary button-sm" href="/people/', self._quick(self.ravi["id"], "?from=people").text)

    def test_manager_sees_subtree_only_and_unknown_is_404(self) -> None:
        boss = self._person("900", "Boss", "manager")
        other = self._person("901", "Other")
        self.database.set_employee_manager(self.ravi["id"], boss["id"])
        self._sign_in(boss)
        self.assertEqual(self._quick(self.ravi["id"]).status_code, 200)
        self.assertEqual(self._quick(other["id"]).status_code, 404)
        self.assertEqual(self._quick(99999).status_code, 404)
        self.assertNotIn("Edit details", self._quick(self.ravi["id"], "?from=people").text)

    def test_no_punches_shows_empty_state(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        self.assertIn("No punches yet today", self._quick(self.ravi["id"]).text)

    def test_expired_session_gets_hx_redirect(self) -> None:
        response = self._quick(self.ravi["id"])
        self.assertEqual(response.headers.get("HX-Redirect"), "/signin")
        self.assertNotIn("<form", response.text)

    def test_plain_get_redirects_to_full_day(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        response = self.client.get(f"/quick/{self.ravi['id']}")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/day?employee={self.ravi['id']}"))


class DayStripTest(_QuickViewBase):
    """U8: last-7-days strip in the drawer and on /day (KTD7)."""

    def _chips(self, page: str) -> list[str]:
        return re.findall(r'<a class="day-chip[^>]*>', page)

    def test_drawer_strip_lists_seven_days_ending_today(self) -> None:
        self.database.insert_punches([_punch_row("ds-1", self.DAY - timedelta(days=2) + timedelta(hours=9), "entry")])
        self._sign_in(self._person("1", "Hema HR", "hr"))
        page = self._quick(self.ravi["id"]).text
        chips = self._chips(page)
        self.assertEqual(len(chips), 7)
        self.assertIn("date=2026-09-03", chips[0])
        self.assertIn("date=2026-09-09", chips[-1])
        self.assertIn('aria-current="date"', chips[-1])
        self.assertIn("Wednesday 09 September, Today", page)
        self.assertIn("Monday 07 September, Punched", page)
        self.assertIn("Sunday 06 September, No punches", page)

    def test_strip_respects_day_start_hour(self) -> None:
        common, me = _fixed_now(datetime(2026, 9, 10, 5, 30))
        common.start()
        me.start()
        self.addCleanup(common.stop)
        self.addCleanup(me.stop)
        self._sign_in(self._person("1", "Hema HR", "hr"))
        chips = self._chips(self._quick(self.ravi["id"]).text)
        self.assertIn("date=2026-09-09", chips[-1])
        self.assertIn('aria-current="date"', chips[-1])

    def test_past_day_in_window_renders_without_poll(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        response = self._quick(self.ravi["id"], "?date=2026-09-06")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("data-poll", response.text)
        selected = [c for c in self._chips(response.text) if 'aria-current="date"' in c]
        self.assertEqual(len(selected), 1)
        self.assertIn("date=2026-09-06", selected[0])

    def test_future_and_malformed_dates_are_400(self) -> None:
        # KTD2: no 7-day guard any more; only future and malformed dates fail.
        self._sign_in(self._person("1", "Hema HR", "hr"))
        for value in ("2026-09-10", "nope", "2026-13-01"):
            self.assertEqual(self._quick(self.ravi["id"], f"?date={value}").status_code, 400, value)
        self.assertEqual(self._quick(self.ravi["id"], "?strip_end=nope").status_code, 400)
        for value in ("2026-09-02", "2026-09-03"):
            self.assertEqual(self._quick(self.ravi["id"], f"?date={value}").status_code, 200, value)

    def test_from_people_carried_in_strip_targets(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        rid = self.ravi["id"]
        page = self._quick(rid, "?from=people&date=2026-09-08").text
        self.assertIn(f'data-qv-day="/quick/{rid}?date=2026-09-07&amp;from=people"', page)
        self.assertIn("Edit details", page)
        plain = self._quick(rid).text
        self.assertNotIn("from=people", plain)

    def test_no_access_is_404(self) -> None:
        boss = self._person("900", "Boss", "manager")
        other = self._person("901", "Other")
        self._sign_in(boss)
        self.assertEqual(self._quick(other["id"], "?date=2026-09-08").status_code, 404)

    def test_day_page_strip_keeps_employee_and_marks_selected(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        rid = self.ravi["id"]
        page = self.client.get(f"/day?employee={rid}&date=2026-09-07").text
        chips = self._chips(page)
        self.assertEqual(len(chips), 7)
        self.assertTrue(all(f"employee={rid}" in c for c in chips))
        self.assertNotIn("data-qv-day", page)
        selected = [c for c in chips if 'aria-current="date"' in c]
        self.assertEqual(len(selected), 1)
        self.assertIn("date=2026-09-07", selected[0])
        # Older days get a strip around them (KTD1), the selection marked.
        old = self.client.get(f"/day?employee={rid}&date=2026-08-01").text
        old_selected = [c for c in self._chips(old) if 'aria-current="date"' in c]
        self.assertEqual(len(old_selected), 1)
        self.assertIn("date=2026-08-01", old_selected[0])


class HistoryPagingTest(_QuickViewBase):
    """U6: full history in the drawer and on /day (KTD1, KTD2). Today is
    2026-09-09; Ravi's first punch is 2026-06-01 unless a test says otherwise."""

    def _chips(self, page: str) -> list[str]:
        return re.findall(r'<a class="day-chip[^>]*>', page)

    def _first_punch(self, at: datetime) -> None:
        self.database.insert_punches([_punch_row(f"hp-{at.isoformat()}", at, "entry")])

    def _selected(self, page: str) -> list[str]:
        return [c for c in self._chips(page) if 'aria-current="date"' in c]

    def test_drawer_opens_a_day_forty_days_back_without_polling(self) -> None:
        self._first_punch(datetime(2026, 6, 1, 9))
        self._sign_in(self._person("1", "Hema HR", "hr"))
        response = self._quick(self.ravi["id"], "?date=2026-07-31")
        self.assertEqual(response.status_code, 200)
        page = response.text
        self.assertIn('id="day-live"', page)
        self.assertNotIn("data-poll", page)
        self.assertIn('value="2026-07-31"', page)
        chips = self._chips(page)
        self.assertEqual(len(chips), 7)
        self.assertIn("date=2026-08-03", chips[-1])
        self.assertIn("date=2026-07-31", self._selected(page)[0])

    def test_day_page_strip_ends_three_days_after_an_old_selection(self) -> None:
        self._first_punch(datetime(2026, 6, 1, 9))
        self._sign_in(self._person("1", "Hema HR", "hr"))
        rid = self.ravi["id"]
        page = self.client.get(f"/day?employee={rid}&date=2026-07-31").text
        chips = self._chips(page)
        self.assertIn("date=2026-07-28", chips[0])
        self.assertIn("date=2026-08-03", chips[-1])
        self.assertTrue(all("strip_end=2026-08-03" in c for c in chips))
        self.assertIn("date=2026-07-31", self._selected(page)[0])
        # Paging keeps the selected day and moves the strip a week.
        self.assertIn('aria-label="Earlier week, 21 Jul to 27 Jul"', page)
        self.assertIn(f'href="/day?employee={rid}&amp;date=2026-07-31&amp;strip_end=2026-07-27"', page)
        self.assertIn('aria-label="Later week, 04 Aug to 10 Aug"', page)
        later = self.client.get(f"/day?employee={rid}&date=2026-07-31&strip_end=2026-08-10").text
        self.assertIn("date=2026-08-10", self._chips(later)[-1])
        self.assertEqual(self._selected(later), [])
        self.assertIn("Friday 31 July 2026", later)

    def test_selected_today_ends_at_today_with_no_later_week(self) -> None:
        self._first_punch(datetime(2026, 6, 1, 9))
        self._sign_in(self._person("1", "Hema HR", "hr"))
        for page in (self.client.get(f"/day?employee={self.ravi['id']}").text, self._quick(self.ravi["id"]).text):
            chips = self._chips(page)
            self.assertIn('aria-label="Wednesday 09 September, Today, no punches"', chips[-1])
            self.assertIn('aria-current="date"', chips[-1])
            self.assertNotIn('data-qv-page="later"', page)
            self.assertNotIn('aria-label="Later week,', page)
            self.assertIn('data-qv-page="earlier"', page)

    def test_strip_end_beyond_today_is_clamped(self) -> None:
        self._first_punch(datetime(2026, 6, 1, 9))
        self._sign_in(self._person("1", "Hema HR", "hr"))
        rid = self.ravi["id"]
        page = self.client.get(f"/day?employee={rid}&date=2026-09-05&strip_end=2026-12-01").text
        self.assertIn('aria-label="Wednesday 09 September, Today, no punches"', self._chips(page)[-1])
        self.assertFalse(any("strip_end=" in c for c in self._chips(page)))
        drawer = self._quick(rid, "?date=2026-09-05&strip_end=2026-12-01")
        self.assertEqual(drawer.status_code, 200)
        self.assertIn('aria-label="Wednesday 09 September, Today, no punches"', self._chips(drawer.text)[-1])
        # A malformed strip_end on /day falls back like a malformed date.
        self.assertEqual(self.client.get(f"/day?employee={rid}&strip_end=nope").status_code, 200)

    def test_earlier_week_disabled_at_the_first_punch_week(self) -> None:
        self._first_punch(datetime(2026, 8, 20, 9))
        self._sign_in(self._person("1", "Hema HR", "hr"))
        rid = self.ravi["id"]
        at_first = self.client.get(f"/day?employee={rid}&date=2026-08-20&strip_end=2026-08-25").text
        self.assertIn('aria-disabled="true" aria-label="Earlier week"', at_first)
        self.assertNotIn('data-qv-page="earlier"', at_first)
        self.assertIn('min="2026-08-20" max="2026-09-09"', at_first)
        week_after = self.client.get(f"/day?employee={rid}&date=2026-08-20&strip_end=2026-09-01").text
        self.assertIn('aria-label="Earlier week, 19 Aug to 25 Aug"', week_after)
        # A strip_end before the first punch week snaps forward to it.
        too_early = self.client.get(f"/day?employee={rid}&date=2026-08-20&strip_end=2026-07-01").text
        self.assertIn("date=2026-08-26", self._chips(too_early)[-1])

    def test_day_before_first_punch_says_so(self) -> None:
        self._first_punch(datetime(2026, 8, 20, 9))
        self._sign_in(self._person("1", "Hema HR", "hr"))
        rid = self.ravi["id"]
        expected = "Before Ravi Kumar&#39;s first punch on 20 August 2026"
        self.assertIn(expected, self._quick(rid, "?date=2026-08-10").text)
        self.assertIn(expected, self.client.get(f"/day?employee={rid}&date=2026-08-10").text)
        self.assertNotIn("first punch on", self._quick(rid, "?date=2026-08-21").text)

    def test_person_with_no_punches_gets_today_as_picker_min(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        rid = self.ravi["id"]
        for response in (self.client.get(f"/day?employee={rid}"), self._quick(rid, "?date=2026-07-31")):
            self.assertEqual(response.status_code, 200)
            self.assertIn('min="2026-09-09" max="2026-09-09"', response.text)
            self.assertIn('aria-disabled="true" aria-label="Earlier week"', response.text)

    def test_no_access_is_still_404_for_old_days(self) -> None:
        boss = self._person("900", "Boss", "manager")
        other = self._person("901", "Other")
        self._sign_in(boss)
        self.assertEqual(self._quick(other["id"], "?date=2026-07-31").status_code, 404)
        self.assertEqual(self.client.get(f"/day?employee={other['id']}&date=2026-07-31").status_code, 404)

    def test_plain_get_redirect_keeps_the_date(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        rid = self.ravi["id"]
        response = self.client.get(f"/quick/{rid}?date=2026-07-31")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/day?employee={rid}&date=2026-07-31"))

    def test_drawer_arrows_and_picker_carry_from_and_strip_end(self) -> None:
        self._first_punch(datetime(2026, 6, 1, 9))
        self._sign_in(self._person("1", "Hema HR", "hr"))
        rid = self.ravi["id"]
        page = self._quick(rid, "?from=people&date=2026-07-31").text
        self.assertIn(f'data-qv-day="/quick/{rid}?date=2026-07-31&amp;strip_end=2026-07-27&amp;from=people"', page)
        self.assertIn(f'data-qv-day="/quick/{rid}?date=2026-07-30&amp;strip_end=2026-08-03&amp;from=people"', page)
        self.assertIn(f'data-qv-date="/quick/{rid}?from=people"', page)
        self.assertIn('min="2026-06-01" max="2026-09-09"', page)


class CorrectionOverlayWebTest(_QuickViewBase):
    """U2/KTD4: approved fixes change hours on every screen; raw punches,
    Frappe and /api/checkins never see them."""

    DAY = datetime(2026, 9, 9)

    def setUp(self) -> None:
        super().setUp()
        # Pin the clock after the day so Team and People show it as today, closed.
        common, me = _fixed_now(self.DAY + timedelta(hours=20))
        common.start()
        me.start()
        self.addCleanup(common.stop)
        self.addCleanup(me.stop)
        self.hema = self._person("1", "Hema HR", "hr")
        at = lambda h, m=0: self.DAY + timedelta(hours=h, minutes=m)  # noqa: E731
        # 09:05 is a stray In; 13:40 was recorded Out but was an In.
        self.database.insert_punches([
            _punch_row("ov-in", at(9), "entry"), _punch_row("ov-stray", at(9, 5), "entry"),
            _punch_row("ov-out", at(13), "exit"), _punch_row("ov-wrong", at(13, 40), "exit"),
            _punch_row("ov-last", at(17), "exit"),
        ])
        self.lines = [
            {"kind": "ignore", "target_fingerprint": "ov-stray"},
            {"kind": "flip", "target_fingerprint": "ov-wrong", "direction": "entry"},
            {"kind": "add_in", "event_time": at(17, 30)},
            {"kind": "add_out", "event_time": at(18, 30)},
        ]

    def _decide(self, status: str) -> None:
        request_id = self.database.file_fix_lines(
            self.ravi["id"], self.DAY.date(), self.lines, source="session")["request_id"]
        self.database.decide_fix_lines(request_id, [
            {"line_id": l["id"], "status": status} for l in self.database.fix_request(request_id)["lines"]
        ], actor_id=self.hema["id"], note="Checked" if status == "rejected" else None)

    def _screens(self) -> dict:
        import csv as csv_module

        from essl_hub import export
        self._sign_in(self.hema)
        day = self.DAY.date().isoformat()
        csv_text = self.client.get(f"/export.csv?from={day}&to={day}").text
        row = next(r for r in csv_module.reader(io.StringIO(csv_text.lstrip("\ufeff"))) if "Ravi Kumar" in r)
        team = self.client.get("/team").text
        team_row = team[team.index("Ravi Kumar"):]
        return {
            "day": self.client.get(f"/day?employee={self.ravi['id']}").text,
            "quick": self._quick(self.ravi["id"]).text,
            "team": team_row[:team_row.index("</tr>")],
            "people": self.client.get("/people").text,
            "csv": row[export.DAILY_HEADER.index("Effective")],
        }

    def test_raw_day_reads_short_with_review(self) -> None:
        screens = self._screens()
        self.assertIn("3h 55m", screens["team"])
        self.assertEqual(screens["csv"], "3:55")
        self.assertNotIn("added by HR", screens["day"])

    def test_approved_fixes_show_the_same_corrected_hours_everywhere(self) -> None:
        self._decide("approved")
        screens = self._screens()
        # 09:00-13:00, 13:40-17:00, 17:30-18:30 = 8h 20m.
        self.assertIn("8h 20m", screens["team"])
        self.assertEqual(screens["csv"], "8:20")
        for name in ("day", "quick"):
            page = screens[name]
            self.assertIn("8h 20m", page)
            self.assertIn('<h3 class="raw-punches-title">All punches (7)</h3>', page)
            self.assertIn('<span class="chip chip-neutral">ignored</span>', page)
            self.assertIn('<span class="chip chip-info">direction fixed</span>', page)
            self.assertEqual(page.count('<span class="chip chip-info">added by HR</span>'), 2)
            self.assertIn("2 punches added by HR, 1 punch ignored", page)
            self.assertNotIn("Fix pending", page)
        self.assertIn('status-chip status-met">Met</span>', screens["people"])
        # The device rows are untouched.
        self.assertEqual([p.get("trusted_direction") for p in self.database.punches],
                         ["entry", "entry", "exit", "exit", "exit"])

    def test_day_chip_says_pending_then_rejected_and_hours_stay_raw(self) -> None:
        self.database.file_fix_lines(self.ravi["id"], self.DAY.date(), self.lines[:1], source="session")
        self.assertIn('<span class="chip chip-info">Fix pending</span>', self._screens()["day"])
        request_id = self.database.pending_fix_request(self.ravi["id"], self.DAY.date())["id"]
        line = self.database.fix_request(request_id)["lines"][0]["id"]
        self.database.decide_fix_lines(request_id, [{"line_id": line, "status": "rejected"}],
                                       actor_id=self.hema["id"], note="Badge log agrees")
        screens = self._screens()
        self.assertIn('<span class="chip chip-warn">Fix rejected</span>', screens["day"])
        self.assertEqual(screens["csv"], "3:55")
        self.assertNotIn(">ignored</span>", screens["day"])


class QuickViewDrawerTest(_QuickViewBase):
    """U6: one drawer in the shell, name links carry data-quick-view."""

    def test_team_and_people_have_one_drawer_and_quick_view_links(self) -> None:
        self._sign_in(self._person("1", "Hema HR", "hr"))
        rid = self.ravi["id"]
        team = self.client.get("/team").text
        self.assertEqual(team.count('<dialog id="quick-view"'), 1)
        self.assertIn(f'href="/day?employee={rid}" data-quick-view="/quick/{rid}"', team)
        self.assertNotIn("hx-trigger", team.split('data-quick-view')[1][:200])
        people = self.client.get("/people").text
        self.assertEqual(people.count('<dialog id="quick-view"'), 1)
        self.assertIn(f'href="/people/{rid}/edit" data-quick-view="/quick/{rid}?from=people"', people)

    def test_day_loads_timer_once(self) -> None:
        self._sign_in(self.ravi)
        page = self.client.get("/day").text
        self.assertEqual(page.count("timer.js"), 1)
        self.assertEqual(page.count("quick-view.js"), 1)

    def test_employee_day_renders_with_unused_drawer(self) -> None:
        self._sign_in(self.ravi)
        response = self.client.get(f"/day?employee={self.ravi['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertIn('<dialog id="quick-view"', response.text)
        self.assertNotIn("data-quick-view=", response.text)


class AdminViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="1", name="Admin", email="admin@example.com")
        admin = self.database.employee_by_email("admin@example.com")
        self.database.set_employee_role(admin["id"], "admin")
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "admin@example.com"})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/settings").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _set_role(self, employee_id: int, role: str):
        return self.client.post(
            f"/people/{employee_id}/role", data={"role": role, "csrf": self._csrf()}
        )

    def _add_employee(self, device_user_id: str = "118", name: str = "Ravi Kumar") -> dict:
        self.database.upsert_employee(
            device_user_id=device_user_id,
            name=name,
            email=f"{device_user_id}@example.com",
        )
        return self.database.employee_by_email(f"{device_user_id}@example.com")

    def test_admin_redirects_to_devices(self) -> None:
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/devices"))

    def test_settings_holds_target_and_keys_without_an_employee_table(self) -> None:
        self.database.upsert_employee(
            device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com"
        )
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Daily target", response.text)
        self.assertIn("HRMS API keys", response.text)
        self.assertNotIn("Ravi Kumar", response.text)

    def test_valid_roster_upload_creates_employees_and_shows_outcomes(self) -> None:
        csv_bytes = (
            b"name,work email,device user id\r\n"
            b"Karthik,karthik@example.com,184\r\n"
            b"Vijay,vijay@example.com,55\r\n"
        )
        response = self.client.post(
            "/people/import",
            data={"roster": (io.BytesIO(csv_bytes), "roster.csv"), "csrf": self._csrf()},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("<strong>2</strong> added", response.text)
        self.assertIsNotNone(self.database.employee_by_email("karthik@example.com"))
        self.assertIsNotNone(self.database.employee_by_email("vijay@example.com"))

    def test_setting_target_changes_the_stored_minutes(self) -> None:
        response = self.client.post(
            "/settings/target",
            data={"hours": "7", "minutes": "30", "csrf": self._csrf()},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.database.setting("daily_target_minutes"), "450")

    def test_setting_day_start_hour_is_stored_and_confirmed(self) -> None:
        self.assertEqual(self.database.setting("day_start_hour", "6"), "6")
        self.assertIn('name="hour"', self.client.get("/settings").text)
        response = self.client.post(
            "/settings/day-start", data={"hour": "0", "csrf": self._csrf()}, follow_redirects=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.database.setting("day_start_hour"), "0")
        self.assertIn("Work days now start at 00:00.", response.text)

    def test_day_start_hour_outside_range_is_refused_inline(self) -> None:
        for bad in ("24", "-1", "abc", ""):
            response = self.client.post("/settings/day-start", data={"hour": bad, "csrf": self._csrf()})
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn("Enter a whole hour from 0 to 23.", response.text)
        self.assertIsNone(self.database.setting("day_start_hour"))

    def test_day_start_hour_requires_csrf(self) -> None:
        response = self.client.post("/settings/day-start", data={"hour": "5"})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.database.setting("day_start_hour"))

    def test_covers_r21_unmapped_device_id_is_shown_on_the_devices_page(self) -> None:
        # Covers R21.
        self.database.insert_punches(
            [
                {
                    "fingerprint": "fp-unmapped",
                    "device_id": DEVICES[1].id,
                    "site": "Gachibowli",
                    "device_user_id": "231",
                    "event_timestamp": _ist_now(),
                }
            ]
        )
        devices = self.client.get("/devices").text
        self.assertIn('href="/people/issues"', devices)
        people = self.client.get("/people").text
        self.assertIn('class="nav-count"', people)
        self.assertIn('class="subtab-count"', people)
        page = self.client.get("/people/issues").text
        self.assertIn("231", page)
        self.assertIn("Not in Directory", page)
        self.assertIn('href="/people/new?device_user_id=231', page)

    def test_name_mismatch_can_be_marked_same_person(self) -> None:
        self.database.upsert_employee(device_user_id="NP0002", name="Ravi Teja", email="ravi@example.com")
        self.database.insert_punches([{"fingerprint": "fp-nm", "device_id": DEVICES[0].id, "site": "Uppal",
                                       "device_user_id": "NP0002", "employee_name": "Mohan Das", "event_timestamp": _ist_now()}])
        page = self.client.get("/people/issues").text
        self.assertIn("Name differs", page)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        self.client.post("/people/issues/dismiss", data={"csrf": csrf, "key": "name:NP0002:das mohan"})
        self.assertNotIn("Name differs", self.client.get("/people/issues").text)
        # A rename on the device brings the issue back.
        self.database.insert_punches([{"fingerprint": "fp-nm2", "device_id": DEVICES[0].id, "site": "Uppal",
                                       "device_user_id": "NP0002", "employee_name": "Suresh K",
                                       "event_timestamp": _ist_now() + timedelta(seconds=5)}])
        self.assertIn("Name differs", self.client.get("/people/issues").text)

    def test_device_name_is_the_latest_punch_name(self) -> None:
        now = _ist_now()
        self.database.insert_punches([
            {"fingerprint": "fp-a", "device_id": DEVICES[0].id, "site": "Uppal", "device_user_id": "NP0777",
             "employee_name": "Zara Old", "event_timestamp": now - timedelta(days=2)},
            {"fingerprint": "fp-b", "device_id": DEVICES[0].id, "site": "Uppal", "device_user_id": "NP0777",
             "employee_name": "Bala New", "event_timestamp": now},
        ])
        page = self.client.get("/people/issues").text
        self.assertIn("On device: <strong>Bala New</strong>", page)
        self.assertNotIn("Zara Old", page)

    def test_link_confirm_states_punch_count(self) -> None:
        self.database.upsert_employee(device_user_id="sita@example.com", name="Sita Rao", email="sita@example.com")
        self.database.insert_punches([{"fingerprint": f"fp-c{i}", "device_id": DEVICES[0].id, "site": "Uppal",
                                       "device_user_id": "NP0231", "employee_name": "Sita R",
                                       "event_timestamp": _ist_now() - timedelta(minutes=i)} for i in range(3)])
        page = self.client.get("/people/issues?confirm=1&device_user_id=NP0231&person=sita@example.com").text
        self.assertIn("Link NP0231 to Sita Rao?", page)
        self.assertIn("<strong>3 punches</strong> will start counting for Sita Rao", page)
        # Nothing changes until the confirm form posts.
        self.assertEqual(self.database.employee_by_email("sita@example.com")["device_user_id"], "sita@example.com")

    def test_crafted_error_param_is_not_rendered(self) -> None:
        page = self.client.get("/people/issues?error=Call+999+now&linked=X&who=Evil").text
        # Only the theme form's escaped return URL may echo the query.
        self.assertNotIn('class="notice notice-bad"', page)
        self.assertNotIn("Linked X to Evil", page)

    def test_never_punched_dismiss_lowers_badge(self) -> None:
        self.database.upsert_employee(device_user_id="NP0500", name="Ghost", email="ghost@example.com")
        page = self.client.get("/people/issues").text
        before = int(re.search(r'class="subtab-count"[^>]*>(\d+)<', page).group(1))
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        self.client.post("/people/issues/dismiss", data={"csrf": csrf, "key": "never:NP0500"})
        page = self.client.get("/people/issues").text
        self.assertNotIn("never:NP0500", page)
        after = re.search(r'class="subtab-count"[^>]*>(\d+)<', page)
        self.assertEqual(int(after.group(1)) if after else 0, before - 1)

    def test_htmx_partial_skips_badge(self) -> None:
        from essl_hub.routes import people as people_routes

        with patch.object(people_routes, "issue_count", side_effect=AssertionError("badge computed")):
            response = self.client.get("/people", headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 200)

    def test_old_devices_ids_url_redirects_to_people(self) -> None:
        response = self.client.get("/devices/ids")
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response.headers["Location"].endswith("/people/issues"))

    def test_unknown_id_can_be_linked_to_a_roster_person(self) -> None:
        self.database.upsert_employee(device_user_id="sita@example.com", name="Sita Rao", email="sita@example.com")
        self.database.insert_punches([{"fingerprint": "fp-link", "device_id": DEVICES[0].id, "site": "Uppal",
                                       "device_user_id": "NP0231", "employee_name": "Sita R", "event_timestamp": _ist_now()}])
        page = self.client.get("/people/issues").text
        self.assertIn("Link to Sita Rao", page)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        sita = self.database.employee_by_email("sita@example.com")
        response = self.client.post("/people/issues/link", data={"csrf": csrf, "device_user_id": "NP0231", "employee_id": sita["id"]},
                                    follow_redirects=True)
        self.assertIn("Linked NP0231 to Sita Rao", response.text)
        self.assertIn("1 past punch now count", response.text)
        # The flash shows once.
        self.assertNotIn("Linked NP0231", self.client.get("/people/issues").text)
        self.assertEqual(self.database.employee_by_id(sita["id"])["device_user_id"], "NP0231")
        self.assertNotIn("Not in Directory", response.text)

    def test_link_refuses_an_id_someone_else_has(self) -> None:
        self.database.upsert_employee(device_user_id="NP0005", name="Owner", email="owner@example.com")
        self.database.upsert_employee(device_user_id="new@example.com", name="New", email="new@example.com")
        new = self.database.employee_by_email("new@example.com")
        response = self.client.post("/people/issues/link", data={"csrf": self._csrf(), "device_user_id": "NP0005", "employee_id": new["id"]},
                                    follow_redirects=True)
        self.assertIn("Owner already uses device ID NP0005", response.text)
        self.assertEqual(self.database.employee_by_id(new["id"])["device_user_id"], "new@example.com")

    def test_add_person_form_is_prefilled_from_the_issue(self) -> None:
        page = self.client.get("/people/new?device_user_id=NP0231&name=New+Joiner").text
        self.assertIn('value="NP0231"', page)
        self.assertIn('value="New Joiner"', page)

    def test_people_subtab_reads_directory(self) -> None:
        page = self.client.get("/people").text
        self.assertIn('href="/people" aria-current="page">Directory</a>', page)
        self.assertNotIn(">Roster</a>", page)

    def test_admin_navigation_links_to_the_employee_data(self) -> None:
        # An admin is authorised for Employees, so the nav must offer it.
        page = self.client.get("/settings")
        self.assertIn('href="/team"', page.text)

    # roster templates (R1) --------------------------------------------------

    def test_roster_templates_match_the_importer(self) -> None:
        csv_response = self.client.get("/people/import/template.csv")
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("attachment", csv_response.headers["Content-Disposition"])
        self.assertEqual(
            csv_response.text.strip(), "name,work email,device user id,manager email,role"
        )
        # the file the admin downloads must import cleanly as-is
        self.assertEqual(roster.parse_csv(csv_response.text), [])

        xlsx_response = self.client.get("/people/import/template.xlsx")
        self.assertEqual(xlsx_response.status_code, 200)
        self.assertEqual(roster.parse_xlsx(xlsx_response.data), [])
        workbook = openpyxl.load_workbook(io.BytesIO(xlsx_response.data))
        self.assertEqual(workbook.sheetnames, ["Roster", "Instructions"])
        header = [cell.value for cell in workbook["Roster"][1]]
        self.assertIn("manager email", header)
        self.assertIn("role", header)

    def test_import_page_links_both_templates_and_admin_links_to_it(self) -> None:
        page = self.client.get("/people/import")
        self.assertIn('href="/people/import/template.csv"', page.text)
        self.assertIn('href="/people/import/template.xlsx"', page.text)
        self.assertIn('href="/people/import"', self.client.get("/people").text)

    # roles (R3 follow-up) --------------------------------------------------

    def test_admin_can_set_an_employees_role(self) -> None:
        ajay = self._add_employee()
        response = self._set_role(ajay["id"], "hr")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.database.employee_by_id(ajay["id"])["role"], "hr")
        self.assertIn("hr", self.client.get("/people").text)

    def test_role_change_requires_csrf(self) -> None:
        ajay = self._add_employee()
        response = self.client.post(
            f"/people/{ajay['id']}/role", data={"role": "hr"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.database.employee_by_id(ajay["id"])["role"], "employee")

    def test_unknown_role_is_refused_without_a_write(self) -> None:
        ajay = self._add_employee()
        response = self._set_role(ajay["id"], "superuser")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Choose employee, manager, hr or admin", response.text)
        self.assertEqual(self.database.employee_by_id(ajay["id"])["role"], "employee")

    def test_an_admin_cannot_change_their_own_role(self) -> None:
        admin = self.database.employee_by_email("admin@example.com")
        response = self._set_role(admin["id"], "hr")
        self.assertEqual(response.status_code, 200)
        self.assertIn("cannot change your own role", response.text)
        self.assertEqual(self.database.employee_by_id(admin["id"])["role"], "admin")

    def test_creating_a_third_active_key_is_refused(self) -> None:
        self.client.post("/settings/keys", data={"label": "Key one", "csrf": self._csrf()})
        self.client.post("/settings/keys", data={"label": "Key two", "csrf": self._csrf()})
        response = self.client.post("/settings/keys", data={"label": "Key three", "csrf": self._csrf()})
        self.assertIn("already active", response.text)
        self.assertEqual(self.database.active_api_key_count(), 2)


class AdminDeviceEditTest(unittest.TestCase):
    """Covers R4, R6, R15, R20: editing a device's connection settings."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="1", name="Admin", email="admin@example.com")
        admin = self.database.employee_by_email("admin@example.com")
        self.database.set_employee_role(admin["id"], "admin")
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "admin@example.com"})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/settings").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _post(self, device_id: str, **data):
        data.setdefault("csrf", self._csrf())
        return self.client.post(f"/devices/{device_id}", data=data)

    def test_edit_persists_and_redirects_with_saved_marker(self) -> None:
        response = self._post("uppal-x990", ip="192.168.5.5", port="5000", comm_password="")
        self.assertEqual(response.status_code, 302)
        self.assertIn("saved=uppal-x990", response.headers["Location"])
        device = self.database.device_by_id("uppal-x990")
        self.assertEqual((device["ip"], device["port"]), ("192.168.5.5", 5000))
        self.assertIsNotNone(device["connection_updated_at"])
        page = self.client.get("/devices?saved=uppal-x990")
        self.assertIn("192.168.5.5", page.text)
        self.assertIn("Saved", page.text)

    def test_pending_until_a_newer_attempt_then_applied(self) -> None:
        self._post("uppal-x990", ip="192.168.1.201", port="4370", comm_password="")
        device = self.database.device_by_id("uppal-x990")
        device["last_attempt"] = device["connection_updated_at"] - timedelta(minutes=5)
        page = self.client.get("/devices")
        self.assertIn("Change pending", page.text)
        device["last_attempt"] = device["connection_updated_at"] + timedelta(minutes=1)
        page = self.client.get("/devices")
        self.assertIn("Change applied", page.text)
        self.assertNotIn("Change pending", page.text)

    def test_non_numeric_port_renders_card_scoped_error_without_writing(self) -> None:
        before = dict(self.database.device_by_id("uppal-x990"))
        response = self._post("uppal-x990", ip="192.168.1.1", port="abc", comm_password="")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.text.count("Port must be a whole number between 1 and 65535"), 1
        )
        self.assertIn('value="abc"', response.text)
        after = self.database.device_by_id("uppal-x990")
        self.assertEqual((before["ip"], before["port"]), (after["ip"], after["port"]))

    def test_port_out_of_range_is_rejected(self) -> None:
        response = self._post("uppal-x990", ip="192.168.1.201", port="70000", comm_password="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Port must be a whole number between 1 and 65535", response.text)

    def test_blank_ip_is_rejected(self) -> None:
        response = self._post("uppal-x990", ip="   ", port="4370", comm_password="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("IP address is required", response.text)

    def test_non_numeric_comm_password_on_zk_device_is_rejected(self) -> None:
        response = self._post(
            "uppal-x990", ip="192.168.1.201", port="4370", comm_password="not-a-number"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.count("comm password for Uppal must be a number"), 1)
        self.assertIsNone(self.database.device_by_id("uppal-x990")["comm_password"])

    def test_blank_comm_password_keeps_the_stored_value(self) -> None:
        self.database.device_by_id("gachibowli-c3-400")["comm_password"] = "secret"
        self._post("gachibowli-c3-400", ip="192.168.1.202", port="4370", comm_password="")
        self.assertEqual(self.database.device_by_id("gachibowli-c3-400")["comm_password"], "secret")

    def test_clear_checkbox_sets_null_and_env_fallback_applies(self) -> None:
        # Covers R6: clearing returns the device to the environment fallback.
        from essl_hub import config

        self.database.device_by_id("gachibowli-c3-400")["comm_password"] = "secret"
        response = self._post(
            "gachibowli-c3-400",
            ip="192.168.1.202",
            port="4370",
            comm_password="",
            clear_comm_password="1",
        )
        self.assertEqual(response.status_code, 302)
        stored = self.database.device_by_id("gachibowli-c3-400")["comm_password"]
        self.assertIsNone(stored)
        device = config.DeviceConfig(
            id="gachibowli-c3-400",
            site="Gachibowli",
            model="C3-400",
            ip="192.168.1.202",
            port=4370,
            protocol="c3-pullsdk",
            password_env="ESSL_C3_PASSWORD",
            password=stored,
        )
        with patch.dict(os.environ, {"ESSL_C3_PASSWORD": "envpw"}):
            self.assertEqual(config.password_for(device), "envpw")

    def test_unknown_device_id_is_404(self) -> None:
        response = self._post("ghost-device", ip="1.2.3.4", port="4370", comm_password="")
        self.assertEqual(response.status_code, 404)

    def test_missing_csrf_token_is_rejected_without_writing(self) -> None:
        before = dict(self.database.device_by_id("uppal-x990"))
        response = self.client.post(
            "/devices/uppal-x990", data={"ip": "192.168.5.5", "port": "5000"}
        )
        self.assertEqual(response.status_code, 400)
        after = self.database.device_by_id("uppal-x990")
        self.assertEqual((before["ip"], before["port"]), (after["ip"], after["port"]))

    def test_invalid_csrf_token_is_rejected_without_writing(self) -> None:
        response = self.client.post(
            "/devices/uppal-x990",
            data={"csrf": "bogus", "ip": "192.168.5.5", "port": "5000"},
        )
        self.assertEqual(response.status_code, 400)

    def test_non_admin_role_is_forbidden(self) -> None:
        self.client.post("/signout")
        self.database.upsert_employee(device_user_id="900", name="Priya HR", email="priya@example.com")
        priya = self.database.employee_by_email("priya@example.com")
        self.database.set_employee_role(priya["id"], "hr")
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "priya@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        response = self.client.post(
            "/devices/uppal-x990",
            data={"csrf": "x", "ip": "1.2.3.4", "port": "4370"},
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_device_post_redirects_to_signin(self) -> None:
        self.client.post("/signout")
        response = self.client.post(
            "/devices/uppal-x990", data={"ip": "1.2.3.4", "port": "4370"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/signin", response.headers["Location"])


class AdminDeviceLifecycleTest(AdminDeviceEditTest):
    """Covers R7, R8, R9: adding, retiring and re-enabling devices."""

    def _add(self, **data):
        data.setdefault("csrf", self._csrf())
        data.setdefault("site", "Kondapur")
        data.setdefault("family", "c3-400")
        data.setdefault("port", "4370")
        return self.client.post("/devices/add", data=data)

    def test_admin_adds_a_c3_at_a_new_ip_and_it_shows_pending(self) -> None:
        response = self._add(ip="192.168.1.210")
        self.assertEqual(response.status_code, 302)
        self.assertIn("added=kondapur-c3-400", response.headers["Location"])
        device = self.database.device_by_id("kondapur-c3-400")
        self.assertEqual(
            (device["site"], device["model"], device["protocol"], device["ip"], device["port"]),
            ("Kondapur", "C3-400", "c3-pullsdk", "192.168.1.210", 4370),
        )
        self.assertIsNone(device["retired_at"])
        page = self.client.get("/devices?added=kondapur-c3-400").text
        self.assertIn("Pending", page)
        self.assertIn("about 2 minutes", page)

    def test_duplicate_active_address_is_rejected_inline(self) -> None:
        before = set(self.database.devices_by_id)
        response = self._add(ip="192.168.1.201", site="Uppal Annex")
        self.assertEqual(response.status_code, 200)
        self.assertIn("192.168.1.201:4370 is already used by Uppal X990", response.text)
        self.assertIn('value="Uppal Annex"', response.text)
        self.assertEqual(set(self.database.devices_by_id), before)

    def test_edit_onto_another_active_address_is_rejected(self) -> None:
        response = self._post("uppal-x990", ip="192.168.1.202", port="4370", comm_password="")
        self.assertEqual(response.status_code, 200)
        self.assertIn("already used by Gachibowli C3-400", response.text)
        self.assertEqual(self.database.device_by_id("uppal-x990")["ip"], "192.168.1.201")

    def test_address_of_a_retired_device_offers_reenable(self) -> None:
        self.database.set_device_retired("gachibowli-c3-400", True)
        before = set(self.database.devices_by_id)
        response = self._add(ip="192.168.1.202")
        self.assertEqual(response.status_code, 200)
        self.assertIn("retired device Gachibowli C3-400", response.text)
        self.assertIn("/devices/gachibowli-c3-400/enable", response.text)
        self.assertEqual(set(self.database.devices_by_id), before)

    def test_invalid_inputs_give_inline_errors(self) -> None:
        cases = [
            ({"ip": "999.1.1.1"}, "is not a valid IP address"),
            ({"ip": "192.168.1.210", "port": "0"}, "Port must be a whole number"),
            ({"ip": "192.168.1.210", "family": "x990", "site": "Miyapur",
              "comm_password": "abc"}, "comm password for Miyapur must be a number"),
            ({"ip": "192.168.1.210", "site": " "}, "Site name is required"),
            ({"ip": "192.168.1.210", "family": "bogus"}, "Choose a device family"),
        ]
        before = set(self.database.devices_by_id)
        for data, message in cases:
            with self.subTest(message=message):
                response = self._add(**data)
                self.assertEqual(response.status_code, 200)
                self.assertIn(message, response.text)
        self.assertEqual(set(self.database.devices_by_id), before)

    def test_retire_then_reenable_keeps_punches(self) -> None:
        self.database.punches.append({"fingerprint": "f1", "device_id": "uppal-x990"})
        response = self.client.post("/devices/uppal-x990/retire", data={"csrf": self._csrf()})
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(self.database.device_by_id("uppal-x990")["retired_at"])
        page = self.client.get("/devices").text
        self.assertIn("Retired (1)", page)
        self.assertIn("Re-enable", page)
        response = self.client.post("/devices/uppal-x990/enable", data={"csrf": self._csrf()})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.database.device_by_id("uppal-x990")["retired_at"])
        self.assertEqual(len(self.database.punches), 1)

    def test_reenable_refused_when_address_now_taken(self) -> None:
        self.database.set_device_retired("gachibowli-c3-400", True)
        self._add(ip="192.168.1.202", site="Gachibowli New")
        # The retired match blocks the add; move the new device in by edit instead.
        self._add(ip="192.168.1.220", site="Gachibowli New")
        self._post("gachibowli-new-c3-400", ip="192.168.1.202", port="4370", comm_password="")
        response = self.client.post("/devices/gachibowli-c3-400/enable", data={"csrf": self._csrf()})
        self.assertEqual(response.status_code, 200)
        self.assertIn("already used by Gachibowli New", response.text)
        self.assertIsNotNone(self.database.device_by_id("gachibowli-c3-400")["retired_at"])

    def test_slug_collision_gets_a_suffix(self) -> None:
        self._add(ip="192.168.1.230", site="Uppal", family="x990")
        self.assertIsNotNone(self.database.device_by_id("uppal-x990-2"))
        self._add(ip="192.168.1.231", site="Uppal", family="x990")
        self.assertIsNotNone(self.database.device_by_id("uppal-x990-3"))

    def test_missing_csrf_is_refused_for_add_and_retire(self) -> None:
        before = set(self.database.devices_by_id)
        self.assertEqual(
            self.client.post("/devices/add", data={"site": "X", "family": "x990",
                                                   "ip": "10.0.0.1", "port": "4370"}).status_code,
            400,
        )
        self.assertEqual(self.client.post("/devices/uppal-x990/retire").status_code, 400)
        self.assertEqual(set(self.database.devices_by_id), before)
        self.assertIsNone(self.database.device_by_id("uppal-x990")["retired_at"])

    def test_hr_cannot_add_or_retire(self) -> None:
        self.client.post("/signout")
        self.database.upsert_employee(device_user_id="900", name="Priya HR", email="priya@example.com")
        priya = self.database.employee_by_email("priya@example.com")
        self.database.set_employee_role(priya["id"], "hr")
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "priya@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        response = self.client.post("/devices/add", data={
            "csrf": "x", "site": "X", "family": "x990", "ip": "10.0.0.1", "port": "4370"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/devices/uppal-x990/retire", data={"csrf": "x"})
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.database.device_by_id("uppal-x990")["retired_at"])


# A four-door C3 as the Pre-implementation Check read it.
_C3_PARAMS = {
    "LockCount": 4, "ReaderCount": 4, "AntiPassback": 0, "InterLock": 0,
    **{
        f"Door{door}{name}": value
        for door in range(1, 5)
        for name, value in (
            ("Intertime", 1), ("Drivertime", 5), ("Detectortime", 15),
            ("CloseAndLock", 1), ("SensorType", 0), ("VerifyType", 7),
        )
    },
}
_C3 = "gachibowli-c3-400"
_X990 = "uppal-x990"


class _DeviceSettingsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.admin = self._person("1", "Admin", "admin@example.com", "admin")
        self.client = self.app.test_client()
        self._sign_in("admin@example.com")

    def _person(self, uid: str, name: str, email: str, role: str) -> dict:
        self.database.upsert_employee(device_user_id=uid, name=name, email=email)
        person = self.database.employee_by_email(email)
        self.database.set_employee_role(person["id"], role)
        return person

    def _sign_in(self, email: str) -> None:
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": email})
        self.client.post("/signin/verify", data={"code": captured["code"]})

    def _sign_in_as(self, role: str) -> None:
        self.client.post("/signout")
        self._person("900", f"Priya {role}", f"{role}@example.com", role)
        self._sign_in(f"{role}@example.com")

    def _csrf(self) -> str:
        html = self.client.get("/settings").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _card(self, device_id: str, html: str | None = None) -> str:
        html = html if html is not None else self.client.get("/devices").text
        start = html.index(f'aria-labelledby="device-{device_id}-name"')
        end = html.find('<section class="device"', start)
        return html[start : end if end != -1 else len(html)]

    def _read_c3(self, **overrides) -> dict:
        device = self.database.device_by_id(_C3)
        device["door_params"] = {**_C3_PARAMS, **overrides}
        device["door_params_read_at"] = _ist_now() - timedelta(minutes=30)
        return device


class DevicePolicyTest(_DeviceSettingsBase):
    """U5: per-device quick-swipe window and, on a C3, Wait for door close."""

    def _policy(self, device_id: str, **data):
        data.setdefault("csrf", self._csrf())
        return self.client.post(f"/devices/{device_id}/policy", data=data)

    def test_admin_saves_window_with_one_audit_row(self) -> None:
        response = self._policy(_X990, window_seconds="45")
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"policy={_X990}", response.headers["Location"])
        device = self.database.device_by_id(_X990)
        self.assertEqual(device["duplicate_window_seconds"], 45)
        self.assertIsNotNone(device["policy_updated_at"])
        rows = self.database.device_change_rows
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["actor_employee_id"]), ("policy", self.admin["id"]))
        self.assertEqual(rows[0]["new_value"]["duplicate_window_seconds"], 45)
        page = self.client.get(f"/devices?policy={_X990}").text
        self.assertIn("Swipe policy saved", self._card(_X990, page))

    def test_x990_floor_and_max_are_accepted_and_below_floor_is_refused(self) -> None:
        for raw, expected in (("30", 30), (" 300 ", 300)):
            with self.subTest(raw=raw):
                self.assertEqual(self._policy(_X990, window_seconds=raw).status_code, 302)
                self.assertEqual(self.database.device_by_id(_X990)["duplicate_window_seconds"], expected)
        for raw in ("0", "2", "29"):
            with self.subTest(refused=raw):
                response = self._policy(_X990, window_seconds=raw)
                self.assertEqual(response.status_code, 400)
                self.assertIn("Enter whole seconds from 30 to 300.", self._card(_X990, response.text))
                self.assertEqual(self.database.device_by_id(_X990)["duplicate_window_seconds"], 300)

    def test_c3_window_zero_is_accepted(self) -> None:
        # The C3 records its directions, so no re-swipe can flip them.
        self.assertEqual(self._policy(_C3, window_seconds="0").status_code, 302)
        self.assertEqual(self.database.device_by_id(_C3)["duplicate_window_seconds"], 0)

    def test_x990_card_advises_a_long_window_and_offers_no_short_preset(self) -> None:
        card = self._card(_X990, self.client.get("/devices").text)
        self.assertIn('min="30"', card)
        self.assertIn("flips", card)
        self.assertNotIn('data-window-preset="5"', card)
        self.assertNotIn('data-window-preset="0"', card)

    def test_invalid_window_rerenders_with_error_and_stores_nothing(self) -> None:
        for raw in ("abc", "1.5", "400", "-1", "", "10s"):
            with self.subTest(raw=raw):
                response = self._policy(_X990, window_seconds=raw)
                self.assertEqual(response.status_code, 400)
                self.assertIn("Enter whole seconds from 30 to 300.", self._card(_X990, response.text))
                self.assertEqual(self.database.device_by_id(_X990)["duplicate_window_seconds"], 60)
        self.assertEqual(self.database.device_change_rows, [])

    def test_hr_and_manager_are_forbidden(self) -> None:
        for role in ("hr", "manager"):
            with self.subTest(role=role):
                self._sign_in_as(role)
                response = self.client.post(
                    f"/devices/{_X990}/policy", data={"csrf": "x", "window_seconds": "45"})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database.device_by_id(_X990)["duplicate_window_seconds"], 60)

    def test_missing_csrf_is_rejected(self) -> None:
        response = self.client.post(f"/devices/{_X990}/policy", data={"window_seconds": "45"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.database.device_change_rows, [])

    def test_unknown_device_is_404(self) -> None:
        self.assertEqual(self._policy("ghost", window_seconds="10").status_code, 404)

    def test_only_the_c3_card_has_the_door_close_checkbox(self) -> None:
        page = self.client.get("/devices").text
        self.assertIn("Swipe policy", self._card(_X990, page))
        self.assertNotIn("0 = count every swipe", self._card(_X990, page))
        self.assertIn("0 = count every swipe", self._card(_C3, page))
        self.assertNotIn('name="wait_for_door_close"', self._card(_X990, page))
        self.assertIn('name="wait_for_door_close"', self._card(_C3, page))
        self.assertIn("Recommended for this C3: 10 s.", self._card(_C3, page))
        self.assertIn("Recommended for the X990: 60 s (minimum 30 s).", self._card(_X990, page))

    def test_x990_post_never_starts_door_denials(self) -> None:
        self._policy(_X990, window_seconds="45")  # no checkbox posted
        self.assertIsNone(self.database.device_by_id(_X990)["door_denied_since"])

    def test_unchecking_door_close_starts_denials_and_rechecking_clears(self) -> None:
        self.assertIn(" checked", self._card(_C3).split('name="wait_for_door_close"')[1][:120])
        self._policy(_C3, window_seconds="10")
        since = self.database.device_by_id(_C3)["door_denied_since"]
        self.assertIsNotNone(since)
        card = self._card(_C3)
        self.assertIn("Counting door denials since", card)
        self.assertNotIn(" checked", card.split('name="wait_for_door_close"')[1][:120])
        self._policy(_C3, window_seconds="10", wait_for_door_close="1")
        self.assertIsNone(self.database.device_by_id(_C3)["door_denied_since"])
        self.assertEqual(len(self.database.device_change_rows), 2)

    def test_door_close_note_follows_the_last_read(self) -> None:
        self.assertNotIn("Last read:", self._card(_C3))
        self._read_c3()
        self.assertIn("Last read: no door sensor, InterLock 0, AntiPassback 0.", self._card(_C3))
        self._read_c3(Door2SensorType=2, InterLock=1)
        self.assertIn("door sensor on door 2, InterLock Doors 1 and 2", self._card(_C3))


class DeviceDoorSettingsTest(_DeviceSettingsBase):
    """U8: reading, changing, restoring and dismissing C3 door parameters."""

    def _door(self, path: str = "", device_id: str = _C3, **data):
        data.setdefault("csrf", self._csrf())
        return self.client.post(f"/devices/{device_id}/door{path}", data=data)

    def _form(self, **changes) -> dict:
        # What the browser posts: every writable key at its current value.
        from essl_hub.door_params import writable_keys

        form = {key: str(_C3_PARAMS[key]) for key in writable_keys(4)}
        form.update(changes)
        return form

    def test_x990_card_has_no_door_section_and_door_routes_404(self) -> None:
        self.assertNotIn("Door settings", self._card(_X990))
        self.assertIn("Door settings", self._card(_C3))
        for path in ("", "/read", "/restore", "/dismiss"):
            with self.subTest(path=path):
                self.assertEqual(self._door(path, device_id=_X990).status_code, 404)
                self.assertEqual(self._door(path, device_id="ghost").status_code, 404)
        self.assertEqual(self.database.device_change_rows, [])

    def test_before_any_read_form_is_hidden_and_read_is_offered(self) -> None:
        card = self._card(_C3)
        self.assertIn("Not read yet. Use Read from device.", card)
        self.assertNotIn("Change door settings", card)
        self.assertIn(f"/devices/{_C3}/door/read", card)
        response = self._door("", **self._form(Door1Intertime="0"))
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.database.device_by_id(_C3)["door_params_request"])

    def test_read_from_device_files_an_empty_request(self) -> None:
        response = self._door("/read")
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"door={_C3}", response.headers["Location"])
        device = self.database.device_by_id(_C3)
        self.assertEqual(device["door_params_request"], {})
        self.assertEqual(self.database.device_change_rows[-1]["kind"], "door_read")
        card = self._card(_C3)
        self.assertIn("Pending", card)
        self.assertIn("applied on the device's next poll (about 1 min)", card)

    def test_values_show_labels_per_door(self) -> None:
        self._read_c3()
        card = self._card(_C3)
        for text in ("Door 1", "Door 4", "Punch interval", "Lock when door closes",
                     "Verify mode (read only)", "Panel-wide", "Anti-passback", "Busy door: 0"):
            self.assertIn(text, card)
        self.assertIn("Change door settings", card)
        self.assertNotIn('name="Door1VerifyType"', card)
        self.assertNotIn('name="LockCount"', card)

    def test_change_confirms_then_files_with_audit(self) -> None:
        self._read_c3()
        response = self._door("", **self._form(Door1Intertime="0"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Punch interval, door 1: 1 → 0", response.text)
        self.assertEqual(response.text.count("→"), 1)
        self.assertIsNone(self.database.device_by_id(_C3)["door_params_request"])
        self.assertEqual(self.database.device_change_rows, [])

        response = self._door("", Door1Intertime="0", confirm="1")
        self.assertEqual(response.status_code, 302)
        device = self.database.device_by_id(_C3)
        self.assertEqual(device["door_params_request"], {"Door1Intertime": 0})
        (row,) = self.database.device_change_rows
        self.assertEqual((row["kind"], row["actor_employee_id"]), ("door_write", self.admin["id"]))
        self.assertEqual((row["old_value"], row["new_value"]), ({"Door1Intertime": 1}, {"Door1Intertime": 0}))
        card = self._card(_C3)
        self.assertIn("Pending", card)
        self.assertIn(f"Requested {device['door_params_requested_at'].strftime('%H:%M')}", card)
        self.assertNotIn("Change door settings", card)

    def test_door_confirm_page_keeps_devices_current(self) -> None:
        self._read_c3()
        response = self._door("", **self._form(Door1Intertime="0"))
        self.assertIn("Punch interval, door 1: 1 → 0", response.text)
        self.assertEqual(_current_nav(response.text), ["Devices"])

    def test_select_change_shows_option_labels(self) -> None:
        self._read_c3()
        response = self._door("", **self._form(Door2CloseAndLock="0"))
        self.assertIn("Lock when door closes, door 2: On → Off", response.text)

    def test_nothing_changed_files_nothing(self) -> None:
        self._read_c3()
        response = self._door("", **self._form())
        self.assertEqual(response.status_code, 200)
        self.assertIn("Nothing changed.", self._card(_C3, response.text))
        self.assertIsNone(self.database.device_by_id(_C3)["door_params_request"])

    def test_forbidden_keys_and_smuggled_values_are_400(self) -> None:
        self._read_c3()
        cases = [
            {"ComPwd": "1"},
            {"Door1Intertime": "0,ComPwd=1"},
            {"Door1VerifyType": "4"},
            {"IPAddress": "1"},
            {"Door5Intertime": "0"},
            {"Door1Intertime": "11"},
            {"Door1Intertime": "1.5"},
        ]
        for extra in cases:
            for confirm in ({}, {"confirm": "1"}):
                with self.subTest(extra=extra, confirm=confirm):
                    response = self._door("", **self._form(**extra), **confirm)
                    self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.database.device_by_id(_C3)["door_params_request"])
        self.assertEqual(self.database.device_change_rows, [])

    def test_restore_without_snapshot_is_disabled_and_400(self) -> None:
        self._read_c3()
        card = self._card(_C3)
        button = card.split("/door/restore")[1].split("</form>")[0]
        self.assertIn("disabled", button)
        self.assertEqual(self._door("/restore").status_code, 400)
        self.assertEqual(self._door("/restore", confirm="1").status_code, 400)
        self.assertIsNone(self.database.device_by_id(_C3)["door_params_request"])

    def test_restore_lists_only_differing_whitelisted_keys(self) -> None:
        device = self._read_c3(Door1Intertime=0, Door2Drivertime=5)
        device["door_params_previous"] = {
            **_C3_PARAMS, "Door2Drivertime": 8, "Door1VerifyType": 4, "LockCount": 2,
            "ComPwd": 5,
        }
        device["door_params_previous_for"] = _ist_now() - timedelta(hours=1)
        card = self._card(_C3)
        self.assertNotIn("disabled", card.split("/door/restore")[1].split("</form>")[0])
        response = self._door("/restore")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Punch interval, door 1: 0 → 1", response.text)
        self.assertIn("Unlock time, door 2: 5 → 8", response.text)
        self.assertEqual(response.text.count("→"), 2)
        self.assertIsNone(device["door_params_request"])

        response = self._door("/restore", confirm="1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(device["door_params_request"], {"Door1Intertime": 1, "Door2Drivertime": 8})
        self.assertEqual(self.database.device_change_rows[-1]["kind"], "door_restore")

    def test_failed_shows_per_key_messages_and_dismiss_clears(self) -> None:
        device = self._read_c3()
        device["door_params_result"] = {
            "state": "failed", "requested_at": "2026-10-26 10:00:00",
            "finished_at": "2026-10-26 10:00:41", "message": "",
            "keys": {
                "Door1Intertime": {"outcome": "failed", "old": 1, "new": 0, "now": 1,
                                   "message": "panel kept 1"},
                "Door2Intertime": {"outcome": "not_attempted", "old": 1, "new": 0,
                                   "now": None, "message": ""},
            },
        }
        card = self._card(_C3)
        self.assertIn("Failed", card)
        self.assertIn("Punch interval, door 1: 1 → 0", card)
        self.assertIn("panel kept 1", card)
        self.assertIn("Not attempted", card)
        self.assertIn(f"/devices/{_C3}/door/dismiss", card)

        response = self._door("/dismiss")
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(device["door_params_result"])
        self.assertIsNone(device["door_params_request"])
        (row,) = self.database.device_change_rows
        self.assertEqual(row["kind"], "door_dismiss")
        self.assertNotIn("panel kept 1", self._card(_C3))

    def test_pending_past_two_polls_says_collector_has_not_picked_it_up(self) -> None:
        device = self._read_c3()
        device["door_params_request"] = {"Door1Intertime": 0}
        device["door_params_requested_at"] = _ist_now()
        self.assertNotIn("Collector has not picked this up.", self._card(_C3))
        device["door_params_requested_at"] = _ist_now() - timedelta(minutes=3)
        card = self._card(_C3)
        self.assertIn("Collector has not picked this up.", card)
        self.assertIn("Cancel request", card)

    def test_a_pending_request_blocks_another(self) -> None:
        device = self._read_c3()
        device["door_params_request"] = {}
        device["door_params_requested_at"] = _ist_now()
        self.assertEqual(self._door("/read").status_code, 409)
        self.assertEqual(self._door("", **self._form(Door1Intertime="0"), confirm="1").status_code, 409)
        self.assertEqual(device["door_params_request"], {})

    def test_hr_is_forbidden_on_every_door_route(self) -> None:
        self._read_c3()
        self._sign_in_as("hr")
        for path in ("", "/read", "/restore", "/dismiss"):
            with self.subTest(path=path):
                response = self.client.post(f"/devices/{_C3}/door{path}", data={"csrf": "x"})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database.device_change_rows, [])

    def test_missing_csrf_is_400_on_every_door_route(self) -> None:
        self._read_c3()
        for path in ("", "/read", "/restore", "/dismiss"):
            with self.subTest(path=path):
                response = self.client.post(
                    f"/devices/{_C3}/door{path}", data={"Door1Intertime": "0", "confirm": "1"})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.database.device_change_rows, [])


class DoorViewStateTest(unittest.TestCase):
    """The card-state helper behind U8, one case per diagram state."""

    def _view(self, **device):
        from essl_hub.routes.admin import _door_view

        base = {"protocol": "c3-pullsdk", **_V7_DEVICE_DEFAULTS}
        return _door_view({**base, **device}, datetime(2026, 10, 26, 10, 10), 60)

    def test_states(self) -> None:
        requested = datetime(2026, 10, 26, 10, 0)
        self.assertIsNone(self._view(protocol="zk-attendance"))
        self.assertEqual(self._view()["state"], "not_read")
        self.assertFalse(self._view()["can_change"])
        pending = self._view(door_params_request={}, door_params_requested_at=requested)
        self.assertEqual((pending["state"], pending["stale"]), ("pending", True))
        self.assertEqual(pending["expires_at"], datetime(2026, 10, 26, 10, 15))
        read = self._view(door_params=_C3_PARAMS, door_params_result={"state": "read", "keys": {}})
        self.assertEqual((read["state"], read["can_change"], len(read["doors"])), ("applied", True, 4))
        for state in ("failed", "expired"):
            view = self._view(door_params=_C3_PARAMS, door_params_result={"state": state, "keys": {}})
            self.assertEqual(view["state"], state)
        pending_after_read = self._view(
            door_params=_C3_PARAMS, door_params_requested_at=requested,
            door_params_request={"Door1Intertime": 0})
        self.assertFalse(pending_after_read["can_change"])


class AdminRosterUploadTest(unittest.TestCase):
    """Covers R1, R2, R18: .xlsx roster uploads and every per-file failure."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="1", name="Admin", email="admin@example.com")
        admin = self.database.employee_by_email("admin@example.com")
        self.database.set_employee_role(admin["id"], "admin")
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "admin@example.com"})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/settings").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _upload(self, data: bytes, filename: str):
        return self.client.post(
            "/people/import",
            data={"roster": (io.BytesIO(data), filename), "csrf": self._csrf()},
            content_type="multipart/form-data",
        )

    def _xlsx_bytes(self, rows) -> bytes:
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        for row in rows:
            sheet.append(list(row))
        buffer = io.BytesIO()
        workbook.save(buffer)
        workbook.close()
        return buffer.getvalue()

    def test_valid_xlsx_upload_creates_employees(self) -> None:
        payload = self._xlsx_bytes(
            [
                ["name", "work email", "device user id"],
                ["Karthik", "karthik@example.com", 184],
                ["Vijay", "vijay@example.com", 55],
            ]
        )
        response = self._upload(payload, "roster.xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<strong>2</strong> added", response.text)
        self.assertIsNotNone(self.database.employee_by_email("karthik@example.com"))
        self.assertIsNotNone(self.database.employee_by_email("vijay@example.com"))

    def test_xls_extension_is_rejected_before_parsing(self) -> None:
        response = self._upload(b"\xd0\xcf\x11\xe0not-really-xls", "roster.xls")
        self.assertEqual(response.status_code, 400)
        self.assertIn(".xlsx", response.text)

    def test_unknown_extension_is_rejected_before_parsing(self) -> None:
        response = self._upload(b"name,email\nA,a@x.com\n", "roster.txt")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported file type", response.text)

    def test_binary_csv_upload_renders_roster_error_not_a_server_error(self) -> None:
        # Covers R2: a non-UTF-8 .csv must not become a 500.
        response = self._upload(b"\xff\xfe\x00\x01binary,payload\n", "roster.csv")
        self.assertEqual(response.status_code, 400)
        self.assertIn("not UTF-8 text", response.text)
        self.assertNotIn("Internal Server Error", response.text)

    def test_malformed_xlsx_upload_renders_roster_error(self) -> None:
        response = self._upload(b"PK\x03\x04 broken workbook", "roster.xlsx")
        self.assertEqual(response.status_code, 400)
        self.assertIn("xlsx", response.text)

    def test_upload_over_the_size_cap_renders_the_import_page_with_an_error(self) -> None:
        # Covers R2: the 5 MB cap must re-render the admin page, not a bare 413.
        oversized = b"a" * (6 * 1024 * 1024)
        response = self._upload(oversized, "roster.csv")
        self.assertEqual(response.status_code, 413)
        self.assertIn("5 MB", response.text)
        self.assertIn("Import roster", response.text)

    def test_anonymous_oversized_upload_keeps_the_bare_413_page(self) -> None:
        # Deliberate deviation from the U5 packet: the 413 handler must not
        # render the admin page (device IPs/ports, API-key prefixes, unmapped
        # IDs) to a client that never signed in.
        anonymous = self.app.test_client()
        oversized = b"a" * (6 * 1024 * 1024)
        response = anonymous.post(
            "/signin",
            data={"email": (io.BytesIO(oversized), "roster.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 413)
        self.assertNotIn("Devices, roster and access", response.text)
        self.assertNotIn("Edit connection settings", response.text)
        self.assertNotIn("API keys", response.text)


class ApiHoursTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.database.upsert_employee(device_user_id="118", name="Ravi Kumar", email="ravi.kumar@example.com")
        self.client = self.app.test_client()

    def _csrf(self) -> str:
        html = self.client.get("/settings").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _create_key(self) -> str:
        from essl_hub import web as web_module

        with self.app.test_request_context():
            pass
        # Exercise the real admin flow to create a key, signed in as admin.
        self.database.upsert_employee(device_user_id="1", name="Admin", email="admin@example.com")
        admin = self.database.employee_by_email("admin@example.com")
        self.database.set_employee_role(admin["id"], "admin")
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "admin@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        response = self.client.post("/settings/keys", data={"label": "HRMS", "csrf": self._csrf()})
        # Parse the reveal block specifically: the page holds other <code>
        # elements (the roster example row), so 'first <code>' is not the key.
        block = response.text.split('<div class="key-reveal">')[1]
        start = block.index("<code>") + len("<code>")
        end = block.index("</code>", start)
        return block[start:end].strip()

    def test_missing_bearer_header_is_refused(self) -> None:
        response = self.client.get("/api/v1/hours?from=2026-09-01&to=2026-09-01")
        self.assertEqual(response.status_code, 401)

    def test_covers_ae6_revoked_key_is_refused_second_key_still_works(self) -> None:
        # Covers AE6.
        key = self._create_key()
        self.client.post("/signout")
        response = self.client.get(
            "/api/v1/hours?from=2026-09-01&to=2026-09-01",
            headers={"Authorization": f"Bearer {key}"},
        )
        self.assertEqual(response.status_code, 200)

        prefix = key[:12]
        # revoke as admin again
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.client.post("/signin", data={"email": "admin@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        self.client.post(f"/settings/keys/{prefix}/revoke", data={"csrf": self._csrf()})
        self.client.post("/signout")

        revoked_response = self.client.get(
            "/api/v1/hours?from=2026-09-01&to=2026-09-01",
            headers={"Authorization": f"Bearer {key}"},
        )
        self.assertEqual(revoked_response.status_code, 401)

    def test_hours_response_carries_day_start_and_uses_the_work_day(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        day = datetime(2026, 9, 1)
        self.database.insert_punches([
            _punch_row("fp-a1", day + timedelta(hours=22, minutes=10), "entry"),
            _punch_row("fp-a2", day + timedelta(days=1, hours=3, minutes=40), "exit"),
        ])
        response = self.client.get(
            "/api/v1/hours?from=2026-09-01&to=2026-09-02",
            headers={"Authorization": f"Bearer {key}"},
        )
        body = response.get_json()
        self.assertEqual(body["day_start"], "06:00")
        ravi = {r["date"]: r for r in body["results"] if r["device_user_id"] == "118"}
        self.assertEqual(ravi["2026-09-01"]["effective_minutes"], 330)
        self.assertEqual(ravi["2026-09-02"]["status"], "empty")

    def test_hours_reads_day_start_setting_once_per_request(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        original = self.database.setting
        calls = []

        def counting(name, *args, **kwargs):
            calls.append(name)
            return original(name, *args, **kwargs)

        with patch.object(self.database, "setting", side_effect=counting):
            response = self.client.get(
                "/api/v1/hours?from=2026-09-01&to=2026-09-07",
                headers={"Authorization": f"Bearer {key}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.get_json()["results"]), 7)
        self.assertEqual(calls.count("day_start_hour"), 1)

    def test_hours_response_carries_anomalies(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        day = datetime(2026, 9, 1)
        self.database.insert_punches([
            _punch_row("fp-o1", day + timedelta(hours=8), "exit"),
            _punch_row("fp-o2", day + timedelta(hours=9), "entry"),
            _punch_row("fp-o3", day + timedelta(hours=17), "exit"),
        ])
        response = self.client.get(
            "/api/v1/hours?from=2026-09-01&to=2026-09-01",
            headers={"Authorization": f"Bearer {key}"},
        )
        ravi = [r for r in response.get_json()["results"] if r["device_user_id"] == "118"][0]
        self.assertEqual(ravi["effective_minutes"], 480)
        self.assertEqual(ravi["anomalies"], ["Exit at 08:00 with no entry"])

    def test_hours_response_adds_exact_effective_seconds(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        day = datetime(2026, 9, 1)
        self.database.insert_punches([
            _punch_row("fp-x1", day + timedelta(hours=9, seconds=10), "entry"),
            _punch_row("fp-x2", day + timedelta(hours=16, minutes=59, seconds=40), "exit"),
        ])
        response = self.client.get(
            "/api/v1/hours?from=2026-09-01&to=2026-09-01",
            headers={"Authorization": f"Bearer {key}"},
        )
        ravi = [r for r in response.get_json()["results"] if r["device_user_id"] == "118"][0]
        self.assertEqual(ravi["effective_seconds"], 7 * 3600 + 59 * 60 + 30)
        self.assertEqual(ravi["effective_minutes"], 479)
        self.assertEqual(ravi["status"], "short")

    def test_range_longer_than_cap_is_refused(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        response = self.client.get(
            "/api/v1/hours?from=2026-01-01&to=2026-12-31",
            headers={"Authorization": f"Bearer {key}"},
        )
        self.assertEqual(response.status_code, 400)

    def test_no_route_accepts_a_write_method(self) -> None:
        key = self._create_key()
        self.client.post("/signout")
        response = self.client.delete(
            "/api/v1/hours?from=2026-09-01&to=2026-09-01",
            headers={"Authorization": f"Bearer {key}"},
        )
        self.assertEqual(response.status_code, 405)


class SetupPageTest(unittest.TestCase):
    TOKEN = "setup-token-1234567890"

    def _make_app_with(self, database, token):
        environment = {"ESSL_SETUP_TOKEN": token} if token else {}
        with patch.dict(os.environ, environment, clear=True):
            app = create_app(database=database)
        app.testing = True
        return app

    @staticmethod
    def _seed(database):
        database.upsert_employee(device_user_id="1", name="Admin", email="admin@example.com")
        admin = database.employee_by_email("admin@example.com")
        database.set_employee_role(admin["id"], "admin")

    def test_turnstile_failure_blocks_setup_even_with_right_token(self) -> None:
        database = FakeDatabase()
        environment = {
            "ESSL_SETUP_TOKEN": self.TOKEN,
            "TURNSTILE_SITE_KEY": "s",
            "TURNSTILE_SECRET_KEY": "k",
        }
        with patch.dict(os.environ, environment, clear=True):
            app = create_app(database=database)
        app.testing = True
        with patch("essl_hub.turnstile.verify", return_value=False):
            response = app.test_client().post(
                "/setup",
                data={"setup_token": self.TOKEN, "name": "Boss", "email": "boss@example.com"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(database.has_admin())

    def test_route_is_absent_without_a_setup_token(self) -> None:
        app = self._make_app_with(FakeDatabase(), None)
        self.assertEqual(app.test_client().get("/setup").status_code, 404)

    def test_route_is_absent_once_an_admin_exists(self) -> None:
        database = FakeDatabase()
        self._seed(database)
        app = self._make_app_with(database, self.TOKEN)
        client = app.test_client()
        self.assertEqual(client.get("/setup").status_code, 404)
        self.assertEqual(
            client.post(
                "/setup",
                data={
                    "setup_token": self.TOKEN,
                    "name": "Boss",
                    "email": "boss@example.com",
                },
            ).status_code,
            404,
        )

    def test_get_renders_the_form_on_an_empty_database(self) -> None:
        app = self._make_app_with(FakeDatabase(), self.TOKEN)
        response = app.test_client().get("/setup")
        self.assertEqual(response.status_code, 200)
        self.assertIn("First-time setup", response.text)
        _assert_auth_layout(self, response.text, "First-time setup")
        self.assertIn('name="setup_token"', response.text)

    def test_wrong_token_is_refused_and_creates_nobody(self) -> None:
        database = FakeDatabase()
        app = self._make_app_with(database, self.TOKEN)
        response = app.test_client().post(
            "/setup",
            data={"setup_token": "not-the-token", "name": "Boss", "email": "boss@example.com"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(database.employee_by_email("boss@example.com"))

    def test_missing_name_is_reported_without_a_write(self) -> None:
        database = FakeDatabase()
        app = self._make_app_with(database, self.TOKEN)
        response = app.test_client().post(
            "/setup",
            data={"setup_token": self.TOKEN, "name": "", "email": "boss@example.com"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Enter your name", response.text)
        self.assertIsNone(database.employee_by_email("boss@example.com"))

    def test_zero_device_id_is_rejected_on_setup(self) -> None:
        database = FakeDatabase()
        app = self._make_app_with(database, self.TOKEN)
        response = app.test_client().post(
            "/setup",
            data={"setup_token": self.TOKEN, "name": "Boss", "email": "boss@example.com",
                  "device_user_id": "NP0000"},
        )
        self.assertIn("Device ID can&#39;t be zero", response.text)
        self.assertFalse(database.has_admin())

    def test_device_id_clash_is_reported(self) -> None:
        database = FakeDatabase()
        database.upsert_employee(device_user_id="5", name="Other", email="other@example.com")
        app = self._make_app_with(database, self.TOKEN)
        response = app.test_client().post(
            "/setup",
            data={
                "setup_token": self.TOKEN,
                "name": "Boss",
                "email": "boss@example.com",
                "device_user_id": "5",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("already belongs to", response.text)
        self.assertIsNone(database.employee_by_email("boss@example.com"))

    def test_creates_the_first_admin_then_closes_itself(self) -> None:
        database = FakeDatabase()
        app = self._make_app_with(database, self.TOKEN)
        client = app.test_client()
        response = client.post(
            "/setup",
            data={"setup_token": self.TOKEN, "name": "Boss", "email": "boss@example.com"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/signin", response.headers["Location"])
        row = database.employee_by_email("boss@example.com")
        self.assertEqual(row["role"], "admin")
        self.assertEqual(row["device_user_id"], "boss@example.com")
        self.assertEqual(client.get("/setup").status_code, 404)

    def test_promotes_an_existing_employee_without_duplicating(self) -> None:
        database = FakeDatabase()
        database.upsert_employee(device_user_id="7", name="Boss", email="boss@example.com")
        app = self._make_app_with(database, self.TOKEN)
        app.test_client().post(
            "/setup",
            data={"setup_token": self.TOKEN, "name": "Boss", "email": "BOSS@example.com"},
        )
        rows = database.employees()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "admin")
        self.assertEqual(rows[0]["device_user_id"], "7")

    def test_the_created_admin_can_sign_in_and_reach_the_console(self) -> None:
        database = FakeDatabase()
        app = self._make_app_with(database, self.TOKEN)
        client = app.test_client()
        client.post(
            "/setup",
            data={"setup_token": self.TOKEN, "name": "Boss", "email": "boss@example.com"},
        )

        captured: dict = {}
        from essl_hub import mail as mail_module

        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        client.post("/signin", data={"email": "boss@example.com"})
        client.post("/signin/verify", data={"code": captured["code"]})

        self.assertEqual(client.get("/settings").status_code, 200)


class BackupsRemovedTest(unittest.TestCase):
    """U4: manual backups, the replication sidecar and restore tooling are gone."""

    def setUp(self) -> None:
        from essl_hub import mail as mail_module

        self.app, self.database = _make_app()
        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _client_as(self, role: str):
        client = self.app.test_client()
        email = f"{role}@example.com"
        self.database.upsert_employee(device_user_id=f"9{len(role)}", name=role.title(), email=email)
        person = self.database.employee_by_email(email)
        self.database.set_employee_role(person["id"], role)
        client.post("/signin", data={"email": email})
        client.post("/signin/verify", data={"code": self.captured["code"]})
        return client

    def test_backups_route_is_404_for_admin_and_hr(self) -> None:
        for role in ("admin", "hr"):
            with self.subTest(role=role):
                client = self._client_as(role)
                self.assertEqual(client.get("/backups").status_code, 404)
                self.assertEqual(client.post("/backups", data={}).status_code, 404)

    def test_no_nav_mentions_backups(self) -> None:
        for role in ("employee", "manager", "hr", "admin"):
            with self.subTest(role=role):
                html = self._client_as(role).get("/week").text
                self.assertNotIn("Backups", html)
                self.assertNotIn("backup-badge", html)

    def test_backup_modules_are_gone(self) -> None:
        import importlib

        # essl_hub.backup returned as the R2 snapshot service (2026-10-14 plan U2/U3).
        for name in ("essl_hub.restore_tool",):
            with self.subTest(module=name):
                with self.assertRaises(ModuleNotFoundError):
                    importlib.import_module(name)


class AppShellTest(unittest.TestCase):
    """U10: CSRF via header for htmx, and the role-aware sidebar."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _client_as(self, role: str):
        email = f"{role}@example.com"
        self.database.upsert_employee(device_user_id=role, name=role.title(), email=email)
        self.database.set_employee_role(self.database.employee_by_email(email)["id"], role)
        client = self.app.test_client()
        # A distinct address per role keeps the per-IP sign-in burst limit out of the way.
        client.environ_base["REMOTE_ADDR"] = f"10.0.{len(role)}.{sum(map(ord, role)) % 250 + 1}"
        client.post("/signin", data={"email": email})
        client.post("/signin/verify", data={"code": self.captured["code"]})
        return client

    @staticmethod
    def _csrf_meta(html: str) -> str:
        marker = '<meta name="csrf-token" content="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    @staticmethod
    def _sidebar_labels(html: str) -> list[str]:
        start = html.index('<nav aria-label="Main">')
        nav = html[start : html.index("</nav>", start)]
        return re.findall(r"<span>([^<]+)</span>(?:<span class=\"nav-count\"[^>]*>[^<]*</span>)?</a>", nav)

    def test_header_only_csrf_post_passes(self) -> None:
        client = self._client_as("admin")
        token = self._csrf_meta(client.get("/week").text)
        self.assertTrue(token)
        response = client.post(
            "/settings/target",
            data={"hours": "7", "minutes": "15"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.database.setting("daily_target_minutes"), "435")

    def test_post_without_header_or_field_is_rejected(self) -> None:
        client = self._client_as("admin")
        before = self.database.setting("daily_target_minutes")
        response = client.post("/settings/target", data={"hours": "7", "minutes": "15"})
        self.assertEqual(response.status_code, 400)
        response = client.post(
            "/settings/target",
            data={"hours": "7", "minutes": "15"},
            headers={"X-CSRF-Token": "bogus"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.database.setting("daily_target_minutes"), before)

    def test_shell_carries_csrf_for_htmx(self) -> None:
        html = self._client_as("employee").get("/week").text
        token = self._csrf_meta(html)
        self.assertIn(f'hx-headers=\'{{"X-CSRF-Token": "{token}"}}\'', html)
        self.assertIn('href="#main-content"', html)
        self.assertIn('<main class="main-content" id="main-content"', html)

    def test_sidebar_follows_navigation_by_role(self) -> None:
        # Entries whose pages do not exist yet (Team, Devices,
        # Settings) are left out; the admin console stands in for Settings.
        # The manager role arrives with U5.
        expected = {
            "employee": ["Today", "Week"],
            "hr": ["Today", "Week", "Employees", "People", "Fix requests"],
            "admin": ["Today", "Week", "Employees", "People", "Fix requests", "Devices", "Settings"],
        }
        for role, labels in expected.items():
            with self.subTest(role=role):
                html = self._client_as(role).get("/week").text
                self.assertEqual(self._sidebar_labels(html), labels)
                self.assertIn('aria-current="page"', html)


def _current_nav(html: str) -> list[str]:
    """Labels of the main sidebar entries marked aria-current="page"."""
    start = html.index('<nav aria-label="Main">')
    nav = html[start : html.index("</nav>", start)]
    return re.findall(r'<a class="nav-item" href="[^"]*" aria-current="page"[^>]*>.*?<span>([^<]+)</span>', nav, re.S)


class NavSectionTest(unittest.TestCase):
    """U4 (KTD7): the nav highlights the section, from the endpoint and whose
    page it is, on sub-pages and other people's days too."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _person(self, uid: str, role: str = "employee") -> dict:
        email = f"nav{uid}@example.com"
        self.database.upsert_employee(device_user_id=uid, name=f"N{uid}", email=email)
        person = self.database.employee_by_email(email)
        self.database.set_employee_role(person["id"], role)
        return person

    def _sign_in(self, person: dict) -> None:
        self.client.post("/signout")
        self.client.post("/signin", data={"email": person["email"]})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/day").text
        marker = 'name="csrf-token" content="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _current(self, path: str) -> list[str]:
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return _current_nav(response.text)

    def test_rules_in_order(self) -> None:
        from essl_hub.web import nav_section

        cases = [
            ("people.id_issues_view", True, "id_issues"),
            ("people.id_issues_archive", True, "id_issues"),
            ("people.punch_archives_view", True, "id_issues"),
            ("people.punch_archive_restore", True, "id_issues"),
            ("people.people_archive_view", True, "people"),
            ("people.person_edit", False, "people"),  # an id in the path is not "someone else's day"
            ("me.week_view", True, "week"),
            ("me.day_view", True, "today"),
            ("me.quick_view", True, "today"),
            ("fixes.report_view", True, "today"),
            ("fixes.report_submit", True, "today"),
            ("me.day_view", False, "team"),
            ("me.week_view", False, "team"),
            ("me.quick_view", False, "team"),
            ("team.team_view", True, "team"),
            ("team.export_csv", True, "team"),
            ("fixes.review_list", True, "fix_requests"),
            ("fixes.review_detail", True, "fix_requests"),
            ("fixes.review_confirm", True, "fix_requests"),
            ("admin.devices_view", True, "devices"),
            ("admin.devices_door", True, "devices"),
            ("admin.settings_view", True, "settings"),
            ("admin.settings_create_key", True, "settings"),
            ("admin.health_view", True, "settings"),
            ("fixes.link_view", True, None),
            ("auth.signin", True, None),
            ("admin.admin_view", True, None),
            (None, True, None),
        ]
        for endpoint, own, section in cases:
            with self.subTest(endpoint=endpoint, own=own):
                self.assertEqual(nav_section(endpoint, own=own), section)

    def test_people_sub_pages_keep_people_current(self) -> None:
        hr = self._person("1", "hr")
        self._sign_in(hr)
        # The fake has no archived people; the page itself is covered in test_people_edit.
        self.database.archived_employees = lambda: []
        for path in ("/people", "/people/archive", "/people/new", f"/people/{hr['id']}/edit", "/people/import"):
            with self.subTest(path=path):
                self.assertEqual(self._current(path), ["People"])

    def test_id_issues_and_archive_list_are_the_id_issues_tab_not_directory(self) -> None:
        self._sign_in(self._person("1", "hr"))
        for path in ("/people/issues", "/people/issues/archived"):
            with self.subTest(path=path):
                html = self.client.get(path).text
                # ID issues has no sidebar entry; People hosts it as a sub-tab.
                self.assertEqual(_current_nav(html), ["People"])
                self.assertIn('href="/people/issues" aria-current="page">ID issues', html)
                self.assertNotIn('href="/people" aria-current="page">Directory', html)
                self.assertNotIn('href="/people/archive" aria-current="page">Archive', html)

    def test_manager_viewing_a_report_is_team_and_own_pages_are_today_and_week(self) -> None:
        boss = self._person("10", "manager")
        report = self._person("11")
        report["manager_id"] = boss["id"]
        self._sign_in(boss)
        self.assertEqual(self._current(f"/day?employee={report['id']}"), ["Team"])
        self.assertEqual(self._current(f"/week?employee={report['id']}"), ["Team"])
        self.assertEqual(self._current("/team"), ["Team"])
        self.assertEqual(self._current("/day"), ["Today"])
        self.assertEqual(self._current(f"/day?employee={boss['id']}"), ["Today"])
        self.assertEqual(self._current("/week"), ["Week"])

    def test_employee_own_day_and_week(self) -> None:
        self._sign_in(self._person("20"))
        self.assertEqual(self._current("/day"), ["Today"])
        self.assertEqual(self._current("/week"), ["Week"])

    def test_health_and_settings_re_render_are_settings(self) -> None:
        self._sign_in(self._person("30", "admin"))
        self.assertEqual(self._current("/settings"), ["Settings"])
        self.assertEqual(self._current("/health"), ["Settings"])
        self.assertEqual(self._current("/devices"), ["Devices"])
        response = self.client.post("/settings/keys", data={"label": "HRMS", "csrf": self._csrf()})
        self.assertEqual(response.status_code, 200)
        self.assertIn('<div class="key-reveal">', response.text)
        self.assertEqual(_current_nav(response.text), ["Settings"])


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class BrandingRemovedTest(unittest.TestCase):
    """U2: the admin sign-in branding feature is gone; stale settings are ignored."""

    setUp = AdminViewTest.setUp
    _csrf = AdminViewTest._csrf

    def test_settings_page_has_no_branding_section(self) -> None:
        html = self.client.get("/settings").text
        self.assertNotIn("Sign-in branding", html)
        self.assertNotIn("/settings/branding", html)

    def test_branding_routes_are_gone(self) -> None:
        csrf = self._csrf()
        for path in ("/settings/branding/logo", "/settings/branding/logo/reset", "/settings/branding-background"):
            with self.subTest(path=path):
                response = self.client.post(
                    path,
                    data={"image": (io.BytesIO(PNG_BYTES), "logo.png"), "mode": "color",
                          "color": "#112233", "csrf": csrf},
                    content_type="multipart/form-data",
                )
                self.assertIn(response.status_code, (404, 405))
        self.assertEqual(self.client.get("/brand/logo").status_code, 404)

    def test_stale_branding_settings_do_not_affect_signin(self) -> None:
        self.database.set_setting("signin_background", "image")
        self.database.set_setting("signin_background_color", "#112233")
        page = self.app.test_client().get("/signin")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("auth-bg", page.text)
        self.assertNotIn('"/brand/', page.text)
        self.assertNotIn("#112233", page.text)


class OfflineTest(unittest.TestCase):
    """Reads survive an unavailable database, writes get the 503 offline page."""

    # Signed in as admin, like AdminViewTest, without re-running its tests.
    setUp = AdminViewTest.setUp
    _csrf = AdminViewTest._csrf
    _add_employee = AdminViewTest._add_employee

    def test_write_while_offline_returns_503_offline_page(self) -> None:
        employee = self._add_employee()
        csrf = self._csrf()
        self.database.offline = True
        with self.assertLogs("essl_hub.web", level="WARNING"):
            response = self.client.post(
                f"/people/{employee['id']}/role", data={"role": "hr", "csrf": csrf}
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("Temporarily offline", response.text)
        self.assertEqual(self.database.employee_by_id(employee["id"])["role"], "employee")

    def test_read_while_offline_still_returns_200(self) -> None:
        self.database.offline = True
        self.assertEqual(self.client.get("/settings").status_code, 200)

    def test_signin_code_request_while_offline_shows_offline_page_and_sends_nothing(self) -> None:
        self.database.offline = True
        self.captured.clear()
        response = self.app.test_client().post("/signin", data={"email": "admin@example.com"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("Temporarily offline", response.text)
        _assert_auth_layout(self, response.text, "Temporarily offline")
        self.assertEqual(self.captured, {})


class StartupTest(unittest.TestCase):
    """KTD8: boot steps that fail on an unavailable database never block serving."""

    def test_offline_boot_with_bootstrap_admin_logs_and_serves(self) -> None:
        database = FakeDatabase()
        database.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
        database.offline = True
        with patch.dict(os.environ, {"ESSL_BOOTSTRAP_ADMIN_EMAIL": "boss@example.com"}):
            with self.assertLogs("essl_hub.web", level="WARNING") as logs:
                app = create_app(database=database)
        self.assertTrue(any("bootstrap admin" in line for line in logs.output))
        self.assertTrue(any("schema migration" in line for line in logs.output))
        self.assertEqual(database.employee_by_email("boss@example.com")["role"], "employee")
        self.assertEqual(app.test_client().get("/signin").status_code, 200)

    def test_startup_migrates_and_seeds(self) -> None:
        database = FakeDatabase()
        create_app(database=database)
        self.assertTrue(database.devices_by_id)


class DatabaseSetupTest(unittest.TestCase):
    """Local-first U1 / KTD5, KTD6: missing file serves setup page; busy is 503."""

    def setUp(self) -> None:
        from tempfile import TemporaryDirectory

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = tmp.name
        self.path = str(Path(tmp.name) / "essl.db")

    def _app(self):
        from essl_hub.db import Database

        app = create_app(database=Database(self.path, must_exist=True))
        app.testing = True
        return app

    def test_missing_database_serves_setup_page_and_creates_nothing(self) -> None:
        with self.assertLogs("essl_hub.web", level="WARNING"):
            app = self._app()
            client = app.test_client()
            response = client.get("/signin")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Database not set up", response.text)
        for command in ("init", "restore", "import-turso"):
            self.assertIn(f"python -m essl_hub.backup {command}", response.text)
        self.assertEqual(os.listdir(self.folder), [])

    def test_health_is_unhealthy_while_database_missing(self) -> None:
        with self.assertLogs("essl_hub.web", level="WARNING"):
            client = self._app().test_client()
            response = client.get("/api/health")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["ok"])

    def test_busy_boot_is_retried_on_next_request(self) -> None:
        from essl_hub.db import BusyError, Database

        creator = Database(self.path)
        creator.migrate_schema()
        creator.close()
        real = Database.initialize
        calls = []

        def flaky(db, devices):
            calls.append(1)
            if len(calls) == 1:
                raise BusyError("locked")
            return real(db, devices)

        with patch.object(Database, "initialize", flaky):
            with self.assertLogs("essl_hub.web", level="WARNING"):
                client = self._app().test_client()
            self.assertEqual(len(calls), 1)
            client.get("/api/health")
            self.assertEqual(len(calls), 2)
            client.get("/api/health")
            self.assertEqual(len(calls), 2)

    def test_first_request_after_init_boots_and_promotes_bootstrap_admin(self) -> None:
        from essl_hub.db import Database

        with patch.dict(os.environ, {"ESSL_BOOTSTRAP_ADMIN_EMAIL": "boss@example.com"}):
            with self.assertLogs("essl_hub.web", level="WARNING"):
                app = self._app()
            client = app.test_client()
            # A backup command creates the file while the web app is running.
            creator = Database(self.path)
            creator.migrate_schema()
            creator.upsert_employee(device_user_id="1", name="Boss", email="boss@example.com")
            creator.close()
            self.assertEqual(client.get("/api/health").status_code, 200)
            self.assertEqual(client.get("/signin").status_code, 200)
        database = app.extensions["essl_database"]
        self.assertEqual(database.employee_by_email("boss@example.com")["role"], "admin")

    def test_busy_database_returns_503_try_again(self) -> None:
        from essl_hub.db import BusyError

        app, _ = _make_app()

        def locked(*args, **kwargs):
            raise BusyError("database is locked")

        app.view_functions["api.health"] = locked
        response = app.test_client().get("/api/health")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Busy, try again", response.text)


if __name__ == "__main__":
    unittest.main()


class RolesAndVisibilityTest(unittest.TestCase):
    """U5: four roles, manager landing, per-employee 404s, role-route rules."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _person(self, device_user_id: str, role: str = "employee") -> dict:
        email = f"{device_user_id}@example.com"
        self.database.upsert_employee(device_user_id=device_user_id, name=f"P{device_user_id}", email=email)
        person = self.database.employee_by_email(email)
        person["role"] = role
        return person

    def _sign_in(self, person: dict):
        self.client.post("/signin", data={"email": person["email"]})
        return self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def test_manager_signs_in_and_lands_without_error(self) -> None:
        boss = self._person("10", "manager")
        response = self._sign_in(boss)
        self.assertEqual(response.status_code, 302)
        landing = self.client.get(response.headers["Location"])
        self.assertEqual(landing.status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_employee_asking_for_someone_else_gets_404_like_a_missing_id(self) -> None:
        me = self._person("11")
        other = self._person("12")
        self._sign_in(me)
        for path in ("/day", "/week"):
            self.assertEqual(self.client.get(f"{path}?employee={other['id']}").status_code, 404)
            self.assertEqual(self.client.get(f"{path}?employee=99999").status_code, 404)
            self.assertEqual(self.client.get(f"{path}?employee=abc").status_code, 404)
            self.assertEqual(self.client.get(f"{path}?employee={me['id']}").status_code, 200)

    def test_manager_sees_report_but_not_their_own_manager(self) -> None:
        top = self._person("20", "manager")
        mid = self._person("21", "manager")
        low = self._person("22")
        mid["manager_id"] = top["id"]
        low["manager_id"] = mid["id"]
        self._sign_in(mid)
        self.assertEqual(self.client.get(f"/day?employee={low['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/day?employee={top['id']}").status_code, 404)

    def _team(self):
        """A (manager) -> B -> C chain plus an unrelated manager D."""
        a = self._person("40", "manager")
        b = self._person("41", "manager")
        c = self._person("42")
        d = self._person("43", "manager")
        b["manager_id"] = a["id"]
        c["manager_id"] = b["id"]
        return a, b, c, d

    def test_manager_opens_an_indirect_reports_week_with_their_name(self) -> None:
        a, _, c, _ = self._team()
        self._sign_in(a)
        page = self.client.get(f"/week?employee={c['id']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn('class="subject-name">P42<', page.text)
        self.assertIn('href="/team"', page.text)
        self.assertIn(f"/week?employee={c['id']}&amp;offset=-1", page.text)
        day = self.client.get(f"/day?employee={c['id']}&date=2026-10-01")
        self.assertIn(f"/day?employee={c['id']}&amp;date=2026-09-30", day.text)

    def test_own_day_has_no_person_header(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        self.assertNotIn("subject-name", self.client.get("/day").text)

    def test_other_manager_gets_404_for_someone_outside_their_team(self) -> None:
        a, _, _, d = self._team()
        self._sign_in(d)
        self.assertEqual(self.client.get(f"/day?employee={a['id']}").status_code, 404)

    def test_hr_opens_anyone_and_unknown_id_is_404(self) -> None:
        _, _, c, _ = self._team()
        hr = self._person("44", "hr")
        self._sign_in(hr)
        self.assertEqual(self.client.get(f"/day?employee={c['id']}").status_code, 200)
        self.assertEqual(self.client.get("/week?employee=99999").status_code, 404)

    def test_team_lists_only_the_managers_subtree_labelled_team(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        page = self.client.get("/team")
        self.assertEqual(page.status_code, 200)
        self.assertIn("<h1>Team</h1>", page.text)
        self.assertIn("<span>Team</span>", page.text)
        self.assertNotIn("Employees", page.text)
        for name in ("P40", "P41", "P42"):
            self.assertIn(f">{name}</a>", page.text)
        self.assertNotIn(">P43</a>", page.text)

    def test_team_is_everyone_for_hr_and_admin(self) -> None:
        self._team()
        for role, uid in (("hr", "45"), ("admin", "46")):
            person = self._person(uid, role)
            self._sign_in(person)
            page = self.client.get("/team")
            self.assertIn("<h1>Employees</h1>", page.text)
            self.assertIn("<span>Employees</span>", page.text)
            self.assertNotIn("Everyone", page.text)
            self.assertIn(">P43</a>", page.text)

    def test_team_search_filters_and_htmx_returns_the_results_region(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        page = self.client.get("/team?q=p42", headers={"HX-Request": "true"})
        self.assertNotIn("<html", page.text)
        self.assertIn(">P42</a>", page.text)
        self.assertNotIn(">P41</a>", page.text)
        self.assertIn('id="team-count"', page.text)

    def test_team_history_restore_gets_the_full_page(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        restore = self.client.get("/team", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
        self.assertIn("<html", restore.text)
        self.assertIn('class="sidebar"', restore.text)
        self.assertNotIn('hx-swap-oob', restore.text)
        partial = self.client.get("/team", headers={"HX-Request": "true"})
        self.assertNotIn("<html", partial.text)
        self.assertIn("HX-Request", restore.headers.get("Vary", ""))
        self.assertIn("HX-Request", partial.headers.get("Vary", ""))

    def test_team_table_has_a_colgroup_matching_its_header(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        for headers in ({}, {"HX-Request": "true"}):
            page = self.client.get("/team?q=p4", headers=headers)
            table = re.search(r'<table class="[^"]*team-table.*?</table>', page.text, re.S).group(0)
            colgroup = re.search(r"<colgroup>(.*?)</colgroup>", table, re.S)
            self.assertIsNotNone(colgroup, headers)
            head = re.search(r"<thead>(.*?)</thead>", table, re.S).group(1)
            self.assertEqual(colgroup.group(1).count("<col "), head.count("<th "), headers)

    def test_team_history_restore_keeps_full_page_with_colgroup(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        restore = self.client.get("/team", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
        self.assertIn("<html", restore.text)
        self.assertIn("<colgroup>", restore.text)
        self.assertIn("HX-Request", restore.headers.get("Vary", ""))

    def test_team_puts_incomplete_and_empty_days_first(self) -> None:
        a, b, c, _ = self._team()
        # The first minutes of today's work day (default start 06:00).
        start = datetime.combine(_ist_today(), datetime.min.time()) + timedelta(hours=6, minutes=1)
        # P40: a full closed day (met or short). P41: a lone exit (incomplete).
        punches = [
            ("40", start, "entry"), ("40", start + timedelta(minutes=5), "exit"),
            ("41", start + timedelta(minutes=2), "exit"),
        ]
        self.database.insert_punches(
            [
                {"fingerprint": f"t{i}", "device_user_id": uid, "event_timestamp": ts, "trusted_direction": d}
                for i, (uid, ts, d) in enumerate(punches)
            ]
        )
        self._sign_in(a)
        html = self.client.get("/team").text
        order = [html.index(f">{n}</a>") for n in ("P41", "P42", "P40")]
        self.assertEqual(order, sorted(order))

    def test_team_marks_flagged_day_needs_review(self) -> None:
        a, _, _, _ = self._team()
        start = datetime.combine(_ist_today(), datetime.min.time()) + timedelta(hours=6, minutes=1)
        self.database.insert_punches([
            {"fingerprint": f"r{i}", "device_user_id": "40", "event_timestamp": ts, "trusted_direction": d}
            for i, (ts, d) in enumerate([
                (start, "exit"), (start + timedelta(minutes=1), "entry"), (start + timedelta(minutes=5), "exit"),
            ])
        ])
        self._sign_in(a)
        html = self.client.get("/team").text
        self.assertIn("Needs review", html)
        self.assertIn("with no entry", html)

    def test_employee_on_team_lands_on_own_today(self) -> None:
        me = self._person("47")
        self._sign_in(me)
        response = self.client.get("/team")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/day"))

    def test_hr_redirects_to_team(self) -> None:
        hr = self._person("48", "hr")
        self._sign_in(hr)
        response = self.client.get("/hr")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/team"))

    def test_navigation_matches_the_role_table(self) -> None:
        expected = {
            "employee": ["Today", "Week"],
            "manager": ["Today", "Week", "Team"],
            "hr": ["Today", "Week", "Employees", "People", "Fix requests"],
        }
        for i, (role, labels) in enumerate(expected.items()):
            self._sign_in(self._person(f"5{i}", role))
            html = self.client.get("/day").text
            nav = html.split('<nav aria-label="Main">')[1].split("</nav>")[0]
            self.assertEqual(re.findall(r"<span>([^<]+)</span>", nav), labels)

    def _csv_names(self, url: str) -> list[str]:
        import csv as csv_module

        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text[:200])
        return sorted({row["Name"] for row in csv_module.DictReader(io.StringIO(response.text.lstrip("\ufeff")))})

    def test_manager_export_is_scoped_to_their_subtree(self) -> None:
        a, b, c, d = self._team()
        self._sign_in(a)
        self.assertEqual(self._csv_names("/export.csv?from=2026-10-01&to=2026-10-02"), ["P40", "P41", "P42"])
        url = f"/export.csv?from=2026-10-01&to=2026-10-01&employee={c['id']}&employee={d['id']}"
        self.assertEqual(self._csv_names(url), ["P42"])

    def test_hr_export_has_everyone_and_matching_xlsx_rows(self) -> None:
        self._team()
        hr = self._person("44", "hr")
        self._sign_in(hr)
        query = "from=2026-10-01&to=2026-10-03"
        self.assertEqual(self._csv_names(f"/export.csv?{query}"), ["P40", "P41", "P42", "P43", "P44"])
        import csv as csv_module

        csv_rows = list(csv_module.reader(io.StringIO(self.client.get(f"/export.csv?{query}").text.lstrip("\ufeff"))))
        response = self.client.get(f"/export.xlsx?{query}")
        self.assertEqual(response.status_code, 200)
        workbook = openpyxl.load_workbook(io.BytesIO(response.data))
        self.assertEqual(workbook.sheetnames, ["Daily Detail", "Monthly Matrix", "Person Summary"])
        daily = list(workbook["Daily Detail"].iter_rows(min_row=3, values_only=True))
        self.assertEqual([list(r) for r in daily[:1]], [csv_rows[0]])
        self.assertEqual([r[1] for r in daily[1:]], [r[1] for r in csv_rows[1:]])
        self.assertEqual(len(daily), 1 + 5 * 3)
        self.assertIn("work day runs 06:00", workbook["Daily Detail"]["A1"].value)
        matrix = list(workbook["Monthly Matrix"].iter_rows(min_row=4, values_only=True))
        self.assertEqual(sorted(r[1] for r in matrix), ["P40", "P41", "P42", "P43", "P44"])

    def test_selected_scope_without_people_and_bad_ranges_are_400_inline(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        for url, message in (
            ("/export.xlsx?from=2026-10-05&to=2026-10-01", "after the To date"),
            ("/export.csv?from=2026-01-01&to=2026-06-01", "at most 93 days"),
            ("/export.xlsx?from=2026-10-01&to=2026-10-01&scope=selected", "Select at least one person"),
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 400)
            self.assertIn(message, response.text)
            self.assertIn('class="inline-error"', response.text)

    def test_employee_cannot_export(self) -> None:
        self._sign_in(self._person("49"))
        self.assertEqual(self.client.get("/export.csv").status_code, 403)
        self.assertEqual(self.client.get("/export.xlsx").status_code, 403)

    def test_team_has_the_export_panel(self) -> None:
        a, *_ = self._team()
        self._sign_in(a)
        html = self.client.get("/team").text
        self.assertIn('action="/export.xlsx"', html)
        self.assertIn('formaction="/export.csv"', html)
        self.assertIn("Download Excel", html)
        self.assertIn('form="export-form"', html)

    def _csrf(self) -> str:
        html = self.client.get("/day").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def test_hr_via_route_cannot_grant_admin_or_touch_an_admin(self) -> None:
        hr = self._person("30", "hr")
        target = self._person("31")
        admin = self._person("32", "admin")
        self._sign_in(hr)
        csrf = self._csrf()
        response = self.client.post(f"/people/{target['id']}/role", data={"role": "admin", "csrf": csrf})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(target["role"], "employee")
        response = self.client.post(f"/people/{admin['id']}/role", data={"role": "hr", "csrf": csrf})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(admin["role"], "admin")
        response = self.client.post(f"/people/{target['id']}/role", data={"role": "manager", "csrf": csrf})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(target["role"], "manager")


class PeopleImportTest(unittest.TestCase):
    """U6: roster import on /people/import for HR and admin."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _person(self, device_user_id: str, role: str = "employee") -> dict:
        email = f"{device_user_id}@example.com"
        self.database.upsert_employee(device_user_id=device_user_id, name=f"P{device_user_id}", email=email)
        person = self.database.employee_by_email(email)
        person["role"] = role
        return person

    def _sign_in(self, person: dict) -> None:
        self.client.post("/signin", data={"email": person["email"]})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/day").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _upload(self, body: bytes, filename: str = "roster.csv"):
        return self.client.post(
            "/people/import",
            data={"roster": (io.BytesIO(body), filename), "csrf": self._csrf()},
            content_type="multipart/form-data",
        )

    def test_employee_and_manager_cannot_upload_or_view(self) -> None:
        for role in ("employee", "manager"):
            with self.subTest(role=role):
                self._sign_in(self._person(f"9{len(role)}", role))
                self.assertEqual(self._upload(b"name,work email,device user id\r\n").status_code, 403)
                self.assertEqual(self.client.get("/people/import").status_code, 403)
                self.assertEqual(self.client.get("/people/import/template.csv").status_code, 403)

    def test_hr_uploads_with_managers_and_sees_only_issue_rows(self) -> None:
        self._sign_in(self._person("1", "hr"))
        body = (
            b"name,work email,device user id,manager email,role\r\n"
            b"Ravi,ravi@n.com,10,sita@n.com,\r\n"
            b"Sita,sita@n.com,11,,manager\r\n"
            b"Ghost,ghost@n.com,12,nobody@n.com,\r\n"
            b"Broken,broken,13,,\r\n"
        )
        response = self._upload(body)
        self.assertEqual(response.status_code, 200)
        ravi = self.database.employee_by_email("ravi@n.com")
        sita = self.database.employee_by_email("sita@n.com")
        self.assertEqual(ravi["manager_id"], sita["id"])
        self.assertEqual(sita["role"], "manager")
        self.assertIn("<strong>3</strong> added", response.text)
        self.assertIn("<strong>1</strong> with warnings", response.text)
        self.assertIn("<strong>1</strong> rejected", response.text)
        self.assertIn("manager not found", response.text)
        self.assertIn("Download issues (CSV)", response.text)
        results = response.text.split('id="result-heading"')[1].split("</table>")[0]
        self.assertNotIn(">Ravi<", results)

    def test_hr_cannot_change_an_admins_device_id_via_upload(self) -> None:
        self._person("2", "admin")
        self._sign_in(self._person("1", "hr"))
        response = self._upload(b"name,work email,device user id\r\nP2,2@example.com,77\r\n")
        self.assertIn("only an admin", response.text)
        self.assertEqual(self.database.employee_by_email("2@example.com")["device_user_id"], "2")

    def test_csv_template_uploaded_unchanged_reports_zero_rows(self) -> None:
        self._sign_in(self._person("1", "hr"))
        template = self.client.get("/people/import/template.csv").data
        response = self._upload(template)
        self.assertEqual(response.status_code, 200)
        self.assertIn("<strong>0</strong> added", response.text)
        self.assertIn("<strong>0</strong> rejected", response.text)


class PeopleManagementTest(unittest.TestCase):
    """U7: /people search, inline manager and role edits, add/edit forms.
    Runs on a real SQLite database so the guarded writes are exercised."""

    def setUp(self) -> None:
        from tempfile import TemporaryDirectory

        from essl_hub import mail as mail_module
        from essl_hub.db import Database

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = Database(str(Path(tmp.name) / "essl.db"))
        self.app = create_app(database=self.database)
        self.app.testing = True
        self.client = self.app.test_client()
        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.hr = self._person("1", "Hema HR", "hr")
        self.admin = self._person("2", "Arun Admin", "admin")
        self.ram = self._person("3", "Ramesh", email="ramesh@n.com")
        self.sita = self._person("4", "Sita", email="sita.ram@n.com")
        self.ravi = self._person("5", "Ravi")

    def _person(self, device_user_id, name, role="employee", email=None) -> dict:
        email = email or f"{device_user_id}@n.com"
        self.database.upsert_employee(device_user_id=device_user_id, name=name, email=email)
        person = self.database.employee_by_email(email)
        if role != "employee":
            self.database.set_employee_role(person["id"], role)
        return self.database.employee_by_id(person["id"])

    def _sign_in(self, person: dict) -> None:
        self.client.post("/signin", data={"email": person["email"]})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/day").text
        marker = 'name="csrf" value="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _assign(self, person: dict, htmx: bool = True, **data):
        headers = {"HX-Request": "true", "X-CSRF-Token": self._csrf()} if htmx else {}
        if not htmx:
            data["csrf"] = self._csrf()
        return self.client.post(f"/people/{person['id']}/assign", data=data, headers=headers)

    def _edit_row(self, person: dict) -> str:
        return self.client.get(f"/people/{person['id']}/row?edit=1", headers={"HX-Request": "true"}).text

    def _seed(self, count: int) -> None:
        for n in range(count):
            self._person(f"S{n:04d}", f"Seeded Person {n:04d}")

    def test_manager_and_employee_get_403(self) -> None:
        manager = self._person("9", "Mona", "manager")
        for person in (manager, self.ravi):
            with self.subTest(role=person["role"]):
                self._sign_in(person)
                self.assertEqual(self.client.get("/people").status_code, 403)
                self.assertEqual(self.client.get("/people/new").status_code, 403)

    def test_search_matches_name_or_email_case_insensitively(self) -> None:
        self._sign_in(self.hr)
        page = self.client.get("/people?q=RAM").text
        self.assertIn("Ramesh", page)
        self.assertIn("Sita", page)  # email sita.ram@
        self.assertNotIn(f'id="person-{self.ravi["id"]}"', page)
        self.assertIn("2 people", page)
        everyone = self.client.get("/people").text
        self.assertIn("5 people", everyone)

    def test_htmx_search_returns_the_results_fragment_with_an_oob_count(self) -> None:
        self._sign_in(self.hr)
        fragment = self.client.get("/people?q=ravi", headers={"HX-Request": "true"}).text
        self.assertNotIn("<html", fragment)
        self.assertIn('hx-swap-oob="true"', fragment)
        self.assertIn("1 person", fragment)

    def test_hr_changes_a_manager_and_the_fragment_shows_it(self) -> None:
        self._sign_in(self.hr)
        response = self._assign(self.ravi, manager_id=str(self.sita["id"]), role="employee")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.database.employee_by_id(self.ravi["id"])["manager_id"], self.sita["id"])
        self.assertTrue(response.text.lstrip().startswith(f'<tr id="person-{self.ravi["id"]}"'))
        # Save swaps the row back to text: the new manager, no select.
        self.assertIn("Sita", response.text)
        self.assertNotIn("<select", response.text)
        self.assertIn("autofocus", response.text)

    def test_cycle_returns_the_error_fragment_and_leaves_the_database_unchanged(self) -> None:
        self.database.set_employee_manager(self.ravi["id"], self.sita["id"])
        self._sign_in(self.hr)
        response = self._assign(self.sita, manager_id=str(self.ravi["id"]), role="manager")
        self.assertIn('role="alert"', response.text)
        self.assertIn(f'aria-describedby="assign-error-{self.sita["id"]}"', response.text)
        self.assertIn("reports to this person", response.text)
        sita = self.database.employee_by_id(self.sita["id"])
        self.assertIsNone(sita["manager_id"])
        self.assertEqual(sita["role"], "employee")

    def test_no_js_save_redirects_and_errors_render_the_page(self) -> None:
        self._sign_in(self.hr)
        ok = self._assign(self.ravi, htmx=False, manager_id="", role="manager")
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(self.database.employee_by_id(self.ravi["id"])["role"], "manager")
        refused = self._assign(self.ravi, htmx=False, manager_id="", role="admin")
        self.assertEqual(refused.status_code, 403)
        self.assertIn("Only an admin", refused.text)

    def test_hr_sees_no_admin_option_and_admin_rows_are_locked(self) -> None:
        self._sign_in(self.hr)
        page = self.client.get("/people").text
        admin_row = page.split(f'id="person-{self.admin["id"]}"')[1].split("</tr>")[0]
        self.assertIn("Admin only", admin_row)
        self.assertNotIn('<option value="admin"', self._edit_row(self.ravi))
        locked = self._edit_row(self.admin)
        self.assertNotIn('name="role"', locked)
        self.assertIn("Admin only", locked)
        self._sign_in(self.admin)
        self.assertIn('<option value="admin"', self._edit_row(self.ravi))

    def test_add_with_duplicate_email_shows_inline_error_and_creates_nothing(self) -> None:
        self._sign_in(self.hr)
        before = len(self.database.employees())
        response = self.client.post(
            "/people/new",
            data={"name": "Copy", "email": "RAMESH@n.com", "device_user_id": "77", "manager_id": "", "role": "employee", "csrf": self._csrf()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already uses this work email", response.text)
        self.assertIn('aria-invalid="true"', response.text)
        self.assertEqual(len(self.database.employees()), before)

    def test_add_then_assign_manager_and_role(self) -> None:
        self._sign_in(self.hr)
        response = self.client.post(
            "/people/new",
            data={"name": "Neha", "email": "neha@n.com", "device_user_id": "60", "manager_id": str(self.sita["id"]), "role": "manager", "csrf": self._csrf()},
        )
        self.assertEqual(response.status_code, 302)
        neha = self.database.employee_by_email("neha@n.com")
        self.assertEqual((neha["manager_id"], neha["role"]), (self.sita["id"], "manager"))

    def test_hr_edit_of_an_admin_locks_identity_and_rejects_forged_changes(self) -> None:
        self._sign_in(self.hr)
        form = self.client.get(f"/people/{self.admin['id']}/edit").text
        self.assertNotIn('id="f-email"', form)
        self.assertNotIn('id="f-device_user_id"', form)
        response = self.client.post(
            f"/people/{self.admin['id']}/edit",
            data={"name": "Arun Admin", "email": "evil@n.com", "device_user_id": "2", "manager_id": "", "role": "admin", "csrf": self._csrf()},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database.employee_by_id(self.admin["id"])["email"], self.admin["email"])
        response = self.client.post(
            f"/people/{self.admin['id']}/edit",
            data={"name": "Arun Admin", "email": self.admin["email"], "device_user_id": "999", "manager_id": "", "role": "admin", "csrf": self._csrf()},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database.employee_by_id(self.admin["id"])["device_user_id"], "2")

    def test_admin_can_edit_another_admins_identity(self) -> None:
        other = self._person("8", "Second Admin", "admin")
        self._sign_in(self.admin)
        response = self.client.post(
            f"/people/{other['id']}/edit",
            data={"name": "Second Admin", "email": "second@n.com", "device_user_id": "80", "manager_id": "", "role": "admin", "csrf": self._csrf()},
        )
        self.assertEqual(response.status_code, 302)
        updated = self.database.employee_by_id(other["id"])
        self.assertEqual((updated["email"], updated["device_user_id"]), ("second@n.com", "80"))

    def test_unknown_person_is_404(self) -> None:
        self._sign_in(self.hr)
        self.assertEqual(self.client.get("/people/9999/edit").status_code, 404)
        self.assertEqual(self._assign({"id": 9999}, manager_id="").status_code, 404)

    def test_people_page_with_150_people_has_no_per_row_select_and_stays_small(self) -> None:
        self._seed(145)
        self._sign_in(self.hr)
        response = self.client.get("/people")
        self.assertEqual(response.status_code, 200)
        self.assertIn("150 people", response.text)
        self.assertNotIn("<select", response.text)
        self.assertLess(len(response.data), 250 * 1024)
        self.assertIn('class="col-person"', response.text)

    def test_row_edit_opens_select_saves_and_cancels(self) -> None:
        self.database.set_employee_manager(self.ravi["id"], self.sita["id"])
        self._sign_in(self.hr)
        edit = self._edit_row(self.ravi)
        self.assertTrue(edit.lstrip().startswith(f'<tr id="person-{self.ravi["id"]}" class="is-editing"'))
        self.assertEqual(edit.count("<tr"), 1)
        self.assertIn(f'<option value="{self.sita["id"]}" selected>Sita</option>', edit)
        self.assertIn('name="manager_id"', edit)
        self.assertIn(">Cancel</a>", edit)
        cancel = self.client.get(f"/people/{self.ravi['id']}/row", headers={"HX-Request": "true"}).text
        self.assertNotIn("<select", cancel)
        self.assertIn("Sita", cancel)
        self.assertEqual(self.database.employee_by_id(self.ravi["id"])["manager_id"], self.sita["id"])
        saved = self._assign(self.ravi, manager_id=str(self.ram["id"]), role="employee")
        self.assertNotIn("<select", saved.text)
        self.assertIn("Ramesh", saved.text)
        self.assertEqual(self.database.employee_by_id(self.ravi["id"])["manager_id"], self.ram["id"])
        self.assertEqual(self.client.get("/people/9999/row").status_code, 404)

    def test_row_edit_is_hr_and_admin_only(self) -> None:
        manager = self._person("9", "Mona", "manager")
        self._sign_in(manager)
        self.assertEqual(self.client.get(f"/people/{self.ravi['id']}/row?edit=1").status_code, 403)

    def test_own_row_edit_keeps_role_locked(self) -> None:
        self._sign_in(self.hr)
        own = self._edit_row(self.hr)
        self.assertNotIn('name="role"', own)
        self.assertIn('name="manager_id"', own)

    def test_history_restore_gets_the_full_page_and_htmx_gets_the_partial(self) -> None:
        self._sign_in(self.hr)
        restore = self.client.get(
            "/people", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}
        )
        self.assertIn("<html", restore.text)
        self.assertIn('class="sidebar"', restore.text)
        partial = self.client.get("/people", headers={"HX-Request": "true"})
        self.assertNotIn("<html", partial.text)
        self.assertIn('hx-swap-oob="true"', partial.text)
        for response in (restore, partial):
            self.assertIn("HX-Request", response.headers.get("Vary", ""))
        full = self.client.get("/people").text
        self.assertIn('"historyCacheSize": 0', full)
        self.assertIn('"refreshOnHistoryMiss": true', full)
        self.assertNotIn('hx-swap-oob', full)

    def test_people_query_count_does_not_grow_with_roster_size(self) -> None:
        def counted(names):
            calls = {"n": 0}
            for name in names:
                original = getattr(self.database, name)

                def wrapper(*args, _original=original, **kwargs):
                    calls["n"] += 1
                    return _original(*args, **kwargs)

                setattr(self.database, name, wrapper)
            self.addCleanup(lambda: [self.database.__dict__.pop(n, None) for n in names])
            return calls

        self._sign_in(self.hr)
        self.client.get("/people")  # warm the nav issue-count cache
        names = ("employees", "punches_for_employee_range", "punches_for_device_users_range")
        calls = counted(names)
        self.client.get("/people")
        small = calls["n"]
        for n in names:
            self.database.__dict__.pop(n, None)
        self._seed(60)
        calls = counted(names)
        self.client.get("/people")
        self.assertEqual(calls["n"], small)
        self.assertLessEqual(small, 3)

    # --- extra device IDs (U5) ---

    def _punches(self, *items) -> None:
        self.database.initialize(DEVICES)
        self.database.insert_punches([
            {"fingerprint": f"x-{uid}-{at.isoformat()}", "device_id": DEVICES[0].id, "site": "Uppal",
             "device_user_id": uid, "employee_name": name, "event_timestamp": at, "trusted_direction": way}
            for uid, name, at, way in items
        ])

    def _two_format_day(self):
        kiran = self._person("NP0128", "Kiran Kumar")
        day = (_ist_now() - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        self._punches(("NP0128", "Kiran", day.replace(hour=9), "entry"),
                      ("NP128", "Kiran", day.replace(hour=17), "exit"))
        return kiran, day.date()

    def test_two_format_ids_link_as_extra_and_count_everywhere(self) -> None:
        kiran, day = self._two_format_day()
        self._sign_in(self.hr)
        page = self.client.get("/people/issues").text
        self.assertIn("Add as another device ID for Kiran Kumar", page)
        confirm = self.client.get(f"/people/issues?confirm=1&device_user_id=NP128&person={kiran['id']}").text
        self.assertIn("added as another device ID", confirm)
        response = self.client.post("/people/issues/link", data={
            "csrf": self._csrf(), "device_user_id": "NP128", "employee_id": kiran["id"]})
        self.assertEqual(response.status_code, 302)
        after = self.database.employee_by_id(kiran["id"])
        self.assertEqual((after["device_user_id"], after["device_ids"]), ("NP0128", ["NP0128", "NP128"]))
        self.assertNotIn("Not in Directory", self.client.get("/people/issues").text)
        self.assertIn("8h 00m", self.client.get(f"/day?date={day.isoformat()}&employee={kiran['id']}").text)
        self.assertIn("8h 00m", self.client.get(f"/week?date={day.isoformat()}&employee={kiran['id']}").text)
        self.assertIn("NP0128, NP128", self.client.get(f"/quick/{kiran['id']}", headers={"HX-Request": "true"}).text)
        csv_text = self.client.get(f"/export.csv?from={day.isoformat()}&to={day.isoformat()}").text
        row = next(line for line in csv_text.splitlines() if "Kiran Kumar" in line)
        self.assertIn("09:00,17:00,2,8:00", row)

    def _quick_swipe_day(self, window: int):
        import csv as csv_module

        from essl_hub import export
        kiran = self._person("NP0128", "Kiran Kumar")
        day = (_ist_now() - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        self._punches(*[("NP0128", "Kiran", day.replace(hour=h, minute=0, second=s), None)
                        for h, s in ((9, 0), (9, 45), (12, 0), (17, 0))])
        self.database.set_device_policy(DEVICES[0].id, window_seconds=window, wait_for_door_close=True)
        self._sign_in(self.hr)
        page = self.client.get(f"/day?date={day.date().isoformat()}&employee={kiran['id']}").text
        csv_text = self.client.get(f"/export.csv?from={day.date().isoformat()}&to={day.date().isoformat()}").text
        row = next(r for r in csv_module.reader(io.StringIO(csv_text.lstrip("\ufeff"))) if "Kiran Kumar" in r)
        return page, row[export.DAILY_HEADER.index("Effective")]

    def test_day_page_and_export_agree_under_device_window(self) -> None:
        # KTD3: the X990's 30 s floor counts 09:00:45 as Out; 12:00 In, 17:00 Out -> 5h.
        page, effective = self._quick_swipe_day(30)
        self.assertIn("5h 00m", page)
        self.assertEqual(effective, "5:00")
        self.assertNotIn("not counted", page)

    def test_default_window_collapses_with_reason_on_day_page(self) -> None:
        # 60 s: 09:00:45 is not counted; 09:00 In, 12:00 Out, 17:00 unpaired -> 3h.
        page, effective = self._quick_swipe_day(60)
        self.assertIn("3h 00m", page)
        self.assertEqual(effective, "3:00")
        self.assertIn("same as In 09:00:00, 45s earlier", page)

    def test_twin_of_never_punched_id_replaces_the_main_id(self) -> None:
        kiran = self._person("128", "Kiran Kumar")
        self._punches(("NP0128", "Someone", _ist_now() - timedelta(days=1), "entry"))
        self._sign_in(self.hr)
        self.assertIn("Change ID to NP0128", self.client.get("/people/issues").text)
        self.client.post("/people/issues/link", data={
            "csrf": self._csrf(), "device_user_id": "NP0128", "employee_id": kiran["id"]})
        after = self.database.employee_by_id(kiran["id"])
        self.assertEqual(after["device_ids"], ["NP0128"])

    def test_zero_unknown_id_gets_no_add_or_link_button(self) -> None:
        self._punches(("0", "Nobody", _ist_now() - timedelta(days=1), "entry"))
        self._sign_in(self.hr)
        page = self.client.get("/people/issues").text
        self.assertIn("all-zero ID", page)
        self.assertNotIn("Add as new person", page)
        self.assertNotIn("people/new?device_user_id=0", page)

    def _add_extra(self, person, value):
        return self.client.post(f"/people/{person['id']}/device-ids",
                                data={"csrf": self._csrf(), "extra_device_user_id": value})

    def test_extra_id_add_clash_and_remove_on_the_form(self) -> None:
        self._sign_in(self.hr)
        response = self._add_extra(self.ravi, "3")  # Ramesh's main ID
        self.assertEqual(response.status_code, 400)
        self.assertIn("Ramesh already uses device ID 3", response.text)
        self.database.add_extra_device_id(self.sita["id"], "NP0004")
        response = self._add_extra(self.ravi, "np0004")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Sita already uses device ID NP0004", response.text)
        self.assertEqual(self._add_extra(self.ravi, "0").status_code, 400)
        self.assertEqual(self._add_extra(self.ravi, " np0005 ").status_code, 302)
        page = self.client.get(f"/people/{self.ravi['id']}/edit").text
        self.assertIn("Other device IDs", page)
        self.assertIn("NP0005", page)
        self._punches(("NP0005", "Ravi", _ist_now() - timedelta(days=1), "entry"))
        self.assertNotIn("Not in Directory", self.client.get("/people/issues").text)
        response = self.client.post(f"/people/{self.ravi['id']}/device-ids/remove",
                                    data={"csrf": self._csrf(), "device_user_id": "NP0005"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.database.extra_device_ids(self.ravi["id"]), [])
        self.assertIn("Not in Directory", self.client.get("/people/issues").text)

    def test_hr_cannot_change_an_admins_extra_ids(self) -> None:
        self.database.add_extra_device_id(self.admin["id"], "A2")
        self._sign_in(self.hr)
        self.assertEqual(self._add_extra(self.admin, "A3").status_code, 403)
        response = self.client.post(f"/people/{self.admin['id']}/device-ids/remove",
                                    data={"csrf": self._csrf(), "device_user_id": "A2"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database.extra_device_ids(self.admin["id"]), ["A2"])
        self.assertNotIn("Add device ID", self.client.get(f"/people/{self.admin['id']}/edit").text)
        self.client.post("/signout", data={"csrf": self._csrf()})
        self._sign_in(self.admin)
        self.assertEqual(self._add_extra(self.admin, "A3").status_code, 302)
        self.assertEqual(self.database.extra_device_ids(self.admin["id"]), ["A2", "A3"])


    # --- inline Device ID (U6) ---

    def test_hr_edits_device_id_inline_and_hours_follow(self) -> None:
        self._sign_in(self.hr)
        row = self._edit_row(self.ravi)
        self.assertIn('name="device_user_id"', row)
        self.assertIn(f'aria-label="Edit {self.ravi["name"]}"', self.client.get(f"/people/{self.ravi['id']}/row", headers={"HX-Request": "true"}).text)
        day = (_ist_now() - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        self._punches(("NP0005", "Ravi", day.replace(hour=9), "entry"), ("NP0005", "Ravi", day.replace(hour=17), "exit"))
        response = self._assign(self.ravi, device_user_id=" np0005 ", manager_id="", role="employee")
        self.assertIn("NP0005", response.text)
        self.assertNotIn('name="device_user_id"', response.text)
        self.assertEqual(self.database.employee_by_id(self.ravi["id"])["device_user_id"], "NP0005")
        csv_text = self.client.get(f"/export.csv?from={day.date().isoformat()}&to={day.date().isoformat()}").text
        self.assertIn("09:00,17:00,2,8:00", next(line for line in csv_text.splitlines() if ",Ravi," in line))

    def test_hr_inline_edit_on_admin_locks_device_id_and_refuses_forgery(self) -> None:
        self._sign_in(self.hr)
        row = self._edit_row(self.admin)
        self.assertNotIn('name="device_user_id"', row)
        self.assertIn("locked", row)
        response = self._assign(self.admin, htmx=False, device_user_id="NP9999", manager_id="", role="admin")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database.employee_by_id(self.admin["id"])["device_user_id"], "2")

    def test_duplicate_and_zero_inline_ids_show_errors_and_keep_other_fields(self) -> None:
        self._sign_in(self.hr)
        response = self._assign(self.ravi, device_user_id="3", manager_id=str(self.sita["id"]), role="manager")
        self.assertIn("Ramesh already uses device ID 3", response.text)
        self.assertIn('value="3"', response.text)
        self.assertIn(f'<option value="{self.sita["id"]}" selected>', response.text)
        self.assertIn('<option value="manager" selected>', response.text)
        after = self.database.employee_by_id(self.ravi["id"])
        self.assertEqual((after["device_user_id"], after["manager_id"], after["role"]), ("5", None, "employee"))
        response = self._assign(self.ravi, device_user_id="NP0000", manager_id="", role="employee")
        self.assertIn("Device ID can&#39;t be zero", response.text)
        self.assertEqual(self.database.employee_by_id(self.ravi["id"])["device_user_id"], "5")


class PunchArchiveWebTest(unittest.TestCase):
    """Punch archive U3 and the retired-device fix U1, on a real SQLite
    database so the archive filter and timestamp types are exercised."""

    def setUp(self) -> None:
        from tempfile import TemporaryDirectory

        from essl_hub import mail as mail_module
        from essl_hub.db import Database

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.database = Database(str(Path(tmp.name) / "essl.db"))
        self.app = create_app(database=self.database)
        self.app.testing = True
        self.client = self.app.test_client()
        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.hr = self._person("1", "Hema HR", "hr")
        self.admin = self._person("2", "Arun Admin", "admin")
        self.sita = self._person("3", "Sita Rao")
        today = datetime.combine(_ist_today(), datetime.min.time())
        self.days = [today - timedelta(days=n) for n in (10, 5, 2)]
        self._punches("9999", [day + timedelta(hours=9) for day in self.days], name="Test card")
        # Sita's own ID, but the device calls it someone else: a "name" issue.
        self._punches("3", [self.days[-1] + timedelta(hours=9)], name="Mohan Das")

    def _person(self, device_user_id, name, role="employee") -> dict:
        email = f"{device_user_id}@n.com"
        self.database.upsert_employee(device_user_id=device_user_id, name=name, email=email)
        person = self.database.employee_by_email(email)
        if role != "employee":
            self.database.set_employee_role(person["id"], role)
        return self.database.employee_by_id(person["id"])

    def _punches(self, uid: str, stamps, name: str = "") -> None:
        self.database.insert_punches([
            {"fingerprint": f"fp-{uid}-{at.isoformat()}", "device_id": DEVICES[0].id, "site": "Uppal",
             "device_user_id": uid, "employee_name": name, "event_timestamp": at}
            for at in stamps
        ])

    def _sign_in(self, person: dict) -> None:
        self.client.post("/signin", data={"email": person["email"]})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        return re.search(r'name="csrf" value="([^"]+)"', self.client.get("/day").text).group(1)

    def _count(self, uid: str = "9999") -> int:
        return next((r["punch_count"] for r in self.database.device_id_summary() if r["device_user_id"] == uid), 0)

    def _preview(self, query: str = "") -> str:
        return self.client.get(f"/people/issues?archive=9999{query}").text

    @staticmethod
    def _hidden(page: str, name: str) -> str:
        return re.search(rf'name="{name}" value="([^"]*)"', page).group(1)

    def _confirm(self, page: str, **extra):
        data = {"csrf": self._csrf(), "device_user_id": self._hidden(page, "device_user_id"),
                "from_ts": self._hidden(page, "from_ts"), "to_ts": self._hidden(page, "to_ts"), **extra}
        return self.client.post("/people/issues/archive", data=data, follow_redirects=True)

    # --- U1 ---------------------------------------------------------------

    def test_devices_page_renders_with_a_retired_device(self) -> None:
        self._sign_in(self.admin)
        self.database.set_device_retired(DEVICES[1].id, True)
        response = self.client.get("/devices")
        self.assertEqual(response.status_code, 200)
        self.assertIn(DEVICES[1].id, response.text)

    # --- archive ------------------------------------------------------------

    def test_admin_previews_and_archives_every_punch_of_an_unknown_id(self) -> None:
        self._sign_in(self.admin)
        issues = self.client.get("/people/issues").text
        self.assertIn('name="archive" value="9999"', issues)
        self.assertIn('href="/people/issues/archived"', issues)
        page = self._preview()
        self.assertIn("Archive punches of 9999?", page)
        self.assertIn("<strong>3 punches</strong>", page)
        self.assertIn("from the first punch", page)
        self.assertEqual(self._hidden(page, "from_ts"), "")
        response = self._confirm(page, reason="Test card")
        self.assertIn("Archived 3 punches from ID 9999.", response.text)
        self.assertNotIn('name="archive" value="9999"', response.text)
        self.assertEqual(self._count(), 0)
        (rule,) = self.database.punch_archives()
        self.assertEqual((rule["reason"], rule["created_by_name"]), ("Test card", "Arun Admin"))

    def test_a_date_range_archives_only_its_days_and_the_issue_stays(self) -> None:
        self._sign_in(self.admin)
        day = self.days[1].date().isoformat()
        page = self._preview(f"&from={day}&to={day}")
        self.assertIn("<strong>1 punch</strong>", page)
        self.assertIn(f'value="{day}"', page)
        response = self._confirm(page)
        self.assertIn("Archived 1 punch from ID 9999.", response.text)
        self.assertEqual(self._count(), 2)
        self.assertIn('name="archive" value="9999"', response.text)
        (rule,) = self.database.punch_archives()
        self.assertEqual(rule["from_ts"], self.days[1] + timedelta(hours=6))
        self.assertEqual(rule["to_ts"], self.days[1] + timedelta(days=1, hours=6))

    def test_hr_gets_no_archive_action_and_a_direct_post_is_403(self) -> None:
        self._sign_in(self.hr)
        self.assertNotIn('name="archive"', self.client.get("/people/issues").text)
        self.assertNotIn("Archive punches of", self._preview())
        response = self.client.post("/people/issues/archive", data={
            "csrf": self._csrf(), "device_user_id": "9999", "to_ts": "2030-01-01 00:00:00"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database.punch_archives(), [])

    def test_a_mapped_id_cannot_be_archived(self) -> None:
        self._sign_in(self.admin)
        self.assertIn("Name differs", self.client.get("/people/issues").text)
        self.assertIn("can be archived", self.client.get("/people/issues?archive=3").text)
        response = self.client.post("/people/issues/archive", data={
            "csrf": self._csrf(), "device_user_id": "3", "to_ts": _ist_now().strftime("%Y-%m-%d %H:%M:%S")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("can be archived", response.text)
        self.assertEqual(self.database.punch_archives(), [])

    def test_missing_csrf_is_refused(self) -> None:
        self._sign_in(self.admin)
        page = self._preview()
        response = self.client.post("/people/issues/archive", data={
            "device_user_id": "9999", "to_ts": self._hidden(page, "to_ts")})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.database.punch_archives(), [])

    def test_a_to_date_of_today_is_clamped_so_later_swipes_stay_visible(self) -> None:
        self._sign_in(self.admin)
        page = self._preview(f"&to={_ist_today().isoformat()}")
        to_ts = datetime.fromisoformat(self._hidden(page, "to_ts"))
        self.assertLessEqual(to_ts, _ist_now())
        self._confirm(page)
        self._punches("9999", [to_ts + timedelta(minutes=1)])
        self.assertEqual(self._count(), 1)

    def test_from_after_to_is_a_form_error_and_writes_nothing(self) -> None:
        self._sign_in(self.admin)
        page = self._preview(f"&from={self.days[2].date().isoformat()}&to={self.days[0].date().isoformat()}")
        self.assertIn("The from date is after the to date.", page)
        self.assertNotIn('action="/people/issues/archive"', page)
        self.assertEqual(self.database.punch_archives(), [])

    def test_an_empty_range_disables_confirm(self) -> None:
        self._sign_in(self.admin)
        day = (self.days[0] + timedelta(days=1)).date().isoformat()
        page = self._preview(f"&from={day}&to={day}")
        self.assertIn("No punches in this range.", page)
        self.assertRegex(page, r'<button class="button button-primary" type="submit" disabled>')
        response = self._confirm(page)
        self.assertIn("No punches in this range.", response.text)
        self.assertEqual(self.database.punch_archives(), [])

    def test_the_previewed_end_is_stored_even_when_punches_arrive_before_confirm(self) -> None:
        self._sign_in(self.admin)
        page = self._preview()
        to_ts = datetime.fromisoformat(self._hidden(page, "to_ts"))
        self._punches("9999", [to_ts + timedelta(seconds=2)])
        self.assertIn("Archived 3 punches from ID 9999.", self._confirm(page).text)
        self.assertEqual(self.database.punch_archives()[0]["to_ts"], to_ts)
        self.assertEqual(self._count(), 1)

    # --- archive list and restore ----------------------------------------

    def test_archive_list_empty_state(self) -> None:
        self._sign_in(self.hr)
        page = self.client.get("/people/issues/archived")
        self.assertEqual(page.status_code, 200)
        self.assertIn("No archived punches", page.text)

    def test_hr_reads_the_list_and_an_admin_restores(self) -> None:
        archive_id = self.database.archive_punches("9999", from_ts=None, to_ts=None, reason="Test card",
                                                   actor_id=self.admin["id"])
        self._sign_in(self.hr)
        page = self.client.get("/people/issues/archived").text
        self.assertIn('<td data-label="Reason">Test card</td>', page)
        self.assertIn('<td data-label="Device ID"><strong>9999</strong></td>', page)
        self.assertIn("Arun Admin", page)
        self.assertNotIn("Restore", page.split("<h1")[1].split("Nothing is deleted")[1])
        self.assertNotIn("Restore the archive of", self.client.get(f"/people/issues/archived?restore={archive_id}").text)
        self.assertEqual(self.client.post(f"/people/issues/archived/{archive_id}/restore",
                                          data={"csrf": self._csrf()}).status_code, 403)
        self.client.post("/signout", data={"csrf": self._csrf()})
        self._sign_in(self.admin)
        confirm = self.client.get(f"/people/issues/archived?restore={archive_id}").text
        self.assertIn("Restore the archive of 9999?", confirm)
        self.assertIn("<strong>3 punches</strong>", confirm)
        response = self.client.post(f"/people/issues/archived/{archive_id}/restore",
                                    data={"csrf": self._csrf()}, follow_redirects=True)
        self.assertIn("Restored 3 punches of ID 9999.", response.text)
        self.assertIn('class="admin-section archive-restored"', response.text)
        self.assertEqual(self._count(), 3)
        self.assertIn("<span class=\"num\">3</span> punches", self.client.get("/people/issues").text)
        again = self.client.post(f"/people/issues/archived/{archive_id}/restore",
                                 data={"csrf": self._csrf()}, follow_redirects=True)
        self.assertEqual(again.status_code, 200)
        self.assertIn("was already restored", again.text)
        self.assertEqual(self.client.post("/people/issues/archived/999/restore",
                                          data={"csrf": self._csrf()}).status_code, 404)

    # --- later mapping ------------------------------------------------------

    def test_linking_an_archived_id_warns_that_its_punches_stay_hidden(self) -> None:
        self.database.archive_punches("9999", from_ts=self.days[1], to_ts=None)
        self._sign_in(self.admin)
        page = self.client.get(f"/people/issues?confirm=1&device_user_id=9999&person={self.sita['id']}").text
        self.assertIn("<strong>1 punch</strong> will start counting", page)
        self.assertIn("2 punches of this ID are archived and stay hidden.", page)

    def test_adding_an_archived_id_on_the_person_form_asks_once_more(self) -> None:
        self.database.archive_punches("9999", from_ts=None, to_ts=None)
        self._sign_in(self.admin)
        url = f"/people/{self.sita['id']}/device-ids"
        first = self.client.post(url, data={"csrf": self._csrf(), "extra_device_user_id": "9999"})
        self.assertEqual(first.status_code, 200)
        self.assertIn("3 punches of this ID are archived and stay hidden.", first.text)
        self.assertIn('name="archived_ok" value="9999"', first.text)
        self.assertEqual(self.database.extra_device_ids(self.sita["id"]), [])
        self.client.post(url, data={"csrf": self._csrf(), "extra_device_user_id": "9999", "archived_ok": "9999"})
        self.assertEqual(self.database.extra_device_ids(self.sita["id"]), ["9999"])

    def test_changing_a_main_id_to_an_archived_one_asks_once_more(self) -> None:
        self.database.archive_punches("9999", from_ts=None, to_ts=None)
        self._sign_in(self.admin)
        data = {"csrf": self._csrf(), "name": "Sita Rao", "email": self.sita["email"],
                "device_user_id": "9999", "manager_id": "", "role": "employee"}
        first = self.client.post(f"/people/{self.sita['id']}/edit", data=data)
        self.assertIn("3 punches of this ID are archived and stay hidden.", first.text)
        self.assertEqual(self.database.employee_by_id(self.sita["id"])["device_user_id"], "3")
        self.client.post(f"/people/{self.sita['id']}/edit", data={**data, "archived_ok": "9999"})
        self.assertEqual(self.database.employee_by_id(self.sita["id"])["device_user_id"], "9999")

    def test_adding_a_person_with_an_archived_id_asks_once_more(self) -> None:
        self.database.archive_punches("9999", from_ts=None, to_ts=None)
        self._sign_in(self.admin)
        data = {"csrf": self._csrf(), "name": "Temp Tester", "email": "tester@n.com",
                "device_user_id": "9999", "manager_id": "", "role": "employee"}
        first = self.client.post("/people/new", data=data)
        self.assertIn("3 punches of this ID are archived and stay hidden.", first.text)
        self.assertIsNone(self.database.employee_by_email("tester@n.com"))
        self.client.post("/people/new", data={**data, "archived_ok": "9999"})
        self.assertEqual(self.database.employee_by_email("tester@n.com")["device_user_id"], "9999")

    def test_the_inline_device_id_edit_refuses_an_archived_id(self) -> None:
        self.database.archive_punches("9999", from_ts=None, to_ts=None)
        self._sign_in(self.admin)
        response = self.client.post(f"/people/{self.sita['id']}/assign", data={
            "csrf": self._csrf(), "device_user_id": "9999", "manager_id": "", "role": "employee"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("3 archived punches that would stay hidden", response.text)
        self.assertEqual(self.database.employee_by_id(self.sita["id"])["device_user_id"], "3")

    # --- bad input ----------------------------------------------------------

    def _post_archive(self, **fields):
        return self.client.post("/people/issues/archive", data={
            "csrf": self._csrf(), "device_user_id": "9999", **fields})

    def test_a_lost_or_malformed_range_is_refused_and_writes_nothing(self) -> None:
        self._sign_in(self.admin)
        for fields in ({}, {"to_ts": "garbage"}, {"from_ts": "2026-10-01T00:00:00", "to_ts": "garbage"}):
            response = self._post_archive(**fields)
            self.assertEqual(response.status_code, 400)
            self.assertIn("The archive range was lost", response.text)
        self.assertEqual(self.database.punch_archives(), [])

    def test_a_timezone_aware_range_is_a_400_not_a_500(self) -> None:
        self._sign_in(self.admin)
        response = self._post_archive(to_ts="2030-01-01T00:00:00+05:30")
        self.assertEqual(response.status_code, 400)
        response = self._post_archive(from_ts="2026-10-01T00:00:00Z", to_ts="2026-10-02 00:00:00")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.database.punch_archives(), [])

    def test_a_reversed_range_is_refused(self) -> None:
        self._sign_in(self.admin)
        response = self._post_archive(from_ts="2026-10-09 00:00:00", to_ts="2026-10-02 00:00:00")
        self.assertEqual(response.status_code, 400)
        self.assertIn("The from date must be before the to date.", response.text)
        self.assertEqual(self.database.punch_archives(), [])

    def test_a_non_ascii_restore_parameter_is_ignored(self) -> None:
        self.database.archive_punches("9999", from_ts=None, to_ts=None)
        self._sign_in(self.admin)
        page = self.client.get("/people/issues/archived?restore=%C2%B2")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Restore the archive of 9999?", page.text)

    def test_an_htmx_fragment_skips_the_device_health_and_unmapped_scans(self) -> None:
        self._sign_in(self.admin)
        with patch.object(self.database, "unreachable_device_details", return_value=[]) as devices, \
                patch.object(self.database, "unmapped_device_user_ids", return_value=[]) as unmapped:
            self.client.get("/team?q=sita", headers={"HX-Request": "true"})
            devices.assert_not_called()
            unmapped.assert_not_called()
            self.client.get("/team")
            devices.assert_called()
            unmapped.assert_called()

    def test_action_buttons_carry_a_well_formed_aria_label(self) -> None:
        archive_id = self.database.archive_punches("9999", from_ts=None, to_ts=None)
        self._sign_in(self.admin)
        self.assertIn('aria-label="Restore the archive of 9999"', self.client.get("/people/issues/archived").text)
        self.database.restore_punch_archive(archive_id)
        self.assertIn('aria-label="Archive punches of 9999"', self.client.get("/people/issues").text)


class ScreensTest(unittest.TestCase):
    """U11: devices split, settings, Today and sign-in screens."""

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))

    def _client_as(self, role: str):
        email = f"{role}@example.com"
        self.database.upsert_employee(device_user_id=f"d-{role}", name=role.title(), email=email)
        self.database.set_employee_role(self.database.employee_by_email(email)["id"], role)
        client = self.app.test_client()
        client.environ_base["REMOTE_ADDR"] = f"10.9.{len(role)}.{sum(map(ord, role)) % 250 + 1}"
        client.post("/signin", data={"email": email})
        client.post("/signin/verify", data={"code": self.captured["code"]})
        return client

    def test_hr_cannot_open_devices_but_gets_id_issues(self) -> None:
        client = self._client_as("hr")
        self.assertEqual(client.get("/devices").status_code, 403)
        people = client.get("/people").text
        self.assertNotIn('href="/devices"', people)
        self.assertIn('href="/people/issues"', people)
        self.assertEqual(client.get("/people/issues").status_code, 200)

    def test_manager_cannot_open_id_issues(self) -> None:
        self.assertEqual(self._client_as("manager").get("/people/issues").status_code, 403)

    def test_admin_sees_the_edit_form(self) -> None:
        page = self._client_as("admin").get("/devices")
        self.assertIn("Edit connection", page.text)
        self.assertIn('action="/devices/uppal-x990"', page.text)

    def test_manager_and_employee_cannot_open_devices(self) -> None:
        for role in ("manager", "employee"):
            with self.subTest(role=role):
                self.assertEqual(self._client_as(role).get("/devices").status_code, 403)

    def test_hr_settings_is_forbidden(self) -> None:
        self.assertEqual(self._client_as("hr").get("/settings").status_code, 403)

    def test_stale_last_checked_shows_a_warning(self) -> None:
        client = self._client_as("admin")
        device = self.database.device_by_id("uppal-x990")
        device["status"] = "connected"
        with patch.dict(os.environ, {"ESSL_HEARTBEAT_WRITE_SECONDS": "300"}):
            device["last_attempt"] = _ist_now() - timedelta(minutes=4)
            fresh = client.get("/devices").text
            device["last_attempt"] = _ist_now() - timedelta(minutes=11)
            stale = client.get("/devices").text
        self.assertNotIn("Not checked recently", fresh)
        self.assertIn("Connected", fresh)
        self.assertIn("Not checked recently", stale)
        self.assertIn("11 min ago", stale)

    def test_day_without_punches_renders_the_empty_state_not_a_chart(self) -> None:
        page = self._client_as("employee").get("/day")
        self.assertIn("No punches yet today", page.text)
        self.assertNotIn('class="day-bar"', page.text)
        self.assertNotIn("GROSS HOURS", page.text)

    def test_day_with_punches_renders_the_attendance_log(self) -> None:
        client = self._client_as("employee")
        day = _ist_today() - timedelta(days=1)
        start = datetime.combine(day, datetime.min.time())
        self.database.insert_punches(
            [
                {"fingerprint": "s-in", "device_user_id": "d-employee", "event_timestamp": start + timedelta(hours=9), "trusted_direction": "entry"},
                {"fingerprint": "s-out", "device_user_id": "d-employee", "event_timestamp": start + timedelta(hours=17, minutes=30), "trusted_direction": "exit"},
            ]
        )
        page = client.get(f"/day?date={day.isoformat()}").text
        self.assertIn('<h3 class="raw-punches-title">All punches (2)</h3>', page)
        self.assertIn('punch-time num">09:00:00</span><span class="punch-dir">In</span>', page)
        self.assertIn('punch-time num">17:30:00</span><span class="punch-dir">Out</span>', page)
        self.assertIn('<dt>Inside</dt><dd><span class="num">8:30:00</span>', page)
        self.assertIn('role="img" aria-label="8h 30m effective, 100% of the', page)
        self.assertNotIn("data-poll=", page)  # past days never poll

    def test_week_renders_bars_and_target_line(self) -> None:
        page = self._client_as("employee").get("/week").text
        self.assertIn('class="chart chart-week"', page)
        self.assertIn('class="wk-target"', page)

    def test_code_field_is_one_time_code_numeric(self) -> None:
        page = self.app.test_client().post("/signin", data={"email": "x@example.com"}).text
        self.assertIn('inputmode="numeric"', page)
        self.assertIn('autocomplete="one-time-code"', page)

    def test_expired_code_offers_a_new_code_inline(self) -> None:
        from essl_hub import auth as auth_module

        client = self.app.test_client()
        with patch.object(auth_module, "verify_code", return_value=auth_module.VerifyOutcome(ok=False, error="expired")):
            page = client.post("/signin/verify", data={"code": "123456"}).text
        self.assertIn("That code has expired", page)
        self.assertIn("Request a new code", page)
        self.assertIn('role="alert"', page)

    def test_wrong_code_keeps_the_form_with_an_inline_error(self) -> None:
        from essl_hub import auth as auth_module

        client = self.app.test_client()
        with patch.object(auth_module, "verify_code", return_value=auth_module.VerifyOutcome(ok=False, error="invalid")):
            page = client.post("/signin/verify", data={"code": "000000"}).text
        self.assertIn("That code is not correct", page)
        self.assertIn('aria-invalid="true"', page)
        self.assertIn('autocomplete="one-time-code"', page)


class SyncHealthTest(unittest.TestCase):
    """Boundaries of the health-state table: collector + backups (U5)."""

    NOW = datetime(2026, 10, 10, 12, 0, 0)

    def _health(self, collector="ok", backup="ok", r2=True):
        from essl_hub.routes.common import sync_health

        if collector == "ok":
            collector = self._collector()
        if backup == "ok":
            backup = self._backup(timedelta(minutes=10))
        return sync_health(collector, backup, self.NOW, r2_configured=r2, backup_minutes=15)

    def _collector(self, age=timedelta(minutes=1), depth=0, oldest=None, dead=0):
        return {
            "last_poll": (self.NOW - age).isoformat(sep=" "),
            "spool_depth": depth,
            "spool_oldest_at": None if oldest is None else (self.NOW - oldest).isoformat(sep=" "),
            "dead_count": dead,
        }

    def _backup(self, age, *, blocked=False, error=None, entries=None):
        at = None if age is None else (self.NOW - age).isoformat(sep=" ")
        return {"last_ok_at": at, "last_upload_at": at, "blocked": blocked, "last_error": error,
                "entries": entries if entries is not None else [{"status": "ok", "started": at}]}

    def _signal(self, health, key):
        return next(d for d in health["details"] if d["key"] == key)

    def test_all_green(self) -> None:
        health = self._health()
        self.assertEqual((health["tone"], health["label"]), ("ok", "All systems healthy"))
        self.assertEqual(self._signal(health, "backup")["summary"], "Last backup 10 min ago")
        self.assertIn("IST", health["title"])
        self.assertEqual([d["key"] for d in health["details"]], ["collector", "backup"])

    def test_backup_boundaries(self) -> None:
        cases = [(timedelta(minutes=29), "ok"), (timedelta(minutes=30), "warn"),
                 (timedelta(minutes=50), "warn"), (timedelta(minutes=89), "warn"),
                 (timedelta(minutes=90), "bad"), (None, "bad")]
        for age, tone in cases:
            with self.subTest(age=age):
                health = self._health(backup=self._backup(age))
                self.assertEqual(health["tone"], tone)
                if tone != "ok":
                    self.assertEqual(health["signal"], "backup")
        for missing in (None, "garbage"):
            self.assertEqual(self._health(backup=missing)["label"], "No backup yet")

    def test_blocked_is_red_with_different_database_message(self) -> None:
        err = "R2 backups belong to a different database (abc)"
        health = self._health(backup=self._backup(timedelta(minutes=5), blocked=True, error=err))
        self.assertEqual((health["tone"], health["signal"]), ("bad", "backup"))
        self.assertIn("different database", health["label"])
        self.assertEqual(self._signal(health, "backup")["error"], err)

    def test_repeated_errors_are_red(self) -> None:
        failed = [{"status": "failed"}] * 3
        health = self._health(backup=self._backup(timedelta(minutes=5), error="boom", entries=failed))
        self.assertEqual((health["tone"], health["label"]), ("bad", "Backups failing"))
        two = [{"status": "ok"}, {"status": "failed"}, {"status": "failed"}]
        self.assertEqual(self._health(backup=self._backup(timedelta(minutes=5), entries=two))["tone"], "ok")

    def test_no_r2_is_off_and_collector_still_rates(self) -> None:
        health = self._health(backup=None, r2=False)
        self.assertEqual(health["tone"], "ok")
        self.assertEqual(self._signal(health, "backup")["summary"], "Off")
        self.assertEqual(self._signal(health, "backup")["tone"], "neutral")
        health = self._health(backup=None, r2=False, collector=None)
        self.assertEqual((health["tone"], health["signal"]), ("bad", "collector"))

    def test_collector_boundaries(self) -> None:
        cases = [
            (self._collector(age=timedelta(minutes=10)), "ok"),
            (self._collector(age=timedelta(minutes=11)), "bad"),
            (self._collector(depth=1, oldest=timedelta(minutes=59)), "warn"),
            (self._collector(depth=1, oldest=timedelta(minutes=61)), "bad"),
            (self._collector(dead=1), "bad"),
            (None, "bad"),
            ({"last_poll": "garbage", "spool_depth": "x"}, "bad"),
        ]
        for status, tone in cases:
            with self.subTest(status=status):
                health = self._health(collector=status)
                self.assertEqual(health["tone"], tone)
                if tone != "ok":
                    self.assertEqual(health["signal"], "collector")
                    self.assertIn("Collector", health["label"])

    def test_worst_signal_wins_and_is_named(self) -> None:
        health = self._health(backup=self._backup(timedelta(minutes=40)), collector=self._collector(dead=2))
        self.assertEqual((health["tone"], health["signal"]), ("bad", "collector"))
        self.assertEqual(health["label"], "Collector: 2 dead punches")

    def _devices(self, *names_and_last):
        return [{"id": name.lower().replace(" ", "-"), "name": name, "last_success": last}
                for name, last in names_and_last]

    def test_one_unreachable_device_is_named_with_its_last_success(self) -> None:
        health = self._health_devices(self._devices(("Uppal X990", datetime(2026, 10, 25, 12, 30))))
        self.assertEqual((health["tone"], health["signal"]), ("bad", "devices"))
        self.assertEqual(health["label"], "Uppal X990 unreachable since 25 Oct")
        # No times on this signal: the title carries just its summary.
        self.assertIn(" · Devices: Uppal X990 unreachable since 25 Oct", health["title"])
        self.assertEqual(self._signal(health, "devices")["count"], 1)
        never = self._health_devices(self._devices(("Uppal X990", None)))
        self.assertEqual(never["label"], "Uppal X990 unreachable, never connected")

    def test_two_unreachable_devices_are_one_signal(self) -> None:
        health = self._health_devices(self._devices(
            ("Gachibowli C3-400", datetime(2026, 10, 26, 8, 0)), ("Uppal X990", None)))
        self.assertEqual([d["key"] for d in health["details"]], ["collector", "backup", "devices"])
        self.assertEqual(health["label"], "2 devices unreachable")
        self.assertEqual(self._signal(health, "devices")["summary"],
                         "2 devices unreachable: Gachibowli C3-400 since 26 Oct; Uppal X990, never connected")

    def test_no_unreachable_devices_adds_no_signal(self) -> None:
        for devices in (None, []):
            health = self._health_devices(devices)
            self.assertEqual([d["key"] for d in health["details"]], ["collector", "backup"])
            self.assertEqual(health["label"], "All systems healthy")

    def _health_devices(self, devices):
        from essl_hub.routes.common import sync_health

        return sync_health(self._collector(), self._backup(timedelta(minutes=10)), self.NOW,
                           backup_minutes=15, devices=devices)


class TeamSummaryTest(unittest.TestCase):
    """U5: the Team / Employees status line, Manager column and unknown-ID notice."""

    NOW = datetime(2026, 10, 26, 18, 0)

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        for patcher in (*_fixed_now(self.NOW), patch("essl_hub.routes.team._now_naive", lambda: self.NOW)):
            patcher.start()
            self.addCleanup(patcher.stop)
        # A manager (no punches) with 4 reports inside and 2 who met the target.
        self.boss = self._person("50", "Boss", "manager")
        day = datetime(2026, 10, 26)
        punches = []
        for i in range(6):
            person = self._person(str(60 + i), f"Report{i}")
            self.database.set_employee_manager(person["id"], self.boss["id"])
            punches.append(_punch_row(f"in-{i}", day.replace(hour=9), "entry", uid=str(60 + i)))
            if i >= 4:
                punches.append(_punch_row(f"out-{i}", day.replace(hour=17, minute=30), "exit", uid=str(60 + i)))
        # A swipe from a card nobody owns.
        punches.append(_punch_row("ghost", day.replace(hour=10), "entry", uid="9999"))
        self.database.insert_punches(punches)
        self.hr = self._person("70", "Hema", "hr")

    def _person(self, uid: str, name: str, role: str = "employee") -> dict:
        email = f"{name.lower()}@example.com"
        self.database.upsert_employee(device_user_id=uid, name=name, email=email)
        person = self.database.employee_by_email(email)
        self.database.set_employee_role(person["id"], role)
        return person

    def _sign_in(self, person: dict) -> None:
        self.client.post("/signout")
        self.client.post("/signin", data={"email": person["email"]})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    @staticmethod
    def _count(html: str) -> str:
        return re.search(r'<p id="team-count"[^>]*>([^<]*)</p>', html).group(1)

    def test_summary_counts_each_person_once_by_todays_status(self) -> None:
        from types import SimpleNamespace

        from essl_hub.routes.team import _status_summary

        self._sign_in(self.boss)
        page = self.client.get("/team").text
        self.assertEqual(self._count(page), "7 people: 4 inside · 2 met · 1 no punches")
        rows = [{"result": SimpleNamespace(status=s)} for s in ("short", "incomplete", "met")]
        self.assertEqual(_status_summary(rows), "3 people: 1 met · 1 short · 1 needs review")
        self.assertEqual(_status_summary(rows[:1]), "1 person: 1 short")
        self.assertEqual(_status_summary([]), "0 people")

    def test_search_recounts_the_summary_out_of_band(self) -> None:
        self._sign_in(self.boss)
        fragment = self.client.get("/team?q=report", headers={"HX-Request": "true"}).text
        self.assertIn('hx-swap-oob="true"', fragment)
        self.assertEqual(self._count(fragment), "6 people: 4 inside · 2 met")
        fragment = self.client.get("/team?q=report4", headers={"HX-Request": "true"}).text
        self.assertEqual(self._count(fragment), "1 person: 1 met")

    def test_manager_sees_no_manager_column_and_no_unknown_id_notice(self) -> None:
        self._sign_in(self.boss)
        page = self.client.get("/team").text
        self.assertNotIn('<th scope="col">Manager</th>', page)
        self.assertNotIn('data-label="Manager"', page)
        self.assertNotIn("col-manager", page)
        self.assertNotIn("no one in the Directory", page)

    def test_hr_sees_manager_column_and_the_notice_above_the_table(self) -> None:
        self._sign_in(self.hr)
        page = self.client.get("/team").text
        self.assertIn('<th scope="col">Manager</th>', page)
        self.assertIn('data-label="Manager">Boss</td>', page)
        notice = page.index("1 device ID has punches but no one in the Directory.")
        self.assertLess(notice, page.index('id="team-count"'))
        self.assertLess(notice, page.index("<table"))
        self.assertIn('<a href="/people/issues">Review ID issues</a>', page)
        self.database.archive_punches("9999", from_ts=None, to_ts=None, actor_id=self.hr["id"])
        self.assertNotIn("no one in the Directory", self.client.get("/team").text)


class DeviceHealthWebTest(unittest.TestCase):
    """U5 (KTD8): an unreachable device in the health badge and on the Devices nav item."""

    NOW = datetime(2026, 10, 26, 18, 0)

    def setUp(self) -> None:
        import json
        import tempfile

        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A healthy collector and backups off, so only the devices can set the tone.
        collector = os.path.join(tmp.name, "collector.json")
        backup = os.path.join(tmp.name, "backup.json")
        with open(collector, "w", encoding="utf-8") as handle:
            json.dump({"last_poll": (self.NOW - timedelta(minutes=1)).isoformat(sep=" "),
                       "spool_depth": 0, "dead_count": 0}, handle)
        with open(backup, "w", encoding="utf-8") as handle:
            json.dump({"r2_configured": False}, handle)
        env = {"ESSL_COLLECTOR_STATUS_PATH": collector, "ESSL_BACKUP_STATUS_PATH": backup}
        for patcher in (*_fixed_now(self.NOW), patch.dict(os.environ, env)):
            patcher.start()
            self.addCleanup(patcher.stop)
        from essl_hub import mail as mail_module

        captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        self.database.upsert_employee(device_user_id="1", name="Ada Admin", email="ada@example.com")
        self.database.set_employee_role(self.database.employee_by_email("ada@example.com")["id"], "admin")
        self.client.post("/signin", data={"email": "ada@example.com"})
        self.client.post("/signin/verify", data={"code": captured["code"]})
        for device in self.database.devices_by_id.values():
            device["status"] = "connected"

    def _fail(self, device_id: str, last_success=datetime(2026, 10, 25, 12, 30)) -> None:
        device = self.database.device_by_id(device_id)
        device["status"] = "error"
        device["last_success"] = last_success

    @staticmethod
    def _devices_entry(html: str) -> str:
        start = html.index('<nav aria-label="Main">')
        nav = html[start : html.index("</nav>", start)]
        return re.search(r'<a class="nav-item" href="/devices".*?</a>', nav, re.S).group(0)

    def test_all_connected_is_healthy_with_no_devices_badge(self) -> None:
        page = self.client.get("/week").text
        self.assertIn("<span>All systems healthy</span>", page)
        self.assertNotIn("nav-count", self._devices_entry(page))

    def test_one_device_in_error_names_it_and_badges_devices(self) -> None:
        self._fail("uppal-x990")
        page = self.client.get("/week").text
        self.assertNotIn("All systems healthy", page)
        self.assertIn("<span>Uppal X990 unreachable since 25 Oct</span>", page)
        entry = self._devices_entry(page)
        self.assertIn('aria-label="Devices, 1 device unreachable"', entry)
        self.assertIn('<span class="nav-count" title="1 device unreachable" aria-hidden="true">1</span>', entry)
        health = self.client.get("/health").text
        self.assertIn('<h2 id="signal-devices">Devices</h2>', health)

    def test_two_devices_are_one_signal_and_a_retired_one_is_ignored(self) -> None:
        self._fail("uppal-x990")
        self._fail(_C3)
        page = self.client.get("/week").text
        self.assertIn("<span>2 devices unreachable</span>", page)
        self.assertIn('aria-label="Devices, 2 devices unreachable"', self._devices_entry(page))
        self.database.set_device_retired(_C3, True)
        page = self.client.get("/week").text
        self.assertIn("<span>Uppal X990 unreachable since 25 Oct</span>", page)
        self.assertIn('aria-label="Devices, 1 device unreachable"', self._devices_entry(page))

    def test_backup_table_cells_carry_data_labels(self) -> None:
        # U7: on a phone the stacked rows label every value.
        import json

        with open(os.environ["ESSL_BACKUP_STATUS_PATH"], "w", encoding="utf-8") as handle:
            json.dump({"r2_configured": False, "entries": [
                {"status": "ok", "started": "2026-10-26 17:00:00", "finished": "2026-10-26 17:00:05",
                 "key": "exports/run-1.db.gz", "size": 2048},
            ]}, handle)
        page = self.client.get("/health").text
        row = re.search(r"<tr>\s*<td[^>]*>2026-10-26 17:00:00.*?</tr>", page, re.S).group(0)
        cells = re.findall(r"<td([^>]*)>", row)
        self.assertEqual(len(cells), 5)
        self.assertEqual(
            [re.search(r'data-label="([^"]+)"', c).group(1) for c in cells],
            ["Started (IST)", "Finished (IST)", "Result", "Size", "Detail"],
        )

    def test_id_issue_notice_is_at_the_top_of_devices(self) -> None:
        from essl_hub.routes.people import forget_issue_count

        # The admin's own swipe keeps "never punched" out of the count.
        self.database.insert_punches([_punch_row("ghost", datetime(2026, 10, 26, 10), "entry", uid="9999"),
                                      _punch_row("ada", datetime(2026, 10, 26, 9), "entry", uid="1")])
        forget_issue_count()  # the badge count is cached across requests
        page = self.client.get("/devices").text
        notice = page.index("1 device ID issue to check.")
        self.assertLess(notice, page.index('class="device-list"'))
        self.database.archive_punches("9999", from_ts=None, to_ts=None)
        forget_issue_count()
        self.assertNotIn("device ID issue", self.client.get("/devices").text)


class BrandIconsTest(unittest.TestCase):
    """U1: the NITCO favicon set on every page."""

    setUp = HoursRenderingTest.setUp
    _sign_in = HoursRenderingTest._sign_in

    ICON_HREF = re.compile(r'<link rel="(?:icon|apple-touch-icon)"[^>]*href="([^"]+)"')

    def _icon_hrefs(self, html: str) -> list[str]:
        return self.ICON_HREF.findall(html)

    def test_signin_and_app_pages_link_existing_brand_icons(self) -> None:
        static_root = Path(self.app.static_folder)
        signin = self.client.get("/signin").get_data(as_text=True)
        self._sign_in()
        day = self.client.get("/day").get_data(as_text=True)
        for html in (signin, day):
            hrefs = self._icon_hrefs(html)
            self.assertGreaterEqual(len(hrefs), 5)
            for href in hrefs:
                self.assertTrue(href.startswith("/static/brand/"), href)
                self.assertTrue((static_root / href.removeprefix("/static/")).is_file(), href)

    def test_head_icons_template_paths_resolve_on_disk(self) -> None:
        source = Path(self.app.template_folder) / "_head_icons.html"
        names = re.findall(r"filename='([^']+)'", source.read_text())
        self.assertTrue(names)
        for name in names:
            self.assertTrue((Path(self.app.static_folder) / name).is_file(), name)

    def test_root_favicon_is_public_and_long_cached(self) -> None:
        response = self.app.test_client().get("/favicon.ico")
        self.assertEqual(response.status_code, 200)
        self.assertIn("icon", response.mimetype)
        cache = response.headers.get("Cache-Control", "")
        self.assertIn("max-age", cache)
        self.assertNotIn("no-store", cache)
        response.close()


class FixReportTest(unittest.TestCase):
    """U7: the employee report flow, from the emailed link and the day view."""

    DAY = datetime(2026, 10, 26).date()
    NOW = datetime(2026, 10, 26, 18, 0)

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        for patcher in (*_fixed_now(self.NOW), patch.dict(os.environ, {"ESSL_PUBLIC_URL": "https://hub.example.com"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.asha = self._person("118", "Asha Rao")
        self.bob = self._person("119", "Bob Das")
        self.boss = self._person("200", "Meera Boss", "manager")
        self.hari = self._person("300", "Hari HR", "hr")
        self.admin = self._person("400", "Ada Admin", "admin")
        self.database.set_employee_manager(self.asha["id"], self.boss["id"])
        self.database.insert_punches([
            _punch_row("a-in", datetime(2026, 10, 26, 9, 0), "entry", uid="118"),
            _punch_row("a-out", datetime(2026, 10, 26, 13, 0), "exit", uid="118"),
            _punch_row("b-in", datetime(2026, 10, 26, 9, 5), "entry", uid="119"),
        ])
        self.token = self._link(self.asha, self.DAY)

    def _person(self, uid: str, name: str, role: str = "employee") -> dict:
        email = f"{name.split()[0].lower()}@example.com"
        self.database.upsert_employee(device_user_id=uid, name=name, email=email)
        person = self.database.employee_by_email(email)
        self.database.set_employee_role(person["id"], role)
        return person

    def _link(self, person: dict, day, expires_at=None) -> str:
        return self.database.create_report_link(
            person["id"], day, expires_at=expires_at or _ist_now() + timedelta(days=7))

    def _sign_in(self, person: dict) -> None:
        self.client.post("/signout")
        self.client.post("/signin", data={"email": person["email"]})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self) -> str:
        html = self.client.get("/day").text
        marker = 'name="csrf-token" content="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _post_link(self, token=None, **data):
        return self.client.post(f"/fix/{token or self.token}", data={"action": "send", **data})

    def _missed_in(self, at="13:40", row=0, **extra):
        return self._post_link(**{f"add-{row}-time": at, f"add-{row}-dir": "in"}, **extra)

    def _requests(self, person=None) -> list[dict]:
        person = person or self.asha
        return [r for r in self.database.fix_requests.values() if r["employee_id"] == person["id"]]

    # --- Viewing the link ---------------------------------------------------

    def test_get_and_head_render_the_swipes_and_create_nothing(self) -> None:
        page = self.client.get(f"/fix/{self.token}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("09:00:00", page.text)
        self.assertIn("13:00:00", page.text)
        self.assertNotIn("09:05:00", page.text)  # Bob's swipe
        self.assertIn("This was the other direction", page.text)
        self.assertIn("Ignore this swipe", page.text)
        self.assertIn('value="a-in"', page.text)
        self.assertIn("Everything&#39;s correct", page.text)
        self.assertIn('href="/privacy"', page.text)
        self.assertIn('class="auth-panel"', page.text)
        self.assertEqual(page.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(self.client.head(f"/fix/{self.token}").status_code, 200)
        self.assertEqual(self.database.fix_requests, {})
        self.assertEqual(self.database.outbox, [])
        self.assertIsNone(self.client.get_cookie("es_session"))

    def test_expired_and_unknown_tokens_show_the_same_expired_page(self) -> None:
        expired = self._link(self.asha, self.DAY, expires_at=_ist_now() - timedelta(minutes=1))
        for token in ("not-a-real-token", expired):
            for response in (self.client.get(f"/fix/{token}"), self._missed_in(token=token)):
                self.assertEqual(response.status_code, 404)
                self.assertIn("This link has expired or isn", response.text)
                self.assertIn('href="/signin"', response.text)
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(self.database.fix_requests, {})

    def test_link_for_a_day_older_than_seven_days_is_expired(self) -> None:
        old = self._link(self.asha, self.DAY - timedelta(days=7))
        self.assertEqual(self.client.get(f"/fix/{old}").status_code, 404)

    # --- Filing a fix -------------------------------------------------------

    def test_missed_in_creates_a_pending_request_and_emails_hr_and_manager(self) -> None:
        response = self._missed_in("13:40")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/fix/{self.token}?sent=1")
        [request] = self._requests()
        self.assertEqual((request["status"], request["source"], request["work_day"]),
                         ("pending", "link", self.DAY))
        self.assertTrue(request["punch_set_hash"])
        [line] = self.database.fix_request(request["id"])["lines"]
        self.assertEqual((line["kind"], line["event_time"], line["direction"]),
                         ("add_in", datetime(2026, 10, 26, 13, 40), "entry"))
        hr_mail, manager_mail = self.database.outbox
        self.assertEqual(hr_mail["recipients"], ["hari@example.com"])
        self.assertIn(f"https://hub.example.com/fixes/review/{request['id']}", hr_mail["text_body"])
        self.assertIn("Missed swipe-in at 13:40", hr_mail["text_body"])
        self.assertEqual(manager_mail["recipients"], ["meera@example.com"])
        self.assertNotIn("/fixes/review/", manager_mail["text_body"])
        self.assertNotIn("Review request", manager_mail["html_body"])
        done = self.client.get(response.headers["Location"])
        self.assertEqual(done.status_code, 200)
        self.assertIn("Sent to HR", done.text)
        self.assertIn("Missed swipe-in at 13:40", done.text)

    def test_a_second_post_adds_a_line_and_sends_no_new_email(self) -> None:
        self._missed_in("13:40")
        self.assertEqual(len(self.database.outbox), 2)
        page = self.client.get(f"/fix/{self.token}")
        self.assertIn("Your report is with HR", page.text)
        self.assertIn("Missed swipe-in at 13:40", page.text)
        self.assertNotIn("Everything&#39;s correct", page.text)
        response = self._post_link(flip="a-out", note="I went back in.")
        self.assertEqual(response.status_code, 302)
        [request] = self._requests()
        lines = self.database.fix_request(request["id"])["lines"]
        self.assertEqual([(l["kind"], l["target_fingerprint"], l["direction"]) for l in lines],
                         [("add_in", None, "entry"), ("flip", "a-out", "entry")])
        self.assertEqual(lines[1]["note"], "I went back in.")
        self.assertEqual(len(self.database.outbox), 2)

    def test_flip_and_ignore_targets_and_after_midnight_times(self) -> None:
        self._post_link(ignore="a-in", **{"add-0-time": "01:30", "add-0-dir": "out"},
                        token=self._link(self.asha, self.DAY - timedelta(days=1)))
        # Day before: no punches of Asha's, so a-in is not hers that day.
        self.assertEqual(self._requests(), [])
        response = self._post_link(token=self._link(self.asha, self.DAY - timedelta(days=1)),
                                   **{"add-0-time": "01:30", "add-0-dir": "out"})
        self.assertEqual(response.status_code, 302)
        [request] = self._requests()
        [line] = self.database.fix_request(request["id"])["lines"]
        # 01:30 on the 25th's work day is the early hours of the 26th.
        self.assertEqual((line["kind"], line["event_time"]), ("add_out", datetime(2026, 10, 26, 1, 30)))

    # --- Closing and reopening ----------------------------------------------

    def test_everythings_correct_closes_the_day_and_notifies_nobody(self) -> None:
        response = self._post_link(action="correct")
        self.assertEqual(response.status_code, 302)
        [request] = self._requests()
        self.assertEqual(request["status"], "closed_correct")
        self.assertEqual(self.database.outbox, [])
        done = self.client.get(response.headers["Location"])
        self.assertIn("we won't send more reminders for this day", done.text)
        page = self.client.get(f"/fix/{self.token}")
        self.assertIn("You told us everything on this day was correct", page.text)
        self.assertIn("Actually, something&#39;s wrong", page.text)
        self.assertNotIn('name="flip"', page.text)
        reopened = self.client.get(f"/fix/{self.token}?report=1")
        self.assertIn('name="flip"', reopened.text)
        self._missed_in("13:40")
        [request] = self._requests()
        self.assertEqual(request["status"], "pending")
        self.assertEqual(len(self.database.outbox), 2)

    def test_i_left_for_the_day_closes_it(self) -> None:
        self._post_link(action="left")
        self.assertEqual(self._requests()[0]["status"], "closed_left")
        self.assertIn("You told us you left for the day", self.client.get(f"/fix/{self.token}").text)

    def test_closing_cannot_be_combined_with_fix_lines(self) -> None:
        response = self._post_link(action="left", flip="a-out")
        self.assertEqual(response.status_code, 400)
        self.assertIn("press Send to HR", response.text)
        self.assertIn('value="a-out" checked', response.text)
        self.assertEqual(self._requests(), [])

    # --- Decided days -------------------------------------------------------

    def test_a_report_on_a_rejected_day_starts_a_new_linked_request(self) -> None:
        self._missed_in("13:40")
        [first] = self._requests()
        line_id = self.database.fix_request(first["id"])["lines"][0]["id"]
        self.database.decide_fix_lines(first["id"], [{"line_id": line_id, "status": "rejected"}],
                                       actor_id=self.hari["id"], note="The reader shows you out until 15:00.")
        page = self.client.get(f"/fix/{self.token}")
        self.assertIn("Not approved", page.text)
        self.assertIn("The reader shows you out until 15:00.", page.text)
        self.assertIn("Report another problem", page.text)
        self.assertNotIn('name="flip"', page.text)
        self.assertIn('name="flip"', self.client.get(f"/fix/{self.token}?report=1").text)
        self._missed_in("15:00")
        first_again, second = sorted(self._requests(), key=lambda r: r["id"])
        self.assertEqual(first_again["status"], "rejected")
        self.assertEqual((second["status"], second["previous_request_id"]), ("pending", first["id"]))
        self.assertEqual(len(self.database.outbox), 4)

    # --- Token and ownership limits ------------------------------------------

    def test_a_token_for_a_cannot_file_for_b_or_another_day(self) -> None:
        response = self._missed_in("13:40", employee_id=str(self.bob["id"]), employee=str(self.bob["id"]),
                                   work_day="2026-10-25", day="2026-10-25")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._requests(self.bob), [])
        [request] = self._requests()
        self.assertEqual(request["work_day"], self.DAY)

    def test_another_employees_fingerprint_is_rejected(self) -> None:
        for field in ("flip", "ignore"):
            response = self._post_link(**{field: "b-in"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("isn&#39;t on this day", response.text)
        self.assertEqual(self.database.fix_requests, {})

    def test_manager_and_hr_cannot_report_someone_elses_day(self) -> None:
        for actor in (self.boss, self.hari):
            self._sign_in(actor)
            csrf = self._csrf()
            url = f"/day/{self.DAY.isoformat()}/report"
            self.assertEqual(self.client.get(f"{url}?employee={self.asha['id']}").status_code, 403)
            response = self.client.post(f"{url}?employee={self.asha['id']}", data={
                "csrf": csrf, "action": "send", "add-0-time": "13:40", "add-0-dir": "in"})
            self.assertEqual(response.status_code, 403)
            response = self.client.post(url, data={"csrf": csrf, "action": "send", "flip": "a-out",
                                                   "employee": str(self.asha["id"])})
            self.assertEqual(response.status_code, 403)
            # Their own page: Asha's swipe is not theirs to flip.
            response = self.client.post(url, data={"csrf": csrf, "action": "send", "flip": "a-out"})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.database.fix_requests, {})

    def test_signed_in_report_lands_on_the_same_pending_request_as_the_link(self) -> None:
        self._missed_in("13:40")
        self._sign_in(self.asha)
        url = f"/day/{self.DAY.isoformat()}/report"
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Your report is with HR", page.text)
        self.assertIn('class="app-shell"', page.text)
        response = self.client.post(url, data={"csrf": self._csrf(), "action": "send", "ignore": "a-in"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"{url}?sent=1")
        [request] = self._requests()
        self.assertEqual(len(self.database.fix_request(request["id"])["lines"]), 2)
        self.assertEqual(len(self.database.outbox), 2)
        self.assertIn("Sent to HR", self.client.get(response.headers["Location"]).text)

    def test_signed_in_report_needs_csrf_and_a_recent_own_day(self) -> None:
        self._sign_in(self.asha)
        url = f"/day/{self.DAY.isoformat()}/report"
        self.assertEqual(self.client.post(url, data={"action": "correct"}).status_code, 400)
        for day in ("2026-10-19", "2026-10-27", "not-a-date"):
            self.assertEqual(self.client.get(f"/day/{day}/report").status_code, 404, day)
        self.assertEqual(self.client.get("/day/2026-10-20/report").status_code, 200)
        self.client.post("/signout")
        self.assertEqual(self.client.get(url).status_code, 302)

    # --- Validation limits --------------------------------------------------

    def test_future_and_malformed_times_are_form_errors(self) -> None:
        for value, message in (("19:30", "hasn&#39;t happened yet"), ("03:00", "hasn&#39;t happened yet"),
                               ("25:10", "Enter a time like 13:40"), ("noon", "Enter a time like 13:40")):
            response = self._missed_in(value)
            self.assertEqual(response.status_code, 400, value)
            self.assertIn(message, response.text)
            self.assertIn('id="f-add-0-time"', response.text)
            self.assertRegex(response.text, r'id="f-add-0-time"[^>]*aria-invalid="true"')
        response = self._post_link(**{"add-0-time": "13:40"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose In or Out", response.text)
        self.assertEqual(self.database.fix_requests, {})

    def test_note_over_500_characters_is_a_form_error(self) -> None:
        response = self._missed_in("13:40", note="x" * 501)
        self.assertEqual(response.status_code, 400)
        self.assertRegex(response.text, r'id="f-note"[^>]*aria-invalid="true"')
        self.assertEqual(self.database.fix_requests, {})

    def test_an_eleventh_line_is_refused(self) -> None:
        ten = {}
        for i in range(10):
            ten.update({f"add-{i}-time": f"{10 + i // 2}:{(i % 2) * 30:02d}", f"add-{i}-dir": "in" if i % 2 else "out"})
        self.assertEqual(self._post_link(**ten).status_code, 302)
        response = self._missed_in("17:00")
        self.assertEqual(response.status_code, 400)
        self.assertIn("at most 10 changes", response.text)
        [request] = self._requests()
        self.assertEqual(len(self.database.fix_request(request["id"])["lines"]), 10)

    def test_an_eleventh_post_from_one_ip_in_a_minute_is_refused(self) -> None:
        for _ in range(10):
            self.assertEqual(self._post_link().status_code, 400)
        response = self._post_link()
        self.assertEqual(response.status_code, 429)
        other_ip = self.client.post(f"/fix/{self.token}", data={"action": "send"},
                                    environ_base={"REMOTE_ADDR": "10.0.0.9"})
        self.assertEqual(other_ip.status_code, 400)

    # --- Approver fallback --------------------------------------------------

    def test_with_no_hr_user_the_request_email_goes_to_the_admins(self) -> None:
        self.database.set_employee_role(self.hari["id"], "employee")
        self._missed_in("13:40")
        hr_mail, manager_mail = self.database.outbox
        self.assertEqual(hr_mail["recipients"], ["ada@example.com"])
        self.assertIn("/fixes/review/", hr_mail["text_body"])
        self.assertEqual(manager_mail["recipients"], ["meera@example.com"])

    def test_hr_requester_is_not_emailed_about_their_own_request(self) -> None:
        token = self._link(self.hari, self.DAY)
        self._post_link(token=token, **{"add-0-time": "10:00", "add-0-dir": "in"})
        [hr_mail] = self.database.outbox
        self.assertEqual(hr_mail["recipients"], ["ada@example.com"])

    # --- Privacy and the day view -------------------------------------------

    def test_privacy_page_renders_signed_out(self) -> None:
        page = self.client.get("/privacy")
        self.assertEqual(page.status_code, 200)
        self.assertIn("How we use attendance data", page.text)
        self.assertIn("never used for discipline", page.text)
        self.assertIn("90 days", page.text)

    def test_report_link_shows_on_own_recent_day_only(self) -> None:
        self._sign_in(self.asha)
        link = f'href="/day/{self.DAY.isoformat()}/report"'
        self.assertIn(link, self.client.get(f"/day?date={self.DAY.isoformat()}").text)
        self.assertIn('href="/day/2026-10-20/report"', self.client.get("/day?date=2026-10-20").text)
        self.assertNotIn("Report a problem", self.client.get("/day?date=2026-10-19").text)
        self._sign_in(self.boss)
        other = self.client.get(f"/day?employee={self.asha['id']}&date={self.DAY.isoformat()}")
        self.assertEqual(other.status_code, 200)
        self.assertNotIn("Report a problem", other.text)
        self.assertIn("Report a problem", self.client.get("/day").text)

    def test_access_logs_redact_the_report_token(self) -> None:
        import logging

        records = []

        class _Keep(logging.Handler):
            def emit(self, record):
                records.append(record)

        logger = logging.getLogger("werkzeug")
        handler = _Keep()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        previous = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.setLevel, previous)
        logger.info('%s - - "%s" %s', "127.0.0.1", f"GET /fix/{self.token}?sent=1 HTTP/1.1", 200)
        [record] = records
        self.assertNotIn(self.token, record.getMessage())
        self.assertIn("/fix/[redacted]?sent=1", record.getMessage())

    def test_a_crash_on_the_link_logs_no_token(self) -> None:
        # Flask's own "Exception on <path>" line, logged when nothing re-raises.
        self.app.testing = False
        with patch.object(self.database, "file_fix_lines", side_effect=RuntimeError("boom")), \
                self.assertLogs("essl_hub.web", level="ERROR") as captured:
            response = self._missed_in("13:40")
        self.assertEqual(response.status_code, 500)
        messages = [record.getMessage() for record in captured.records]
        self.assertTrue(any("/fix/[redacted]" in message for message in messages), messages)
        self.assertFalse(any(self.token in message for message in messages))

    # --- Robustness ---------------------------------------------------------

    def test_a_failed_notification_still_lands_on_sent(self) -> None:
        with patch.object(self.database, "enqueue_mail", side_effect=RuntimeError("smtp down")), \
                self.assertLogs("essl_hub.routes.fixes", level="ERROR"):
            response = self._missed_in("13:40")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/fix/{self.token}?sent=1")
        [request] = self._requests()
        self.assertEqual(request["status"], "pending")
        self.assertEqual(len(self.database.fix_request(request["id"])["lines"]), 1)

    def test_sending_the_same_form_twice_files_one_line(self) -> None:
        for _ in range(2):
            self.assertEqual(self._missed_in("13:40", flip="a-out").status_code, 302)
        [request] = self._requests()
        lines = self.database.fix_request(request["id"])["lines"]
        self.assertEqual([(l["kind"], l["target_fingerprint"]) for l in lines],
                         [("add_in", None), ("flip", "a-out")])
        self.assertEqual(len(self.database.outbox), 2)  # HR and manager, once

    def test_the_twenty_first_post_to_one_link_in_an_hour_is_refused(self) -> None:
        for n in range(20):
            response = self.client.post(f"/fix/{self.token}", data={"action": "send"},
                                        environ_base={"REMOTE_ADDR": f"10.0.1.{n}"})
            self.assertEqual(response.status_code, 400)
        response = self.client.post(f"/fix/{self.token}", data={"action": "send"},
                                    environ_base={"REMOTE_ADDR": "10.0.2.1"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "60")
        other = self._link(self.bob, self.DAY)
        response = self.client.post(f"/fix/{other}", data={"action": "send"},
                                    environ_base={"REMOTE_ADDR": "10.0.2.2"})
        self.assertEqual(response.status_code, 400)

    def test_sliding_window_frees_hits_once_they_age_out(self) -> None:
        from essl_hub import rate_limit

        clock = [1000.0]
        window = rate_limit.SlidingWindow(2, 60)
        with patch.object(rate_limit._clock, "monotonic", side_effect=lambda: clock[0]):
            self.assertTrue(window.allow("k"))
            clock[0] += 30
            self.assertTrue(window.allow("k"))
            self.assertFalse(window.allow("k"))
            self.assertTrue(window.allow("other"))
            clock[0] += 30  # the first hit is now exactly 60 s old
            self.assertTrue(window.allow("k"))
            self.assertFalse(window.allow("k"))
            clock[0] += 61
            self.assertTrue(window.allow("k"))
            self.assertTrue(window.allow("k"))


class FixReviewTest(unittest.TestCase):
    """U8: the HR review page, confirm-then-apply, and the sign-in deep link."""

    DAY = datetime(2026, 10, 26).date()
    NOW = datetime(2026, 10, 26, 18, 0)

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        for patcher in (*_fixed_now(self.NOW), patch.dict(os.environ, {"ESSL_PUBLIC_URL": "https://hub.example.com"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.asha = self._person("118", "Asha Rao")
        self.boss = self._person("200", "Meera Boss", "manager")
        self.hari = self._person("300", "Hari HR", "hr")
        self.admin = self._person("400", "Ada Admin", "admin")
        self.database.set_employee_manager(self.asha["id"], self.boss["id"])
        # 09:00-13:00, then an Out at 17:00 with no In before it: 4h 00m.
        self.database.insert_punches([
            _punch_row("a-in", datetime(2026, 10, 26, 9, 0), "entry", uid="118"),
            _punch_row("a-out", datetime(2026, 10, 26, 13, 0), "exit", uid="118"),
            _punch_row("a-last", datetime(2026, 10, 26, 17, 0), "exit", uid="118"),
        ])

    def _person(self, uid: str, name: str, role: str = "employee") -> dict:
        email = f"{name.split()[0].lower()}@example.com"
        self.database.upsert_employee(device_user_id=uid, name=name, email=email)
        person = self.database.employee_by_email(email)
        self.database.set_employee_role(person["id"], role)
        return person

    def _sign_in(self, person: dict, client=None) -> None:
        client = client or self.client
        client.post("/signout")
        client.post("/signin", data={"email": person["email"]})
        client.post("/signin/verify", data={"code": self.captured["code"]})

    def _csrf(self, client=None) -> str:
        html = (client or self.client).get("/day").text
        marker = 'name="csrf-token" content="'
        start = html.index(marker) + len(marker)
        return html[start : html.index('"', start)]

    def _file(self, *lines, person=None) -> int:
        from essl_hub.db import device_ids_of
        from essl_hub.routes.fixes import punch_set_hash

        person = person or self.asha
        start, end = datetime(2026, 10, 26, 6), datetime(2026, 10, 27, 6)
        rows = self.database.punches_for_employee_range(device_ids_of(person), start, end)
        lines = lines or ({"kind": "add_in", "event_time": datetime(2026, 10, 26, 13, 40)},)
        return self.database.file_fix_lines(person["id"], self.DAY, list(lines), source="link",
                                            punch_set_hash=punch_set_hash(rows))["request_id"]

    def _lines(self, request_id: int) -> list[dict]:
        return self.database.fix_request(request_id)["lines"]

    def _approve_all(self, request_id: int) -> dict:
        form = {}
        for line in self._lines(request_id):
            form[f"line-{line['id']}-decision"] = "approved"
            if line["kind"] in ("add_in", "add_out"):
                form[f"line-{line['id']}-time"] = line["event_time"].strftime("%H:%M")
            if line["kind"] != "ignore":
                form[f"line-{line['id']}-dir"] = {"entry": "in", "exit": "out"}.get(line["direction"], "in")
        return form

    def _post(self, request_id: int, form: dict, step: str = "", client=None):
        client = client or self.client
        return client.post(f"/fixes/review/{request_id}{step}",
                           data={"csrf": self._csrf(client), **form})

    def _flash(self, client=None) -> str:
        return (client or self.client).get("/fixes/review").text

    def _decision_mail(self) -> dict:
        mails = [m for m in self.database.outbox if m["recipients"] == ["asha@example.com"]]
        self.assertEqual(len(mails), 1)
        return mails[0]

    # --- Approving ----------------------------------------------------------

    def test_review_detail_has_fix_requests_current_and_report_form_today(self) -> None:
        request_id = self._file()
        self._sign_in(self.hari)
        detail = self.client.get(f"/fixes/review/{request_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(_current_nav(detail.text), ["Fix requests"])
        self._sign_in(self.asha)
        report = self.client.get(f"/day/{self.DAY.isoformat()}/report")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(_current_nav(report.text), ["Today"])

    def test_approving_a_missed_in_corrects_hours_audits_and_emails(self) -> None:
        request_id = self._file()
        self._sign_in(self.hari)
        detail = self.client.get(f"/fixes/review/{request_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Missed swipe-in at 13:40", detail.text)
        self.assertIn("Waiting for review", detail.text)
        self.assertIn("17:00:00", detail.text)
        self.assertIn('value="approved" checked', detail.text)
        self.assertNotIn("Day changed since report", detail.text)
        form = self._approve_all(request_id)
        preview = self._post(request_id, form)
        self.assertEqual(preview.status_code, 200)
        self.assertIn("Confirm decision", preview.text)
        self.assertIn("4h 00m", preview.text)
        self.assertIn("7h 20m", preview.text)
        self.assertEqual(self.database.corrections, [])  # the preview writes nothing
        response = self._post(request_id, form, "/confirm")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/fixes/review"))
        self.assertIn("Saved. 1 line approved, 0 rejected.", self._flash())
        [correction] = self.database.corrections
        self.assertEqual((correction["kind"], correction["event_timestamp"], correction["direction"]),
                         ("add", datetime(2026, 10, 26, 13, 40), "entry"))
        self.assertEqual(self.database.fix_request(request_id)["status"], "approved")
        approved = [e for e in self.database.fix_request_events(request_id) if e["kind"] == "line_approved"]
        self.assertEqual([e["actor_name"] for e in approved], ["Hari HR"])
        mail = self._decision_mail()
        self.assertIn("was approved", mail["subject"])
        self.assertIn("Missed swipe-in at 13:40: approved", mail["text_body"])
        self.assertIn("https://hub.example.com/day?date=2026-10-26", mail["text_body"])
        day = self.client.get(f"/day?employee={self.asha['id']}&date=2026-10-26").text
        self.assertIn("7h 20m", day)
        self.assertIn("added by HR", day)

    def test_hr_edit_of_the_time_is_applied_and_audited_with_both_values(self) -> None:
        request_id = self._file()
        self._sign_in(self.hari)
        form = {**self._approve_all(request_id), f"line-{self._lines(request_id)[0]['id']}-time": "13:35"}
        preview = self._post(request_id, form)
        self.assertIn("Missed swipe-in at 13:35", preview.text)
        self.assertIn("Employee asked: Missed swipe-in at 13:40", preview.text)
        self.assertIn("7h 25m", preview.text)
        self.assertEqual(self._post(request_id, form, "/confirm").status_code, 302)
        [correction] = self.database.corrections
        self.assertEqual(correction["event_timestamp"], datetime(2026, 10, 26, 13, 35))
        [line] = self._lines(request_id)
        self.assertEqual(line["hr_event_time"], datetime(2026, 10, 26, 13, 35))
        [event] = [e for e in self.database.fix_request_events(request_id) if e["kind"] == "line_approved"]
        self.assertEqual(event["old_value"]["event_time"], datetime(2026, 10, 26, 13, 40))
        self.assertEqual(event["new_value"]["correction"]["event_timestamp"], datetime(2026, 10, 26, 13, 35))
        self.assertIn("Missed swipe-in at 13:35: approved", self._decision_mail()["text_body"])

    def test_change_decisions_returns_to_the_form_with_the_edits(self) -> None:
        request_id = self._file()
        self._sign_in(self.hari)
        line_id = self._lines(request_id)[0]["id"]
        form = {**self._approve_all(request_id), f"line-{line_id}-time": "13:35", "action": "edit"}
        page = self._post(request_id, form)
        self.assertEqual(page.status_code, 200)
        self.assertIn('value="13:35"', page.text)
        self.assertEqual(self.database.corrections, [])

    def test_day_changed_since_report_warns_and_still_approves(self) -> None:
        request_id = self._file()
        self.database.insert_punches([_punch_row("a-new", datetime(2026, 10, 26, 15, 0), "entry", uid="118")])
        self._sign_in(self.hari)
        self.assertIn("Day changed since report", self.client.get(f"/fixes/review/{request_id}").text)
        self.assertEqual(self._post(request_id, self._approve_all(request_id), "/confirm").status_code, 302)
        self.assertEqual(self.database.fix_request(request_id)["status"], "approved")
        self.assertNotIn("Day changed since report", self.client.get(f"/fixes/review/{request_id}").text)

    def test_a_line_whose_swipe_is_gone_is_flagged(self) -> None:
        request_id = self._file({"kind": "flip", "target_fingerprint": "a-last", "direction": "entry",
                                 "event_time": datetime(2026, 10, 26, 17, 0)})
        self.database.punches = [p for p in self.database.punches if p["fingerprint"] != "a-last"]
        self._sign_in(self.hari)
        self.assertIn("The swipe this line points to no longer exists",
                      self.client.get(f"/fixes/review/{request_id}").text)

    def test_bad_time_and_long_note_rerender_the_form(self) -> None:
        request_id = self._file()
        self._sign_in(self.hari)
        line_id = self._lines(request_id)[0]["id"]
        bad = self._post(request_id, {**self._approve_all(request_id), f"line-{line_id}-time": "25:99"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("Enter a time like 13:40.", bad.text)
        long_note = self._post(request_id, {**self._approve_all(request_id), "note": "x" * 501}, "/confirm")
        self.assertEqual(long_note.status_code, 400)
        self.assertIn("Keep the note to 500 characters", long_note.text)
        self.assertEqual(self.database.corrections, [])

    # --- Rejecting ----------------------------------------------------------

    def test_reject_needs_a_note_and_the_note_reaches_the_employee(self) -> None:
        request_id = self._file()
        self._sign_in(self.hari)
        line_id = self._lines(request_id)[0]["id"]
        form = {f"line-{line_id}-decision": "rejected", f"line-{line_id}-time": "13:40", f"line-{line_id}-dir": "in"}
        for step in ("", "/confirm"):
            refused = self._post(request_id, form, step)
            self.assertEqual(refused.status_code, 400)
            self.assertIn("Add a note for the employee", refused.text)
        self.assertEqual(self.database.fix_request(request_id)["status"], "pending")
        form["note"] = "The reader log shows you outside until 15:00."
        self.assertEqual(self._post(request_id, form, "/confirm").status_code, 302)
        self.assertIn("Saved. 0 lines approved, 1 rejected.", self._flash())
        self.assertEqual(self.database.corrections, [])
        self.assertEqual(self.database.fix_request(request_id)["status"], "rejected")
        # Alert suppression reads the day's request statuses: rejected never re-alerts.
        self.assertEqual(self.database.fix_request_days([self.asha["id"]], self.DAY, self.DAY),
                         {(self.asha["id"], self.DAY): ["rejected"]})
        mail = self._decision_mail()
        self.assertIn("wasn't approved", mail["subject"])
        self.assertIn("The reader log shows you outside until 15:00.", mail["text_body"])

    def test_mixed_decisions_give_partial(self) -> None:
        request_id = self._file({"kind": "add_in", "event_time": datetime(2026, 10, 26, 13, 40)},
                                {"kind": "ignore", "target_fingerprint": "a-in",
                                 "event_time": datetime(2026, 10, 26, 9, 0)})
        self._sign_in(self.hari)
        add, ignore = self._lines(request_id)
        form = {**self._approve_all(request_id), f"line-{ignore['id']}-decision": "rejected", "note": "Keep 09:00."}
        self.assertEqual(self._post(request_id, form, "/confirm").status_code, 302)
        self.assertEqual(self.database.fix_request(request_id)["status"], "partial")
        self.assertEqual([c["kind"] for c in self.database.corrections], ["add"])
        self.assertIn("Saved. 1 line approved, 1 rejected.", self._flash())
        self.assertIn("partly approved", self._decision_mail()["subject"])

    # --- Who can approve ----------------------------------------------------

    def test_hr_sees_their_own_request_read_only(self) -> None:
        self.database.insert_punches([_punch_row("h-in", datetime(2026, 10, 26, 9, 0), "entry", uid="300")])
        request_id = self._file(person=self.hari)
        self._sign_in(self.hari)
        page = self.client.get(f"/fixes/review/{request_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("This is your own request", page.text)
        self.assertNotIn('name="line-', page.text)
        form = self._approve_all(request_id)
        self.assertEqual(self._post(request_id, form).status_code, 403)
        self.assertEqual(self._post(request_id, form, "/confirm").status_code, 403)
        self.assertEqual(self.database.corrections, [])
        # Hari is the only HR user, so the admin decides it.
        self._sign_in(self.admin)
        self.assertIn('name="line-', self.client.get(f"/fixes/review/{request_id}").text)
        self.assertEqual(self._post(request_id, form, "/confirm").status_code, 302)
        self.assertEqual(self.database.fix_request(request_id)["status"], "approved")

    def test_admin_approves_while_an_hr_user_exists_and_is_recorded_as_the_decider(self) -> None:
        request_id = self._file()
        self._sign_in(self.admin)
        page = self.client.get(f"/fixes/review/{request_id}")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("View only", page.text)
        self.assertIn('name="line-', page.text)
        form = self._approve_all(request_id)
        self.assertEqual(self._post(request_id, form, "/confirm").status_code, 302)
        filed = self.database.fix_request(request_id)
        self.assertEqual(filed["status"], "approved")
        self.assertEqual(filed["decided_by"], self.admin["id"])

    def test_admin_never_approves_their_own_request(self) -> None:
        self.database.set_employee_role(self.hari["id"], "employee")
        request_id = self._file(person=self.admin)
        self._sign_in(self.admin)
        self.assertIn("This is your own request", self.client.get(f"/fixes/review/{request_id}").text)
        self.assertEqual(self._post(request_id, self._approve_all(request_id), "/confirm").status_code, 403)

    def test_managers_and_employees_get_403(self) -> None:
        request_id = self._file()
        for person in (self.boss, self.asha):
            self._sign_in(person)
            csrf = self._csrf()
            self.assertEqual(self.client.get("/fixes/review").status_code, 403)
            self.assertEqual(self.client.get(f"/fixes/review/{request_id}").status_code, 403)
            for step in ("", "/confirm"):
                response = self.client.post(f"/fixes/review/{request_id}{step}",
                                            data={"csrf": csrf, **self._approve_all(request_id)})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database.corrections, [])

    def test_confirm_needs_csrf(self) -> None:
        request_id = self._file()
        self._sign_in(self.hari)
        response = self.client.post(f"/fixes/review/{request_id}/confirm", data=self._approve_all(request_id))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.database.corrections, [])

    # --- Concurrency --------------------------------------------------------

    def test_two_hr_users_confirming_the_same_line_make_one_correction(self) -> None:
        gita = self._person("301", "Gita HR", "hr")
        request_id = self._file()
        other = self.app.test_client()
        self._sign_in(self.hari)
        self._sign_in(gita, client=other)
        form = self._approve_all(request_id)
        self.assertEqual(self._post(request_id, form, "/confirm").status_code, 302)
        self.assertEqual(self._post(request_id, form, "/confirm", client=other).status_code, 302)
        self.assertEqual(len(self.database.corrections), 1)
        page = self._flash(client=other)
        self.assertIn("Nothing was saved.", page)
        self.assertIn("already decided by Hari HR", page)
        self.assertEqual(len([m for m in self.database.outbox if m["recipients"] == ["asha@example.com"]]), 1)

    # --- List and nav -------------------------------------------------------

    def test_list_shows_pending_then_decisions_and_empty_states(self) -> None:
        self._sign_in(self.hari)
        page = self.client.get("/fixes/review").text
        self.assertIn("No fix requests waiting.", page)
        self.assertIn("No decisions yet.", page)
        request_id = self._file()
        page = self.client.get("/fixes/review").text
        self.assertIn("Asha Rao", page)
        self.assertIn(f'href="/fixes/review/{request_id}"', page)
        self.assertIn("Waiting for review", page)
        self.assertNotIn("No fix requests waiting.", page)
        self._post(request_id, self._approve_all(request_id), "/confirm")
        page = self.client.get("/fixes/review").text
        self.assertIn("No fix requests waiting.", page)
        self.assertIn('class="chip chip-ok">Approved</span>', page)

    def test_nav_badge_counts_pending_requests_for_hr_and_admin(self) -> None:
        self._file()
        self.database.insert_punches([_punch_row("h-in", datetime(2026, 10, 26, 9, 0), "entry", uid="300")])
        self._file(person=self.hari)
        for person in (self.hari, self.admin):
            self._sign_in(person)
            nav = self.client.get("/day").text.split('<nav aria-label="Main">')[1].split("</nav>")[0]
            self.assertIn('aria-label="Fix requests, 2 pending"', nav)
            self.assertIn('aria-hidden="true">2</span>', nav)
        self._sign_in(self.asha)
        self.assertNotIn("Fix requests", self.client.get("/day").text)

    # --- Sign-in deep link --------------------------------------------------

    def test_signed_out_hr_follows_the_email_link_to_the_request(self) -> None:
        request_id = self._file()
        path = f"/fixes/review/{request_id}"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 302)
        signin_url = response.headers["Location"]
        self.assertIn("/signin?next=", signin_url)
        self.assertIn(f'name="next" value="{path}"', self.client.get(signin_url).text)
        code_page = self.client.post("/signin", data={"email": self.hari["email"], "next": path})
        self.assertIn(f'name="next" value="{path}"', code_page.text)
        verify = self.client.post("/signin/verify", data={"code": self.captured["code"], "next": path})
        self.assertEqual(verify.status_code, 302)
        self.assertEqual(verify.headers["Location"], path)
        self.assertIn("Missed swipe-in at 13:40", self.client.get(path).text)

    def test_off_site_next_is_ignored(self) -> None:
        for target in ("https://evil.example.com/x", "//evil.example.com/x", "/\t/evil.example.com"):
            with self.subTest(target=target):
                self.client.post("/signout")
                page = self.client.get("/signin", query_string={"next": target}).text
                self.assertNotIn('name="next"', page)
                self.client.post("/signin", data={"email": self.hari["email"], "next": target})
                verify = self.client.post("/signin/verify", data={"code": self.captured["code"], "next": target})
                self.assertEqual(verify.status_code, 302)
                self.assertEqual(verify.headers["Location"], "/team")

    def test_wrong_code_keeps_next_and_the_right_code_then_lands_on_it(self) -> None:
        path = f"/fixes/review/{self._file()}"
        self.client.post("/signin", data={"email": self.hari["email"], "next": path})
        code = self.captured["code"]
        wrong = "0" * len(code) if code != "0" * len(code) else "1" * len(code)
        page = self.client.post("/signin/verify", data={"code": wrong, "next": path})
        self.assertEqual(page.status_code, 200)
        self.assertIn("That code is not correct", page.text)
        self.assertIn(f'name="next" value="{path}"', page.text)
        verify = self.client.post("/signin/verify", data={"code": code, "next": path})
        self.assertEqual(verify.status_code, 302)
        self.assertEqual(verify.headers["Location"], path)

    def test_signed_in_visit_to_signin_with_next_goes_there(self) -> None:
        self._sign_in(self.hari)
        response = self.client.get("/signin", query_string={"next": "/fixes/review"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/fixes/review")

    def test_signed_out_post_redirects_to_plain_signin(self) -> None:
        request_id = self._file()
        response = self.client.post(f"/fixes/review/{request_id}/confirm",
                                    data=self._approve_all(request_id))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/signin")
        self.assertEqual(self.database.fix_request(request_id)["status"], "pending")

    def test_deep_link_keeps_its_query_string(self) -> None:
        response = self.client.get("/day", query_string={"date": "2026-10-20"})
        self.assertEqual(response.status_code, 302)
        signin_url = response.headers["Location"]
        self.assertIn("next=", signin_url)
        target = "/day?date=2026-10-20"
        self.assertIn(f'name="next" value="{target}"', self.client.get(signin_url).text)
        self.client.post("/signin", data={"email": self.hari["email"], "next": target})
        verify = self.client.post("/signin/verify", data={"code": self.captured["code"], "next": target})
        self.assertEqual(verify.headers["Location"], target)

    def test_a_failed_decision_email_still_saves_and_redirects(self) -> None:
        request_id = self._file()
        self._sign_in(self.hari)
        form = self._approve_all(request_id)
        with patch.object(self.database, "enqueue_mail", side_effect=RuntimeError("smtp down")), \
                self.assertLogs("essl_hub.routes.fixes", level="ERROR"):
            response = self._post(request_id, form, "/confirm")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/fixes/review"))
        self.assertEqual(self.database.fix_request(request_id)["status"], "approved")
        self.assertIn("Saved. 1 line approved, 0 rejected.", self._flash())

    # --- Admin direct fixes (v10): the form, confirm-then-apply, who may use them -----

    def _url(self, person=None, step: str = "", day=None) -> str:
        return f"/fixes/direct/{(person or self.asha)['id']}/{(day or self.DAY).isoformat()}{step}"

    def _form(self, **extra) -> dict:
        return {"reason": "Badge log shows he arrived at 13:40", **extra}

    def _fp(self, uid: str, name: str) -> str:
        return next(p["fingerprint"] for p in self.database.punches if p["fingerprint"] == name)

    def _fix(self, step: str, form: dict, client=None, person=None):
        client = client or self.client
        return client.post(self._url(person, step), data={"csrf": self._csrf(client), **form})

    def test_admin_sees_a_form_listing_the_punches_and_a_button_on_their_day(self) -> None:
        self._sign_in(self.admin)
        page = self.client.get(self._url())
        self.assertEqual(page.status_code, 200)
        for name in ("act-a-in", "act-a-out", "act-a-last"):
            self.assertIn(f'name="{name}"', page.text)
        self.assertIn('name="reason"', page.text)
        self.assertIn('name="add-0-time"', page.text)
        day = self.client.get(f"/day?employee={self.asha['id']}&date=2026-10-26")
        self.assertIn("Fix punches", day.text)
        self.assertIn(self._url(), day.text)

    def test_only_an_admin_may_open_it_and_never_for_their_own_day(self) -> None:
        # Three sign-ins at most per test: the code limiter blocks a fourth.
        for person in (self.hari, self.boss):
            with self.subTest(person=person["name"]):
                self._sign_in(person)
                self.assertEqual(self.client.get(self._url()).status_code, 403)
                self.assertEqual(self._fix("", self._form()).status_code, 403)
                self.assertNotIn("Fix punches", self.client.get(f"/day?employee={self.asha['id']}&date=2026-10-26").text)
        self._sign_in(self.admin)
        self.assertEqual(self.client.get(self._url(self.admin)).status_code, 403)
        self.assertEqual(self._fix("/confirm", self._form(), person=self.admin).status_code, 403)
        self.assertNotIn("Fix punches", self.client.get("/day?date=2026-10-26").text)
        self.assertEqual(self.database.corrections, [])

    def test_unknown_future_and_archived_targets_are_not_found(self) -> None:
        self._sign_in(self.admin)
        self.assertEqual(self.client.get("/fixes/direct/9999/2026-10-26").status_code, 404)
        self.assertEqual(self.client.get(self._url(day=self.DAY + timedelta(days=3))).status_code, 404)
        self.assertEqual(self.client.get("/fixes/direct/%d/not-a-day" % self.asha["id"]).status_code, 404)
        self.database.employees_by_id[self.asha["id"]]["archived_at"] = datetime(2026, 10, 26, 12, 0)
        self.assertEqual(self.client.get(self._url()).status_code, 404)

    def test_preview_shows_hours_before_and_after_and_writes_nothing(self) -> None:
        self._sign_in(self.admin)
        # 09:00 In, 13:00 Out, 17:00 Out (orphan): 4h 00m. Counting 17:00 as In adds nothing, but a
        # missed In at 13:40 makes 13:40-17:00 a stretch: 7h 20m.
        preview = self._fix("", self._form(**{"add-0-time": "13:40", "add-0-dir": "in"}))
        self.assertEqual(preview.status_code, 200)
        self.assertIn("Confirm fix", preview.text)
        self.assertIn("4h 00m", preview.text)
        self.assertIn("7h 20m", preview.text)
        self.assertIn("Add a missed punch: In at 13:40", preview.text)
        self.assertIn("Badge log shows he arrived at 13:40", preview.text)
        self.assertEqual(self.database.corrections, [])

    def test_confirm_records_the_admin_the_reason_and_the_path(self) -> None:
        self._sign_in(self.admin)
        form = self._form(**{"act-a-last": "ignore", "add-0-time": "13:40", "add-0-dir": "in"})
        preview = self._fix("", form)
        hidden = {"punch_set_hash": self._hash_of(preview.text)}
        response = self._fix("/confirm", {**form, **hidden})
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/day?employee={self.asha['id']}&date=2026-10-26", response.headers["Location"])
        kinds = sorted((c["kind"], c["fingerprint"], c["direction"]) for c in self.database.corrections)
        self.assertEqual(kinds, [("add", None, "entry"), ("ignore", "a-last", None)])
        for correction in self.database.corrections:
            self.assertEqual(
                (correction["created_by"], correction["created_by_name"], correction["created_by_role"],
                 correction["path"], correction["request_line_id"], correction["reason"]),
                (self.admin["id"], "Ada Admin", "admin", "direct", None,
                 "Badge log shows he arrived at 13:40"))
        day = self.client.get(f"/day?employee={self.asha['id']}&date=2026-10-26").text
        # 09:00-13:00 (4h) plus the added In at 13:40, open until 18:00 now: 8h 20m.
        self.assertIn("8h 20m", day)

    def test_a_flip_sets_an_absolute_direction_even_on_a_direction_less_punch(self) -> None:
        self.database.insert_punches([{**_punch_row("x-1", datetime(2026, 10, 26, 18, 0), None, uid="118")}])
        self._sign_in(self.admin)
        form = self._form(**{"act-x-1": "out"})
        response = self._fix("/confirm", {**form, "punch_set_hash": self._hash_of(self._fix("", form).text)})
        self.assertEqual(response.status_code, 302)
        [correction] = self.database.corrections
        self.assertEqual((correction["kind"], correction["fingerprint"], correction["direction"]),
                         ("set_direction", "x-1", "exit"))

    def test_validation_refuses_without_writing(self) -> None:
        self._sign_in(self.admin)
        cases = {
            "no reason": ({"reason": "", "act-a-last": "ignore"}, "Say why you are changing"),
            "no change": (self._form(), "Choose at least one change"),
            "add without a direction": (self._form(**{"add-0-time": "13:40"}), "Choose In or Out"),
            "add without a time": (self._form(**{"add-0-dir": "in"}), "Enter the time"),
            "add in the future": (self._form(**{"add-0-time": "23:00", "add-0-dir": "in"}), "happened yet"),
            "a punch that is not on the day": (self._form(**{"act-nope": "ignore"}), "no longer on the day"),
            "a bad action": (self._form(**{"act-a-in": "explode"}), "Choose In, Out or Ignore"),
        }
        for label, (form, message) in cases.items():
            with self.subTest(label):
                for step in ("", "/confirm"):
                    response = self._fix(step, form)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(message, response.text)
        self.assertEqual(self.database.corrections, [])

    def test_a_day_that_changed_since_the_preview_is_refused(self) -> None:
        self._sign_in(self.admin)
        form = self._form(**{"act-a-last": "ignore"})
        stale = self._hash_of(self._fix("", form).text)
        self.database.insert_punches([_punch_row("late", datetime(2026, 10, 26, 17, 30), "entry", uid="118")])
        response = self._fix("/confirm", {**form, "punch_set_hash": stale})
        self.assertEqual(response.status_code, 409)
        self.assertIn("changed after you opened this page", response.text)
        self.assertEqual(self.database.corrections, [])

    def test_forged_actor_fields_in_the_form_are_ignored(self) -> None:
        self._sign_in(self.admin)
        form = self._form(**{"act-a-last": "ignore", "created_by": str(self.hari["id"]),
                             "created_by_role": "hr", "path": "request", "created_by_name": "Hari HR"})
        self._fix("/confirm", {**form, "punch_set_hash": self._hash_of(self._fix("", form).text)})
        [correction] = self.database.corrections
        self.assertEqual((correction["created_by"], correction["created_by_role"], correction["path"]),
                         (self.admin["id"], "admin", "direct"))

    def test_bad_csrf_is_refused(self) -> None:
        self._sign_in(self.admin)
        response = self.client.post(self._url(), data={"csrf": "wrong", **self._form(**{"act-a-last": "ignore"})})
        self.assertIn(response.status_code, (400, 403))
        self.assertEqual(self.database.corrections, [])

    @staticmethod
    def _hash_of(html: str) -> str:
        marker = 'name="punch_set_hash" value="'
        start = html.index(marker) + len(marker)
        return html[start: html.index('"', start)]



class CopyAndPhoneLayoutTest(unittest.TestCase):
    """U6 (chips and copy) and U7 (phone layouts). Today is Wednesday
    2026-10-28, mid-morning; Asha reports to Meera."""

    NOW = datetime(2026, 10, 28, 12, 0)

    def setUp(self) -> None:
        self.app, self.database = _make_app()
        self.client = self.app.test_client()
        from essl_hub import mail as mail_module

        self.captured = {}
        original = mail_module.send_login_code
        mail_module.send_login_code = lambda e, c: self.captured.update(code=c)
        self.addCleanup(lambda: setattr(mail_module, "send_login_code", original))
        for patcher in _fixed_now(self.NOW):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.asha = self._person("118", "Asha Rao")
        self.boss = self._person("200", "Meera Boss", "manager")
        self.hari = self._person("300", "Hari HR", "hr")
        self.database.set_employee_manager(self.asha["id"], self.boss["id"])

    def _person(self, uid: str, name: str, role: str = "employee") -> dict:
        email = f"{name.split()[0].lower()}@example.com"
        self.database.upsert_employee(device_user_id=uid, name=name, email=email)
        person = self.database.employee_by_email(email)
        self.database.set_employee_role(person["id"], role)
        return person

    def _sign_in(self, person: dict) -> None:
        self.client.post("/signout")
        self.client.post("/signin", data={"email": person["email"]})
        self.client.post("/signin/verify", data={"code": self.captured["code"]})

    def _flag(self, day: datetime) -> None:
        """A day with an entry that has no exit after it: needs review."""
        tag = day.date().isoformat()
        self.database.insert_punches([
            _punch_row(f"{tag}-1", day + timedelta(hours=9), "entry"),
            _punch_row(f"{tag}-2", day + timedelta(hours=14, minutes=33), "entry"),
            _punch_row(f"{tag}-3", day + timedelta(hours=18), "exit"),
        ])

    @staticmethod
    def _notice(page: str) -> str:
        return re.search(r'<div class="notice review-notice">.*?</div>', page, re.S).group(0)

    # --- U6 -----------------------------------------------------------------

    def test_needs_review_chip_is_a_warn_chip_not_the_review_flag(self) -> None:
        self._flag(datetime(2026, 10, 28))
        self._sign_in(self.hari)
        for path in ("/people", "/team"):
            with self.subTest(path=path):
                page = self.client.get(path).text
                self.assertIn('<span class="chip chip-warn" title="No exit after 09:00">Needs review', page)
                self.assertNotIn("review-flag", page)

    def test_target_reached_line_says_they_to_others_and_you_to_the_person(self) -> None:
        self.database.insert_punches([_punch_row("live-in", datetime(2026, 10, 28, 9), "entry")])
        self._sign_in(self.asha)
        own = self.client.get("/day").text
        self.assertIn("</strong> if you stay in.", own)
        self._sign_in(self.hari)
        other = self.client.get(f"/day?employee={self.asha['id']}").text
        self.assertIn("</strong> if they stay in.", other)
        self.assertNotIn("if you stay in", other)

    def test_own_recent_flagged_day_points_to_report_a_problem(self) -> None:
        self._flag(datetime(2026, 10, 26))
        self._sign_in(self.asha)
        page = self.client.get("/day?date=2026-10-26").text
        notice = self._notice(page)
        self.assertIn('Use <a href="/day/2026-10-26/report">Report a problem</a> to tell HR what happened.', notice)
        self.assertNotIn("Ask HR", page)
        self.assertNotIn("too old", page)

    def test_own_ten_day_old_flagged_day_says_too_old_to_report(self) -> None:
        self._flag(datetime(2026, 10, 18))
        self._sign_in(self.asha)
        page = self.client.get("/day?date=2026-10-18").text
        self.assertIn("This day is too old to report here — ask HR.", self._notice(page))
        self.assertNotIn("/day/2026-10-18/report", page)

    def test_manager_view_of_a_flagged_day_keeps_the_hr_line(self) -> None:
        self._flag(datetime(2026, 10, 26))
        self._sign_in(self.boss)
        page = self.client.get(f"/day?employee={self.asha['id']}&date=2026-10-26").text
        notice = self._notice(page)
        self.assertIn("Ask HR to record an explanation.", notice)
        self.assertNotIn("Report a problem", notice)

    # --- U7 -----------------------------------------------------------------

    def test_week_marks_future_days_only(self) -> None:
        self._sign_in(self.asha)
        page = self.client.get("/week").text
        rows = re.findall(r"<tr(?: class=\"([^\"]*)\")?>\s*<td data-label=\"Day\" class=\"week-day\">(.*?)</td>", page, re.S)
        self.assertEqual(len(rows), 5)
        classes = {re.search(r"\w{3} \d\d \w{3}", cell).group(0): cls for cls, cell in rows}
        self.assertEqual(classes, {
            "Mon 26 Oct": "", "Tue 27 Oct": "", "Wed 28 Oct": "is-today",
            "Thu 29 Oct": "is-future", "Fri 30 Oct": "is-future",
        })
        self.assertIn('Thu 29 Oct<span class="week-future-note"> · not yet</span>', page)
        self.assertNotIn('href="/day?date=2026-10-29"', page)

    def test_day_strip_chips_carry_full_status_label_and_a_glyph(self) -> None:
        self.database.insert_punches([_punch_row("strip-1", datetime(2026, 10, 27, 9), "entry")])
        self._sign_in(self.asha)
        page = self.client.get("/day").text
        chips = re.findall(r'<a class="day-chip.*?</a>', page, re.S)
        self.assertEqual(len(chips), 7)
        for chip in chips:
            self.assertRegex(chip, r'aria-label="\w+day \d\d \w+, (Punched|No punches|Today, (punched|no punches))"')
            self.assertRegex(chip, r'<span class="day-chip-dot" aria-hidden="true"><svg class="icon day-chip-glyph"')
            self.assertIn('<span class="day-chip-word">', chip)
        tuesday = next(c for c in chips if "Tuesday 27 October" in c)
        self.assertIn('aria-label="Tuesday 27 October, Punched"', tuesday)
        self.assertIn('href="#i-check"', tuesday)
        today = chips[-1]
        self.assertIn('aria-label="Wednesday 28 October, Today, no punches"', today)
        self.assertIn('href="#i-minus"', today)
