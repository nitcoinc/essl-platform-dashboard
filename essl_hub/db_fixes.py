"""Database methods for fix requests (v8, KTD4-KTD7): filing, closing and
deciding fix lines, their audit events, the HR review lists and approvers,
and the approved corrections the punch reads overlay. ``Database``
(essl_hub.db) inherits FixesMixin; the mixin uses only ``self.connect`` /
``self.transaction`` from it. The overlay read itself (``_overlaid_punches``,
``_corrections_for``) stays in essl_hub.db with the other punch reads.

Every name here stays importable from essl_hub.db.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .db import _Connection

# KTD7 limits, also enforced here so no caller can exceed them.
FIX_MAX_LINES = 10
FIX_NOTE_MAX_CHARS = 500
FIX_ADD_KINDS = ("add_in", "add_out")
FIX_LINE_KINDS = frozenset({*FIX_ADD_KINDS, "flip", "ignore"})
FIX_CLOSED_STATUSES = frozenset({"closed_correct", "closed_left"})
FIX_DECIDED_STATUSES = frozenset({"approved", "partial", "rejected"})
FIX_SOURCES = frozenset({"link", "session"})
FIX_EVENT_KINDS = frozenset(
    {"filed", "lines_added", "closed", "reopened", "line_approved", "line_rejected", "decided"}
)
DIRECTIONS = frozenset({"entry", "exit"})
OPPOSITE_DIRECTION = {"entry": "exit", "exit": "entry"}


def _core():
    """essl_hub.db, imported at call time only: db.py imports this module to
    build Database, so a top-level import would cycle."""
    from . import db

    return db


def _now() -> datetime:
    # Through essl_hub.db at call time, so tests that patch db._now reach
    # these methods as they reach the core ones.
    return _core()._now()


class FixesMixin:
    """Fix requests, their lines, audit events and approvers, mixed into Database."""

    # --- Approvers (KTD6) ----------------------------------------------------

    def active_hr_users(self) -> list[dict[str, object]]:
        """Active role-hr people: the approvers (KTD6) and request recipients."""
        return self._active_with_role("hr")

    def active_admins(self) -> list[dict[str, object]]:
        """Active admins: the fallback recipients when no other HR user exists (KTD6)."""
        return self._active_with_role("admin")

    def _active_with_role(self, role: str) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM employees WHERE role = ? AND archived_at IS NULL ORDER BY name",
                (role,),
            ).fetchall()
            return _core()._attach_device_ids(connection, list(rows))

    # --- Fix requests and corrections (v8, KTD4, KTD5) ---------------------

    def pending_fix_request(self, employee_id: int, work_day: date) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM fix_requests WHERE employee_id = ? AND work_day = ? "
                "AND status = 'pending'",
                (employee_id, work_day),
            ).fetchone()
        return row

    def latest_fix_request(self, employee_id: int, work_day: date) -> dict[str, object] | None:
        with self.connect() as connection:
            return _latest_fix_request(connection, employee_id, work_day)

    def file_fix_lines(
        self,
        employee_id: int,
        work_day: date,
        lines: Iterable[dict],
        *,
        source: str,
        punch_set_hash: str | None = None,
    ) -> dict[str, object]:
        """File an employee's fix lines for one day in one transaction (KTD5):
        they join the pending request, reopen a closed one, or start a new
        request linked to the last decided one. A line the request already
        holds (same kind, swipe, time and direction: a double-submitted form)
        is skipped, and nothing is written when every line is. Raises
        FixLineLimit past FIX_MAX_LINES and ValueError for a malformed line.
        Returns request_id and first_lines, True when the request had no lines
        before, so the caller notifies the manager and HR once (U7)."""
        if source not in FIX_SOURCES:
            raise ValueError(f"unknown fix request source: {source}")
        prepared = [_fix_line_values(line) for line in lines]
        if not prepared:
            raise ValueError("a fix request needs at least one line")
        now = _now()
        with self.transaction() as connection:
            latest = _latest_fix_request(connection, employee_id, work_day)
            if latest is not None and latest["status"] in FIX_CLOSED_STATUSES | {"pending"}:
                request_id = latest["id"]
                rows = connection.execute(
                    "SELECT kind, target_fingerprint, event_time, direction "
                    "FROM fix_request_lines WHERE request_id = ?",
                    (request_id,),
                ).fetchall()
                existing = len(rows)
                held = {_fix_line_key(row) for row in rows}
                prepared = [values for values in prepared if _fix_line_key(values) not in held]
                if not prepared:
                    return {"request_id": request_id, "first_lines": False}
                event = "lines_added"
                if latest["status"] != "pending":
                    # A reopened request is a new report: it queues from now.
                    connection.execute(
                        "UPDATE fix_requests SET status = 'pending', source = ?, punch_set_hash = ?, "
                        "decided_at = NULL, decided_by = NULL, created_at = ? WHERE id = ?",
                        (source, punch_set_hash, now, request_id),
                    )
                    _insert_fix_event(
                        connection, request_id, None, employee_id, "reopened",
                        {"status": latest["status"]}, {"status": "pending"},
                    )
            else:
                existing = 0
                event = "filed"
                request_id = connection.execute(
                    "INSERT INTO fix_requests (employee_id, work_day, status, punch_set_hash, "
                    "source, previous_request_id, created_at) VALUES (?, ?, 'pending', ?, ?, ?, ?)",
                    (
                        employee_id, work_day, punch_set_hash, source,
                        latest["id"] if latest else None, now,
                    ),
                ).lastrowid
            if existing + len(prepared) > FIX_MAX_LINES:
                raise _core().FixLineLimit(f"A request holds at most {FIX_MAX_LINES} lines.")
            line_ids = [
                connection.execute(
                    "INSERT INTO fix_request_lines (request_id, kind, target_fingerprint, "
                    "event_time, direction, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (request_id, *values, now),
                ).lastrowid
                for values in prepared
            ]
            _insert_fix_event(
                connection, request_id, None, employee_id, event, None, {"lines": line_ids}
            )
        return {"request_id": request_id, "first_lines": existing == 0}

    def close_fix_day(self, employee_id: int, work_day: date, status: str, *, source: str) -> int:
        """"It was correct" / "I left for the day": close the day without HR
        (R10). A closed day switches reply; a decided one gets a new linked
        request. Raises RuleViolation while a request is pending. Returns
        the request id."""
        if status not in FIX_CLOSED_STATUSES:
            raise ValueError(f"not a closing status: {status}")
        if source not in FIX_SOURCES:
            raise ValueError(f"unknown fix request source: {source}")
        now = _now()
        with self.transaction() as connection:
            latest = _latest_fix_request(connection, employee_id, work_day)
            if latest is not None and latest["status"] == "pending":
                raise _core().RuleViolation("A fix request for this day is waiting for HR.")
            if latest is not None and latest["status"] in FIX_CLOSED_STATUSES:
                request_id = latest["id"]
                connection.execute(
                    "UPDATE fix_requests SET status = ?, source = ?, decided_at = ? WHERE id = ?",
                    (status, source, now, request_id),
                )
            else:
                request_id = connection.execute(
                    "INSERT INTO fix_requests (employee_id, work_day, status, source, "
                    "previous_request_id, created_at, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (employee_id, work_day, status, source, latest["id"] if latest else None, now, now),
                ).lastrowid
            _insert_fix_event(
                connection, request_id, None, employee_id, "closed",
                {"status": latest["status"]} if latest and latest["id"] == request_id else None,
                {"status": status},
            )
        return request_id

    def fix_request(self, request_id: int) -> dict[str, object] | None:
        """One request with the requester, the decider and its ``lines``."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT r.*, e.name AS employee_name, e.email AS employee_email, "
                "e.manager_id AS employee_manager_id, d.name AS decided_by_name "
                "FROM fix_requests r JOIN employees e ON e.id = r.employee_id "
                "LEFT JOIN employees d ON d.id = r.decided_by WHERE r.id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            row["lines"] = connection.execute(
                "SELECT l.*, d.name AS decided_by_name FROM fix_request_lines l "
                "LEFT JOIN employees d ON d.id = l.decided_by "
                "WHERE l.request_id = ? ORDER BY l.id",
                (request_id,),
            ).fetchall()
        return row

    def fix_request_events(self, request_id: int) -> list[dict[str, object]]:
        """Audit rows oldest first, with the actor's name when known."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT v.*, e.name AS actor_name FROM fix_request_events v "
                "LEFT JOIN employees e ON e.id = v.actor_employee_id "
                "WHERE v.request_id = ? ORDER BY v.created_at, v.id",
                (request_id,),
            ).fetchall()
        return list(rows)

    def fix_requests_for_review(self, recent: int = 20) -> list[dict[str, object]]:
        """Pending requests oldest first, then the ``recent`` latest HR
        decisions. Closed days never need HR and are left out."""
        select = (
            "SELECT r.*, e.name AS employee_name, d.name AS decided_by_name, "
            "(SELECT COUNT(*) FROM fix_request_lines l WHERE l.request_id = r.id) AS line_count "
            "FROM fix_requests r JOIN employees e ON e.id = r.employee_id "
            "LEFT JOIN employees d ON d.id = r.decided_by "
        )
        with self.connect() as connection:
            pending = connection.execute(
                select + "WHERE r.status = 'pending' ORDER BY r.created_at, r.id"
            ).fetchall()
            decided_statuses = sorted(FIX_DECIDED_STATUSES)
            decided = connection.execute(
                select + f"WHERE r.status IN ({', '.join('?' * len(decided_statuses))}) "
                "ORDER BY r.decided_at DESC, r.id DESC LIMIT ?",
                (*decided_statuses, recent),
            ).fetchall()
        return [*pending, *decided]

    def pending_fix_request_count(self) -> int:
        with self.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) AS n FROM fix_requests WHERE status = 'pending'"
            ).fetchone()["n"]

    def decide_fix_lines(
        self,
        request_id: int,
        decisions: Iterable[dict],
        *,
        actor_id: int,
        note: str | None = None,
    ) -> dict[str, object]:
        """Apply HR's line decisions in one transaction (R13, R14): each
        line's status and HR edits, a correction per approved line (KTD4),
        an audit row per line, and the request's status once no line is
        pending. ``decisions`` carry line_id, status ('approved' / 'rejected')
        and optional event_time / direction edits. A line decided meanwhile is
        skipped and reported with who decided it. Raises RuleViolation for an
        unknown request or a requester deciding their own, ValueError for a
        rejection without a note or a line outside the request.
        Returns status, decided (line ids) and skipped."""
        decisions = list(decisions)
        note = (note or "").strip() or None
        for decision in decisions:
            if decision.get("status") not in ("approved", "rejected"):
                raise ValueError(f"bad line decision: {decision.get('status')}")
            if decision.get("direction") not in (None, *DIRECTIONS):
                raise ValueError(f"bad direction: {decision.get('direction')}")
        if note is None and any(d["status"] == "rejected" for d in decisions):
            raise ValueError("A note is required when rejecting.")
        if note is not None and len(note) > FIX_NOTE_MAX_CHARS:
            raise ValueError(f"note is longer than {FIX_NOTE_MAX_CHARS} characters")
        now = _now()
        decided: list[int] = []
        skipped: list[dict[str, object]] = []
        with self.transaction() as connection:
            request = connection.execute(
                "SELECT * FROM fix_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if request is None:
                raise _core().RuleViolation("No such fix request.")
            if actor_id == request["employee_id"]:
                raise _core().RuleViolation("Nobody decides their own fix request.")
            # The audit row keeps the actor's name and role as they are now.
            actor = connection.execute(
                "SELECT name, role FROM employees WHERE id = ?", (actor_id,)
            ).fetchone()
            if actor is None:
                raise _core().RuleViolation("Unknown approver.")
            for decision in decisions:
                line = connection.execute(
                    "SELECT l.*, d.name AS decided_by_name FROM fix_request_lines l "
                    "LEFT JOIN employees d ON d.id = l.decided_by "
                    "WHERE l.id = ? AND l.request_id = ?",
                    (decision["line_id"], request_id),
                ).fetchone()
                if line is None:
                    raise ValueError(f"line {decision['line_id']} is not on request {request_id}")
                adds = line["kind"] in FIX_ADD_KINDS
                hr_time = decision.get("event_time") if adds else None
                hr_direction = decision.get("direction") if line["kind"] != "ignore" else None
                cursor = connection.execute(
                    "UPDATE fix_request_lines SET status = ?, hr_event_time = ?, hr_direction = ?, "
                    "decided_by = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
                    (decision["status"], hr_time, hr_direction, actor_id, now, line["id"]),
                )
                if not cursor.rowcount:
                    skipped.append({
                        "line_id": line["id"], "status": line["status"],
                        "decided_by": line["decided_by"], "decided_by_name": line["decided_by_name"],
                    })
                    continue
                correction = None
                if decision["status"] == "approved":
                    correction = _correction_values(connection, line, hr_time, hr_direction)
                    connection.execute(
                        "INSERT INTO punch_corrections (employee_id, work_day, kind, fingerprint, "
                        "event_timestamp, direction, request_line_id, created_at, created_by, "
                        "created_by_name, created_by_role, path, reason) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'request', ?)",
                        (
                            request["employee_id"], request["work_day"], correction["kind"],
                            correction["fingerprint"], correction["event_timestamp"],
                            correction["direction"], line["id"], now, actor_id,
                            actor["name"], actor["role"], note,
                        ),
                    )
                _insert_fix_event(
                    connection, request_id, line["id"], actor_id,
                    "line_approved" if decision["status"] == "approved" else "line_rejected",
                    {
                        "status": "pending", "kind": line["kind"],
                        "target_fingerprint": line["target_fingerprint"],
                        "event_time": line["event_time"], "direction": line["direction"],
                    },
                    {"status": decision["status"], "correction": correction, "note": note},
                )
                decided.append(line["id"])
            if note is not None and decided:
                connection.execute(
                    "UPDATE fix_requests SET decision_note = ? WHERE id = ?", (note, request_id)
                )
            statuses = {
                row["status"]
                for row in connection.execute(
                    "SELECT status FROM fix_request_lines WHERE request_id = ?", (request_id,)
                ).fetchall()
            }
            status = request["status"]
            if decided and statuses and "pending" not in statuses:
                final = (
                    "approved" if statuses == {"approved"}
                    else "rejected" if statuses == {"rejected"}
                    else "partial"
                )
                cursor = connection.execute(
                    "UPDATE fix_requests SET status = ?, decided_at = ?, decided_by = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (final, now, actor_id, request_id),
                )
                if cursor.rowcount:
                    _insert_fix_event(
                        connection, request_id, None, actor_id, "decided",
                        {"status": "pending"}, {"status": final, "note": note},
                    )
                    status = final
        return {"status": status, "decided": decided, "skipped": skipped}

    def corrections_for(
        self, employee_ids: Iterable[int], start: datetime, end: datetime
    ) -> list[dict[str, object]]:
        """Approved corrections that can touch [start, end) for these people
        (KTD4): added punches inside the window, and fingerprint-targeted rows
        whose work day is near it. A fingerprint row whose punch is not in the
        caller's rows has no effect, so the one-day margin only widens the
        read. Oldest first, so a later set_direction wins."""
        with self.connect() as connection:
            return _core()._corrections_for(connection, employee_ids, start, end)

    def fix_request_days(
        self, employee_ids: Iterable[int], start: date, end: date
    ) -> dict[tuple[int, date], list[str]]:
        """Request statuses per (employee, work day) for days in [start, end],
        oldest request first: alert suppression (any request) and clean-day
        exclusion (pending or approved) read this."""
        ids = sorted(set(employee_ids))
        if not ids:
            return {}
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT employee_id, work_day, status FROM fix_requests "
                "WHERE employee_id IN (SELECT value FROM json_each(?)) "
                "AND work_day >= ? AND work_day <= ? ORDER BY id",
                (json.dumps(ids), start, end),
            ).fetchall()
        days: dict[tuple[int, date], list[str]] = {}
        for row in rows:
            days.setdefault((row["employee_id"], row["work_day"]), []).append(row["status"])
        return days


def _insert_fix_event(
    connection: _Connection,
    request_id: int,
    line_id: int | None,
    actor_id: int | None,
    kind: str,
    old_value: dict | None,
    new_value: dict | None,
) -> None:
    """Append a fix-request audit row inside the caller's transaction (R14)."""
    if kind not in FIX_EVENT_KINDS:
        raise ValueError(f"unknown fix request event kind: {kind}")
    connection.execute(
        "INSERT INTO fix_request_events (request_id, line_id, actor_employee_id, kind, "
        "old_value, new_value, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (request_id, line_id, actor_id, kind, old_value, new_value, _now()),
    )


