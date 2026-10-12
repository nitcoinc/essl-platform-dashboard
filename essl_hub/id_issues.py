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

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.device_user_id}"


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


def find_issues(summary: list[dict], employees: list[dict], dismissed: set[str] | frozenset = frozenset()) -> list[Issue]:
    by_id = {str(row["device_user_id"]): row for row in summary}
    roster = {str(e["device_user_id"]): e for e in employees}
    issues: list[Issue] = []

    for uid, row in sorted(by_id.items()):
        common = dict(
            device_user_id=uid, device_name=row.get("device_name") or "", punch_count=int(row.get("punch_count") or 0),
            last_seen=row.get("last_seen"), sites=row.get("sites") or "",
        )
        employee = roster.get(uid)
        if uid.startswith("C3-"):
            issues.append(Issue("shared", "info", title="Shared card, not counted",
                                detail="Listed in ESSL_C3_UNLINKED_PINS, so these swipes never count for anyone.", **common))
        elif employee is None:
            issues.append(Issue("unknown", "bad", title="Punches but nobody on the roster",
                                detail="Hours are not counted until someone with this device ID is added.", **common))
        elif not names_agree(common["device_name"], employee["name"]):
            issues.append(Issue("name", "warn", title="Device name differs from the roster",
                                detail=f"The device calls {uid} “{common['device_name']}”; the roster says “{employee['name']}”. Check it is the same person.",
                                employee_id=employee["id"], employee_name=employee["name"], **common))

    for uid, employee in sorted(roster.items(), key=lambda kv: kv[1]["name"]):
        common = dict(device_user_id=uid, employee_id=employee["id"], employee_name=employee["name"])
        if "@" in uid:
            issues.append(Issue("placeholder", "warn", title="No device ID yet",
                                detail="The ID is the work email, so no door punch can ever match. Set the ID enrolled on the device.", **common))
        elif uid not in by_id:
            issues.append(Issue("never", "warn", title="No punches on any device",
                                detail="Nobody has scanned with this ID. Check it matches the device enrolment.", **common))

    return [i for i in issues if i.key not in dismissed]
