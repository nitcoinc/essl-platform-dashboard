from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace
from typing import NamedTuple

import openpyxl

from . import access
from .db import IntegrityError, RuleViolation

REQUIRED_COLUMNS = {
    "name": ("name",),
    "email": ("work email", "email"),
    "device_user_id": ("device user id", "device_user_id", "id"),
}

# Optional columns (KTD9). An absent column leaves that attribute alone; a
# present but blank cell means "clear" for managers and "keep" for roles.
OPTIONAL_COLUMNS = {
    "manager_email": ("manager email", "manager"),
    "role": ("role",),
}

# Roles a roster may grant. Admin is granted only on the People page.
ROSTER_ROLES = ("employee", "manager", "hr")

# R18: .xlsx is a zip container, so a small upload can decompress into a very
# large sheet. The HTTP upload cap bounds only the compressed size, so both
# roster formats are bounded by row count here.
MAX_ROSTER_ROWS = 5000

_UNREADABLE_XLSX = "could not read this file as an .xlsx workbook; save it again from Excel"
_ADMIN_ROW = "this row matches an admin; only an admin can change an admin's email or device user ID"


class RosterFileError(Exception):
    pass


class RosterRow(NamedTuple):
    row_number: int
    name: str
    email: str
    device_user_id: str
    # None when the column is absent from the file.
    manager_email: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class RosterOutcome:
    row_number: int
    name: str
    email: str
    device_user_id: str
    result: str  # "added" | "updated" | "rejected"
    reason: str | None = None
    warnings: tuple[str, ...] = ()


def apply_roster(database, csv_text: str, actor: dict | None = None) -> list[RosterOutcome]:
    return apply_rows(database, parse_csv(csv_text), actor=actor)


def apply_rows(database, rows, actor: dict | None = None) -> list[RosterOutcome]:
    """Two passes (KTD9): pass 1 upserts identity and role per row, pass 2
    resolves managers in file order once every row exists, so a manager listed
    later in the file still links. ``actor`` is the signed-in uploader; every
    write to an existing record goes through access.can_modify_employee."""
    rows = [RosterRow(*row) for row in rows]
    rows = [row._replace(email=row.email.strip().lower()) for row in rows]
    outcomes: dict[int, RosterOutcome] = {}

    shape_valid: list[RosterRow] = []
    for row in rows:
        reason = identity_error(row.name, row.email, row.device_user_id)
        if reason:
            outcomes[row.row_number] = _outcome(row, "rejected", reason)
        else:
            shape_valid.append(row)

    duplicate_emails = _duplicates(row.email.lower() for row in shape_valid)
    duplicate_ids = _duplicates(row.device_user_id for row in shape_valid)

    applied: list[tuple[RosterRow, list[str]]] = []
    for row in shape_valid:
        if row.email.lower() in duplicate_emails:
            outcomes[row.row_number] = _outcome(row, "rejected", "duplicate work email in this file")
            continue
        if row.device_user_id in duplicate_ids:
            outcomes[row.row_number] = _outcome(row, "rejected", "duplicate device user ID in this file")
            continue
        if actor is not None and _touches_protected_admin(database, actor, row):
            outcomes[row.row_number] = _outcome(row, "rejected", _ADMIN_ROW)
            continue
        owner = database.employee_by_email(row.email)
        if owner is not None and owner["device_user_id"] != row.device_user_id:
            outcomes[row.row_number] = _outcome(
                row, "rejected", "work email already used by another employee"
            )
            continue
        try:
            result = database.upsert_employee(
                device_user_id=row.device_user_id, name=row.name, email=row.email
            )
        except IntegrityError:
            outcomes[row.row_number] = _outcome(
                row, "rejected", "work email or device user ID is already used by another employee"
            )
            continue
        warnings: list[str] = []
        if row.role:
            warning = _apply_role(database, actor, row)
            if warning:
                warnings.append(warning)
        outcomes[row.row_number] = _outcome(row, result)
        applied.append((row, warnings))

    for row, warnings in applied:
        if row.manager_email is not None:
            warning = _apply_manager(database, row)
            if warning:
                warnings.append(warning)
        if warnings:
            outcomes[row.row_number] = replace(outcomes[row.row_number], warnings=tuple(warnings))

    return [outcomes[key] for key in sorted(outcomes)]


