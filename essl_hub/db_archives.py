"""Database methods for punch archives (v9, KTD1-KTD3): rules that hide one
device user ID's punches over a range, their restore, and the archive list.
``Database`` (essl_hub.db) inherits ArchivesMixin; the mixin uses only
``self.connect`` / ``self.transaction`` from it. The read-side filter itself
(``_ACTIVE_PUNCH_SQL``) stays in essl_hub.db next to the punch reads it
guards.

A rule never edits a punch row: the collector's fingerprint dedupe still sees
every row, so a re-poll inserts nothing, and restore is one field update.

Every name here stays importable from essl_hub.db.
"""

from __future__ import annotations

from datetime import datetime

ARCHIVE_REASON_MAX_CHARS = 200

# Punches one rule covers, whether or not another rule also hides them.
# Needs the rule aliased ``a`` and the punch ``p``.
_COVERED_SQL = (
    "p.device_user_id = a.device_user_id "
    "AND (a.from_ts IS NULL OR p.event_timestamp >= a.from_ts) "
    "AND p.event_timestamp < a.to_ts"
)


def _core():
    """essl_hub.db, imported at call time only: db.py imports this module to
    build Database, so a top-level import would cycle."""
    from . import db

    return db


def _now() -> datetime:
    # Through essl_hub.db at call time, so tests that patch db._now reach
    # these methods as they reach the core ones.
    return _core()._now()


def archive_range(from_ts: datetime | None, to_ts: datetime | None) -> tuple[datetime | None, datetime]:
    """KTD3: no end means now, and any end is clamped to now, so a swipe made
    after archiving (a reused card) is never hidden. Whole seconds, as stored.
    RuleViolation when the range is empty or reversed."""
    now = _now().replace(microsecond=0)
    to_ts = now if to_ts is None else min(to_ts.replace(microsecond=0), now)
    if from_ts is not None:
        from_ts = from_ts.replace(microsecond=0)
        if from_ts >= to_ts:
            raise _core().RuleViolation("The from date must be before the to date.")
    return from_ts, to_ts


class ArchivesMixin:
    """Punch archive rules (v9), mixed into Database."""

    def archive_preview(
        self, device_user_id: str, from_ts: datetime | None, to_ts: datetime | None
    ) -> dict[str, object]:
        """What archiving would hide right now: the resolved range (so the
        confirm step can carry it and the saved rule matches the count shown)
        and the still-visible punches of this ID inside it."""
        from_ts, to_ts = archive_range(from_ts, to_ts)
        sql = (
            "SELECT COUNT(*) AS n, MIN(p.event_timestamp) AS first_at, "
            "MAX(p.event_timestamp) AS last_at FROM punches p "
            "WHERE p.device_user_id = ? AND p.event_timestamp < ? "
            f"AND {_core()._ACTIVE_PUNCH_SQL}"
        )
        params: list[object] = [device_user_id, to_ts]
        if from_ts is not None:
            sql += " AND p.event_timestamp >= ?"
            params.append(from_ts)
        with self.connect() as connection:
            row = connection.execute(sql, params).fetchone()
        return {
            "device_user_id": device_user_id,
            "from_ts": from_ts,
            "to_ts": to_ts,
            "count": int(row["n"] or 0),
            # MIN()/MAX() drop the column's declared type, so timestamps come back as text.
            "first": _as_datetime(row["first_at"]),
            "last": _as_datetime(row["last_at"]),
        }

    def archive_punches(
        self,
        device_user_id: str,
        *,
        from_ts: datetime | None,
        to_ts: datetime | None,
        reason: str | None = None,
        actor_id: int | None = None,
    ) -> int:
        """Write one archive rule and return its id. Which IDs may be archived
        (unknown and shared only, KTD4) is the route's check."""
        from_ts, to_ts = archive_range(from_ts, to_ts)
        reason = (reason or "").strip()[:ARCHIVE_REASON_MAX_CHARS] or None
        with self.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO punch_archives (device_user_id, from_ts, to_ts, reason, created_by, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (device_user_id, from_ts, to_ts, reason, actor_id, _now()),
            )
            return int(cursor.lastrowid)

    def restore_punch_archive(self, archive_id: int, *, actor_id: int | None = None) -> bool:
        """End one rule; its punches reappear wherever no other active rule
        covers them. False when it is missing or already restored."""
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE punch_archives SET restored_at = ?, restored_by = ? "
                "WHERE id = ? AND restored_at IS NULL",
                (_now(), actor_id, archive_id),
            )
        return cursor.rowcount > 0

    def punch_archive(self, archive_id: int) -> dict[str, object] | None:
        rows = self._archives("WHERE a.id = ?", (archive_id,))
        return rows[0] if rows else None

    def punch_archives(self, include_restored: bool = True) -> list[dict[str, object]]:
        """Active rules first (newest first), then restored ones (most recently
        restored first), with actor names. ``hidden_count`` is the live number
        of punches an active rule covers; None once restored."""
        return self._archives("" if include_restored else "WHERE a.restored_at IS NULL", ())

    def _archives(self, where: str, params: tuple) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT a.*, c.name AS created_by_name, r.name AS restored_by_name,
                    CASE WHEN a.restored_at IS NULL THEN (
                        SELECT COUNT(*) FROM punches p WHERE {_COVERED_SQL}
                    ) END AS hidden_count
                FROM punch_archives a
                LEFT JOIN employees c ON c.id = a.created_by
                LEFT JOIN employees r ON r.id = a.restored_by
                {where}
                ORDER BY a.restored_at IS NOT NULL, COALESCE(a.restored_at, a.created_at) DESC, a.id DESC
                """,
                params,
            ).fetchall()
        return list(rows)

    def active_archive_for(self, device_user_id: str) -> dict[str, int] | None:
        """Active rules on this ID and the punches they hide, for the warning
        when the ID is mapped to a person later; None when nothing is active."""
        with self.connect() as connection:
            rules = connection.execute(
                "SELECT COUNT(*) AS n FROM punch_archives WHERE device_user_id = ? AND restored_at IS NULL",
                (device_user_id,),
            ).fetchone()["n"]
            if not rules:
                return None
            hidden = connection.execute(
                "SELECT COUNT(*) AS n FROM punches p WHERE p.device_user_id = ? "
                f"AND NOT {_core()._ACTIVE_PUNCH_SQL}",
                (device_user_id,),
            ).fetchone()["n"]
        return {"archives": int(rules), "hidden": int(hidden)}


def _as_datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