def _latest_fix_request(
    connection: _Connection, employee_id: int, work_day: date
) -> dict[str, object] | None:
    return connection.execute(
        "SELECT * FROM fix_requests WHERE employee_id = ? AND work_day = ? "
        "ORDER BY id DESC LIMIT 1",
        (employee_id, work_day),
    ).fetchone()


def _fix_line_values(line: dict) -> tuple[object, ...]:
    """(kind, target_fingerprint, event_time, direction, note) for one
    employee line. A missed IN/OUT carries a time and its implied direction;
    flip and ignore target a punch by fingerprint (KTD4)."""
    kind = line.get("kind")
    if kind not in FIX_LINE_KINDS:
        raise ValueError(f"unknown fix line kind: {kind}")
    target = line.get("target_fingerprint") or None
    event_time = line.get("event_time")
    direction = line.get("direction")
    note = (line.get("note") or "").strip() or None
    if note is not None and len(note) > FIX_NOTE_MAX_CHARS:
        raise ValueError(f"note is longer than {FIX_NOTE_MAX_CHARS} characters")
    if direction not in (None, *DIRECTIONS):
        raise ValueError(f"bad direction: {direction}")
    if kind in FIX_ADD_KINDS:
        implied = "entry" if kind == "add_in" else "exit"
        if not isinstance(event_time, datetime) or target is not None:
            raise ValueError(f"{kind} needs a time and no target swipe")
        if direction not in (None, implied):
            raise ValueError(f"{kind} cannot carry direction {direction}")
        return (kind, None, event_time, implied, note)
    if target is None:
        raise ValueError(f"{kind} needs a target swipe")
    return (kind, target, event_time, direction if kind == "flip" else None, note)


