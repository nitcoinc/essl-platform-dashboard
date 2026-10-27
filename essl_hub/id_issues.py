"""Device-ID problems HR and admins should check: who the devices see that the
roster does not, and roster entries the devices never see.

Pure over two lists (per-ID punch summary, employees) so the rules are
unit-testable; the route only fetches and renders.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime

DISMISSED_SETTING = "id_issues_dismissed"
_WORD = re.compile(r"[a-z]+")
# Labels people put after a name on the X990 ("Gopal R (office boy)").
_NOISE = {"office", "boy"}


@dataclass(frozen=True)
class Issue:
    kind: str  # unknown | placeholder | never | name | shared
    severity: str  # bad | warn | info
    device_user_id: str
    title: str
    detail: str
    device_name: str = ""
    employee_id: int | None = None
    employee_name: str = ""
    punch_count: int = 0
    last_seen: datetime | None = None
    sites: str = ""
    first_seen: datetime | None = None

    @property
    def key(self) -> str:
        """Dismissal key. A "same person" dismissal also carries the device
        name, so a later rename on the device brings the issue back."""
        if self.kind == "name":
            return f"name:{self.device_user_id}:{' '.join(sorted(_words(self.device_name)))}"
        return f"{self.kind}:{self.device_user_id}"

    @property
    def impact(self) -> str:
        """One plain sentence on what the issue costs."""
        n = self.punch_count
        punches = f"{n} {'punch' if n == 1 else 'punches'}"
        since = f" since {self.first_seen.day} {self.first_seen.strftime('%b')}" if self.first_seen else ""
        if self.kind == "unknown":
            return f"{punches}{since} count for nobody."
        if self.kind == "shared":
            return f"{punches}{since} are not counted for anyone."
        if self.kind == "name":
            return f"{punches}{since} count for {self.employee_name}, but the device calls this ID “{self.device_name}”."
        if self.kind == "placeholder":
            return f"No door punch can count for {self.employee_name} until a device ID is set."
        return f"{self.employee_name} has no door hours: no device has seen ID {self.device_user_id}."


@dataclass(frozen=True)
class Fix:
    """The one most likely fix for an issue.

    action: link (give employee_id the device_user_id, via a confirm step;
    the route adds it as an extra ID when their main ID has punches, else
    replaces the main ID),
    add (new person), set_id (type an ID), same (dismiss a name difference),
    none.
    """
    action: str
    label: str
    device_user_id: str = ""
    employee_id: int | None = None


def _words(name: str) -> set[str]:
    return {w for w in _WORD.findall((name or "").lower()) if len(w) > 1 and w not in _NOISE}


def names_agree(device_name: str, roster_name: str) -> bool:
    """Same person when any real word is shared, allowing initials and
    reordering ("K Arun Prakash" vs "Arun Prakash") and spelling drift
    sharing four leading letters ("Gopal R" vs "Rao Gopala")."""
    device, roster = _words(device_name), _words(roster_name)
    if not device or not roster:
        return True
    if device & roster:
        return True
    return any(len(os.path.commonprefix([a, b])) >= 4 for a in device for b in roster)


def names_match(a: str, b: str) -> bool:
    """Stricter than names_agree: both names known and agreeing. Used to
    suggest which unclaimed device ID belongs to which roster person."""
    return bool(_words(a)) and bool(_words(b)) and names_agree(a, b)


# Issues whose punches an admin may archive (KTD4): IDs nobody holds, and
# shared cards. A mapped ID ("name") would silently change someone's hours.
ARCHIVABLE_KINDS = ("unknown", "shared")


def archivable(issues: list[Issue], device_user_id: str) -> Issue | None:
    """The unknown or shared issue for this ID, or None when its punches may
    not be archived (mapped, or nothing left to archive)."""
    return next(
        (i for i in issues if i.kind in ARCHIVABLE_KINDS and i.device_user_id == device_user_id), None
    )


def actionable(issues: list[Issue]) -> int:
    return sum(1 for i in issues if i.severity != "info")


def suggestions(issues: list[Issue]) -> dict[str, list[Issue]]:
    """Likely pairings between an unknown device ID and a roster person whose
    ID is a placeholder or never punched, keyed by Issue.key both ways."""
    unknown = [i for i in issues if i.kind == "unknown"]
    unmatched = [i for i in issues if i.kind in ("placeholder", "never")]
    pairs: dict[str, list[Issue]] = {}
    for u in unknown:
        for p in unmatched:
            if names_match(u.device_name, p.employee_name):
                pairs.setdefault(u.key, []).append(p)
                pairs.setdefault(p.key, []).append(u)
    return pairs


def _number(uid: str) -> int | None:
    """Canonical key of an ID written as optional letters then digits
    ("NP0128" and "128" both give 128); None for anything else, and for
    all-zero numbers ("0", "NP0000"), which never pair (KTD4)."""
    match = re.fullmatch(r"[A-Za-z]*0*(\d+)", (uid or "").strip())
    number = int(match.group(1)) if match else None
    return number or None


def _is_zero(uid: str) -> bool:
    digits = [ch for ch in uid if ch.isdigit()]
    return bool(digits) and all(ch == "0" for ch in digits)


def _all_ids(employee: dict) -> list[str]:
    ids = employee.get("device_ids") or [employee["device_user_id"]]
    return [str(uid) for uid in ids]


def id_twins(issues: list[Issue], employees: list[dict] | None = None) -> dict[str, Issue]:
    """Unknown device IDs that are a roster ID in another format (roster
    "128", device "NP0128"), keyed by Issue.key. Only unambiguous
    one-to-one pairs count.

    Without ``employees`` only never-punched roster IDs pair (both ways).
    With them, any roster person pairs through any of their IDs (KTD4); the
    twin is their "never" issue when the main ID has never punched (fix:
    change the ID), else a "roster" stand-in (fix: add another device ID)."""
    unknown: dict[int, list[Issue]] = {}
    for i in issues:
        if i.kind == "unknown" and _number(i.device_user_id) is not None:
            unknown.setdefault(_number(i.device_user_id), []).append(i)
    never: dict[int, list[Issue]] = {}
    for i in issues:
        if i.kind == "never" and _number(i.device_user_id) is not None:
            never.setdefault(_number(i.device_user_id), []).append(i)
    holders: dict[int, dict[int, dict]] = {}
    for employee in employees or []:
        for uid in _all_ids(employee):
            if (number := _number(uid)) is not None:
                holders.setdefault(number, {})[employee["id"]] = employee
    twins: dict[str, Issue] = {}
    for number, found in unknown.items():
        if len(found) != 1:
            continue
        if employees is None:
            roster = never.get(number, [])
            if len(roster) == 1:
                twins[found[0].key] = roster[0]
                twins[roster[0].key] = found[0]
            continue
        people = list(holders.get(number, {}).values())
        if len(people) != 1:
            continue
        person = people[0]
        if person.get("archived_at"):
            continue  # archived holders keep the number taken but are never suggested
        match = next((i for i in never.get(number, []) if i.employee_id == person["id"]), None)
        if match is not None:
            twins[found[0].key] = match
            twins[match.key] = found[0]
        else:
            twins[found[0].key] = Issue(
                "roster", "info", device_user_id=str(person["device_user_id"]), title="", detail="",
                employee_id=person["id"], employee_name=person["name"],
            )
    return twins


def primary_fix(issue: Issue, pairs: dict[str, list[Issue]], twins: dict[str, Issue]) -> Fix:
    """The single most likely fix, shown as the card's primary button."""
    twin = twins.get(issue.key)
    matches = pairs.get(issue.key, [])
    if issue.kind == "unknown":
        if _is_zero(issue.device_user_id):
            return Fix("none", "Nothing to do")  # devices use 0 for "no user"; never assignable
        if twin and twin.kind == "roster":
            return Fix("link", f"Add as another device ID for {twin.employee_name}", issue.device_user_id, twin.employee_id)
        if twin:
            return Fix("link", f"Change ID to {issue.device_user_id}", issue.device_user_id, twin.employee_id)
        if matches:
            return Fix("link", f"Link to {matches[0].employee_name}", issue.device_user_id, matches[0].employee_id)
        return Fix("add", "Add as new person", issue.device_user_id)
    if issue.kind in ("placeholder", "never"):
        if twin:
            return Fix("link", f"Change ID to {twin.device_user_id}", twin.device_user_id, issue.employee_id)
        if matches:
            return Fix("link", f"Use {matches[0].device_user_id}", matches[0].device_user_id, issue.employee_id)
        return Fix("set_id", "Set device ID", employee_id=issue.employee_id)
    if issue.kind == "name":
        return Fix("same", "Same person", issue.device_user_id, issue.employee_id)
    return Fix("none", "Nothing to do")


