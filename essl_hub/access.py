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
        return {row["id"] for row in database.employees()}
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