def summarize(outcomes: list[RosterOutcome]) -> dict[str, int]:
    return {
        "added": sum(o.result == "added" for o in outcomes),
        "updated": sum(o.result == "updated" for o in outcomes),
        "warnings": sum(bool(o.warnings) for o in outcomes if o.result != "rejected"),
        "rejected": sum(o.result == "rejected" for o in outcomes),
    }


def issues_csv(outcomes: list[RosterOutcome]) -> str:
    """Only the non-clean rows, for the "Download issues (CSV)" link."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["row", "name", "work email", "device user id", "result", "reason"])
    for o in outcomes:
        if o.result == "rejected":
            writer.writerow([o.row_number, o.name, o.email, o.device_user_id, "error", o.reason])
        for warning in o.warnings:
            writer.writerow([o.row_number, o.name, o.email, o.device_user_id, "warning", warning])
    return output.getvalue()


def identity_error(name: str, email: str, device_user_id: str) -> str | None:
    """Shape checks shared by the roster and the People form."""
    if not name:
        return "name is empty"
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        return "email has no @ sign"
    if not device_user_id:
        return "device user ID is empty"
    return None


def _outcome(row: RosterRow, result: str, reason: str | None = None) -> RosterOutcome:
    return RosterOutcome(row.row_number, row.name, row.email, row.device_user_id, result, reason)


def _touches_protected_admin(database, actor: dict, row: RosterRow) -> bool:
    """True when the row would change an existing admin's email or device
    user ID and the uploader may not (KTD9, R14)."""
    for existing, field in (
        (database.employee_by_device_user_id(row.device_user_id), "email"),
        (database.employee_by_email(row.email), "device_user_id"),
    ):
        if existing is None or existing["role"] != "admin":
            continue
        changed = (
            existing["email"].lower() != row.email.lower()
            if field == "email"
            else existing["device_user_id"] != row.device_user_id
        )
        if changed and not access.can_modify_employee(actor, existing, field):
            return True
    return False


def _apply_role(database, actor: dict | None, row: RosterRow) -> str | None:
    role = row.role.strip().lower()
    target = database.employee_by_device_user_id(row.device_user_id)
    if target["role"] == "admin" or role == target["role"]:
        return None  # ignored for existing admins (KTD9)
    if role == "admin":
        return "role admin cannot be set from a roster; role left unchanged"
    if role not in ROSTER_ROLES:
        return f"role '{row.role}' is not employee, manager or hr; role left unchanged"
    if actor is not None and not access.can_assign_role(actor, target, role):
        return "you cannot set this role; role left unchanged"
    try:
        database.set_employee_role(target["id"], role)
    except RuleViolation as exc:
        return f"{exc} Role left unchanged."
    return None


def _apply_manager(database, row: RosterRow) -> str | None:
    target = database.employee_by_device_user_id(row.device_user_id)
    manager_email = row.manager_email.strip()
    if not manager_email:
        manager_id = None
    else:
        manager = database.employee_by_email(manager_email)
        if manager is None:
            return f"manager not found: {manager_email}; manager not set"
        if manager["id"] == target["id"]:
            return "cannot manage self; manager not set"
        if access.would_create_cycle(database, target["id"], manager["id"]):
            return f"{manager_email} already reports to this employee; manager not set"
        manager_id = manager["id"]
    if target.get("manager_id") == manager_id:
        return None
    try:
        database.set_employee_manager(target["id"], manager_id)
    except RuleViolation as exc:
        return f"{exc} Manager not set."
    return None


def parse_csv(csv_text: str) -> list[RosterRow]:
    try:
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))
        fieldnames = list(reader.fieldnames or [])
    except csv.Error as exc:
        # _csv.Error (for example a cell past csv.field_size_limit) must not
        # reach the caller as a 500.
        raise RosterFileError(f"the file is not valid CSV: {exc}") from exc
    if reader.fieldnames is None:
        raise RosterFileError("The file has no header row")

    positions = _match_columns(fieldnames)
    columns = {field: fieldnames[index] for field, index in positions.items()}

    rows = []
    try:
        for row_number, raw_row in enumerate(reader, start=1):
            if row_number > MAX_ROSTER_ROWS:
                raise RosterFileError(
                    f"the file has more than {MAX_ROSTER_ROWS} rows; "
                    "split the roster into smaller files"
                )
            rows.append(
                RosterRow(
                    row_number,
                    *((raw_row.get(columns[field]) or "").strip() for field in REQUIRED_COLUMNS),
                    *(
                        (raw_row.get(columns[field]) or "").strip() if field in columns else None
                        for field in OPTIONAL_COLUMNS
                    ),
                )
            )
    except csv.Error as exc:
        raise RosterFileError(f"the file is not valid CSV: {exc}") from exc
    return rows


def parse_xlsx(file_bytes: bytes) -> list[RosterRow]:
    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(file_bytes), read_only=True, data_only=True
        )
    except Exception as exc:
        # openpyxl uses defusedxml when it is installed, so an XML entity or XXE
        # payload surfaces here as an exception rather than a resolved reference.
        raise RosterFileError(_UNREADABLE_XLSX) from exc

    try:
        sheet = workbook.active
        title = sheet.title
    except Exception as exc:  # pragma: no cover - defensive: no sheet to name
        workbook.close()
        raise RosterFileError(_UNREADABLE_XLSX) from exc

    try:
        return _rows_from_xlsx_sheet(sheet, title)
    except RosterFileError:
        raise
    except Exception as exc:
        raise RosterFileError(
            f"could not read sheet '{title}'; it may be corrupt"
        ) from exc
    finally:
        workbook.close()


def _rows_from_xlsx_sheet(sheet, title: str) -> list[RosterRow]:
    data_rows = sheet.iter_rows(values_only=True)
    header_row = next(data_rows, None)
    if header_row is None or not any(_cell_text(cell) for cell in header_row):
        raise RosterFileError(f"no header row found in first row of sheet '{title}'")

    positions = _match_columns(
        [_cell_text(cell) for cell in header_row], location=f"sheet '{title}'"
    )
    width = len(header_row)

    rows: list[RosterRow] = []
    for row_index, raw_row in enumerate(data_rows, start=1):
        # Bound the iteration itself (R18), not just the appended rows: a sparse
        # sheet makes openpyxl yield gap rows, so counting only data rows would
        # let a small file walk millions of rows before the ceiling fires.
        if row_index > MAX_ROSTER_ROWS:
            raise RosterFileError(
                f"sheet '{title}' has more than {MAX_ROSTER_ROWS} rows; "
                "split the roster into smaller files"
            )
        values = [_cell_text(cell) for cell in raw_row]
        values += [""] * (width - len(values))
        if not any(values):
            continue
        row_number = len(rows) + 1
        rows.append(
            RosterRow(
                row_number,
                *(values[positions[field]] for field in REQUIRED_COLUMNS),
                *(
                    values[positions[field]] if field in positions else None
                    for field in OPTIONAL_COLUMNS
                ),
            )
        )
    return rows


def _match_columns(header_names: list[str], location: str = "") -> dict[str, int]:
    """Position of the first matching alias for each REQUIRED_COLUMNS field,
    plus each OPTIONAL_COLUMNS field the header names."""
    lowered = [name.strip().lower() for name in header_names]
    positions: dict[str, int] = {}
    for field, aliases in REQUIRED_COLUMNS.items():
        match = next((lowered.index(alias) for alias in aliases if alias in lowered), None)
        if match is None:
            where = f" in {location}" if location else ""
            raise RosterFileError(f"Missing required column: {aliases[0]}{where}")
        positions[field] = match
    for field, aliases in OPTIONAL_COLUMNS.items():
        match = next((lowered.index(alias) for alias in aliases if alias in lowered), None)
        if match is not None:
            positions[field] = match
    return positions


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def _duplicates(values) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return dupes
