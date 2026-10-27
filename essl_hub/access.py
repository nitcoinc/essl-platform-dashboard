"""Every access rule in one place (KTD8).

These are the pure pre-checks that drive routes and UI messages. The writes
that must stay correct under concurrency (last admin, manager cycles) are
re-checked by the guarded ``Database`` methods inside one transaction.
"""

from __future__ import annotations

ROLES = ("employee", "manager", "hr", "admin")
EVERYONE_ROLES = ("hr", "admin")
EDITOR_ROLES = ("hr", "admin")
# Fields of an admin's record that only another admin may change (R14).
ADMIN_PROTECTED_FIELDS = ("email", "device_user_id", "device_ids", "role")
EDITABLE_FIELDS = ("name", "email", "device_user_id", "device_ids", "manager_id", "role")


def visible_employee_ids(database, viewer: dict) -> set[int]:
    """Ids the viewer may see: everyone for hr/admin, own subtree for a
    manager, only themselves otherwise. Having reports without the manager
    role grants nothing (KTD7)."""
    role = viewer["role"]
    if role in EVERYONE_ROLES:
        # Archived people stay visible here so exports keep their past hours;
        # live views list only database.employees() (active).
        return {row["id"] for row in database.employees(include_archived=True)}
    if role == "manager":
        return set(database.subtree_ids(viewer["id"]))
    return {viewer["id"]}


def can_view(database, viewer: dict, target_id: int) -> bool:
    if target_id == viewer["id"]:
        return True
    if viewer["role"] in EVERYONE_ROLES:
        return database.employee_by_id(target_id) is not None
    return target_id in visible_employee_ids(database, viewer)


def can_modify_employee(actor: dict, target: dict, field: str) -> bool:
    if field not in EDITABLE_FIELDS or actor["role"] not in EDITOR_ROLES:
        return False
    if target["role"] == "admin" and field in ADMIN_PROTECTED_FIELDS:
        return actor["role"] == "admin"
    return True


def can_assign_role(actor: dict, target: dict, new_role: str) -> bool:
    """Role grant rules (R15). The last-admin rule needs a count, so the
    guarded Database.set_employee_role enforces it."""
    if new_role not in ROLES:
        return False
    if actor["role"] == "admin":
        return True
    if actor["role"] == "hr":
        return new_role != "admin" and target["role"] != "admin"
    return False


def would_create_cycle(database, employee_id: int, manager_id: int | None) -> bool:
    """True when manager_id is the employee or one of their reports, i.e. the
    employee already sits on the chain above the proposed manager."""
    if manager_id is None:
        return False
    return employee_id in database.manager_chain(manager_id)


def can_archive(actor: dict, target: dict) -> tuple[str, int] | None:
    """Archive/restore pre-check (KTD6): (message, status) when refused. The
    last-admin and direct-report rules need counts, so the guarded
    Database.archive_employee enforces them in its UPDATE."""
    if actor["role"] not in EDITOR_ROLES:
        return "Only HR and admins can archive people.", 403
    if target["id"] == actor["id"]:
        return "You cannot archive yourself.", 400
    if target["role"] == "admin" and actor["role"] != "admin":
        return "Only an admin can archive or restore an admin.", 403
    return None


# --- Fix requests (KTD6) ------------------------------------------------------

FIX_REVIEW_ROLES = ("hr", "admin")


def can_view_fix_reviews(actor: dict | None) -> bool:
    """HR and admins open the fix review pages; deciding is can_approve_fix."""
    return actor is not None and actor["role"] in FIX_REVIEW_ROLES


def can_approve_fix(actor: dict | None, request: dict, database) -> bool:
    """KTD6: HR decides, never the requester. An admin who is not the
    requester decides only while no active HR user other than the requester
    exists. Database.decide_fix_lines re-checks the requester rule."""
    if actor is None or request is None:
        return False
    requester = request["employee_id"]
    if actor["id"] == requester:
        return False
    if actor["role"] == "hr":
        return True
    if actor["role"] == "admin":
        return not database.has_active_hr_other_than(requester)
    return False


def fix_approvers(requester_id: int, database) -> list[dict]:
    """Who decides a person's fix request, by the can_approve_fix rule: every
    active HR user but the requester, else every active admin but them."""
    approvers = [p for p in database.active_hr_users() if p["id"] != requester_id]
    if approvers:
        return approvers
    return [p for p in database.active_admins() if p["id"] != requester_id]


# --- Punch archives (v9, KTD4) --------------------------------------------------

# Archiving hides punches everywhere, so it stays with the role that runs the
# system; HR and admins (EDITOR_ROLES) read the archive list.
PUNCH_ARCHIVE_ROLES = ("admin",)


def can_archive_punches(actor: dict | None) -> bool:
    """Archive and restore ID-issue punches (admins only)."""
    return actor is not None and actor["role"] in PUNCH_ARCHIVE_ROLES