def _fix_line_key(line: dict | tuple) -> tuple[object, ...]:
    """(kind, target_fingerprint, event_time, direction): what makes two
    lines of one request the same ask. Takes a stored row or the tuple
    _fix_line_values returns."""
    if isinstance(line, dict):
        line = (line["kind"], line["target_fingerprint"], line["event_time"], line["direction"])
    kind, target, event_time, direction = line[:4]
    if isinstance(event_time, datetime):
        # As stored: whole seconds (_adapt_datetime).
        event_time = event_time.replace(microsecond=0)
    return (kind, target, event_time, direction)


def _correction_values(
    connection: _Connection, line: dict, hr_time: datetime | None, hr_direction: str | None
) -> dict[str, object]:
    """The punch_corrections row an approved line becomes (KTD4), with HR's
    edits taking precedence over the employee's values."""
    if line["kind"] in FIX_ADD_KINDS:
        return {
            "kind": "add", "fingerprint": None,
            "event_timestamp": hr_time or line["event_time"],
            "direction": hr_direction or line["direction"],
        }
    if line["kind"] == "ignore":
        return {"kind": "ignore", "fingerprint": line["target_fingerprint"],
                "event_timestamp": None, "direction": None}
    direction = hr_direction or line["direction"]
    if direction is None:
        # No stated direction: the opposite of the stored one, when there is one.
        row = connection.execute(
            "SELECT trusted_direction FROM punches WHERE fingerprint = ?",
            (line["target_fingerprint"],),
        ).fetchone()
        stored = row["trusted_direction"] if row else None
        direction = OPPOSITE_DIRECTION.get(stored)
    if direction is None:
        raise ValueError(f"line {line['id']} needs a direction to flip to")
    return {"kind": "set_direction", "fingerprint": line["target_fingerprint"],
            "event_timestamp": None, "direction": direction}