def find_issues(summary: list[dict], employees: list[dict], dismissed: set[str] | frozenset = frozenset()) -> list[Issue]:
    by_id = {str(row["device_user_id"]): row for row in summary}
    # Every main and extra ID maps to its holder (archived people included,
    # so their punches never resurface as unknown).
    roster = {uid: e for e in employees for uid in _all_ids(e)}
    issues: list[Issue] = []

    for uid, row in sorted(by_id.items()):
        common = dict(
            device_user_id=uid, device_name=row.get("device_name") or "", punch_count=int(row.get("punch_count") or 0),
            last_seen=row.get("last_seen"), sites=row.get("sites") or "", first_seen=row.get("first_seen"),
        )
        employee = roster.get(uid)
        if uid.startswith("C3-"):
            issues.append(Issue("shared", "info", title="Shared card, not counted",
                                detail="Listed in ESSL_C3_UNLINKED_PINS, so these swipes never count for anyone.", **common))
        elif employee is None and _is_zero(uid):
            issues.append(Issue("unknown", "bad", title="Punches under ID 0",
                                detail="Devices use an all-zero ID for “no user”, so it can't be given to anyone. "
                                       "Fix the enrolment on the device.", **common))
        elif employee is None:
            issues.append(Issue("unknown", "bad", title="Punches but nobody on the roster",
                                detail="Hours are not counted until someone with this device ID is added.", **common))
        elif employee.get("archived_at"):
            continue  # archived: mapped, so not unknown, but no other rule applies
        elif not names_agree(common["device_name"], employee["name"]):
            issues.append(Issue("name", "warn", title="Device name differs from the roster",
                                detail=(f"The device calls {uid} “{common['device_name']}”; the roster says “{employee['name']}”. "
                                        "If it is the same person, mark it so (or fix the name on the device itself). "
                                        "If it is someone else, change the roster ID here."),
                                employee_id=employee["id"], employee_name=employee["name"], **common))

    mains = {str(e["device_user_id"]): e for e in employees if not e.get("archived_at")}
    for uid, employee in sorted(mains.items(), key=lambda kv: kv[1]["name"]):
        common = dict(device_user_id=uid, employee_id=employee["id"], employee_name=employee["name"])
        if "@" in uid:
            issues.append(Issue("placeholder", "warn", title="No device ID yet",
                                detail="The ID is the work email, so no door punch can ever match. Set the ID enrolled on the device.", **common))
        elif not any(i in by_id for i in _all_ids(employee)):
            issues.append(Issue("never", "warn", title="No punches on any device",
                                detail="Nobody has scanned with this ID. Check it matches the device enrolment.", **common))

    return [i for i in issues if i.key not in dismissed]
