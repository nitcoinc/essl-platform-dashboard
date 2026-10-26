"""C3 door parameters the dashboard may read and write (KTD8, R12).

One closed whitelist, used by the web (filing a request) and the collector
(applying it). ``validate_request`` is the only way a value reaches the panel:
it takes JSON integers only and returns a fresh ``{key: int}`` map, so the
SETPARAM text is rebuilt from parsed integers and whitelisted key names and
no comma or ``=`` can smuggle a second key into the frame.

Ranges and option meanings follow the PullSDK User Guide v2.0 for a four-door
C3; values come back from GETPARAM as strings (``parse_params``).

The collector (``collectors.c3_door_step``) stores what it did with a request
in ``devices.door_params_result``::

    {
      "state": "applied" | "failed" | "expired" | "read",
      "requested_at": "2026-10-26 10:00:00",   # the request it answers
      "finished_at": "2026-10-26 10:00:41",
      "message": "",                           # request-level note, "" if none
      "keys": {                                # in request order; {} for "read"
        "Door1Intertime": {
          "outcome": "applied" | "unchanged" | "failed" | "not_attempted",
          "old": 1,      # value read before the write; None if not read
          "new": 0,      # value requested
          "now": 0,      # value read back after the write; None if not read back
          "message": ""  # e.g. "panel kept 1", "communication password rejected"
        }
      }
    }

"applied" means every key is applied or unchanged; any other key outcome
makes the request "failed". "expired" requests were never sent (KTD6).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

# Connection and identity parameters, plus the per-door verify mode. Never
# written from the dashboard (R12); rejected by name so the error is explicit.
NEVER_WRITABLE = frozenset(
    {"ComPwd", "IPAddress", "NetMask", "GATEIPAddress", "RS232BaudRate", "DeviceID"}
)
_VERIFY_TYPE = re.compile(r"^Door\d+VerifyType$")

MAX_LOCK_COUNT = 4
# KTD6: a request the collector has not reached by then expires unsent.
DOOR_REQUEST_TTL = timedelta(minutes=15)
# GETPARAM keys per call. A four-door read is 28 keys, so it fits in one.
READ_BATCH = 30


@dataclass(frozen=True)
class DoorParam:
    name: str  # "Intertime" for the per-door DoorNIntertime, else the full key
    label: str
    minimum: int
    maximum: int
    per_door: bool
    unit: str = ""
    # Select inputs: (value, label). Empty means a number input.
    options: tuple[tuple[int, str], ...] = ()
    suggestion: str = ""


PER_DOOR = (
    DoorParam("Intertime", "Punch interval", 0, 10, True, unit="s", suggestion="0"),
    DoorParam("Drivertime", "Unlock time", 1, 254, True, unit="s", suggestion="5-8"),
    DoorParam(
        "Detectortime", "Door-open alarm after", 1, 254, True, unit="s",
        suggestion="Unlock time + 10 or more",
    ),
    DoorParam(
        "CloseAndLock", "Lock when door closes", 0, 1, True,
        options=((0, "Off"), (1, "On")), suggestion="1",
    ),
    DoorParam(
        "SensorType", "Door sensor", 0, 2, True,
        options=((0, "None"), (1, "Normally open"), (2, "Normally closed")),
        suggestion="Match the wiring",
    ),
)
PANEL_WIDE = (
    DoorParam(
        "AntiPassback", "Anti-passback", 0, 6, False,
        options=(
            (0, "Off"),
            (1, "Doors 1 and 2"),
            (2, "Doors 3 and 4"),
            (3, "Doors 1 and 2, and doors 3 and 4"),
            (4, "Doors 1, 2 and doors 3, 4"),
            (5, "Door 1 and doors 2, 3"),
            (6, "Door 1 and doors 2, 3, 4"),
        ),
        suggestion="0",
    ),
    DoorParam(
        "InterLock", "Interlock", 0, 5, False,
        options=(
            (0, "Off"),
            (1, "Doors 1 and 2"),
            (2, "Doors 3 and 4"),
            (3, "Doors 1, 2 and 3"),
            (4, "Doors 1 and 2, and doors 3 and 4"),
            (5, "Doors 1, 2, 3 and 4"),
        ),
        suggestion="0",
    ),
)
_BY_NAME = {param.name: param for param in (*PER_DOOR, *PANEL_WIDE)}
_DOOR_KEY = re.compile(r"^Door([1-9])(" + "|".join(p.name for p in PER_DOOR) + r")$")
_INTEGER = re.compile(r"^-?\d+$")

# Read for display only, never written.
READ_ONLY_BASE = ("LockCount", "ReaderCount")
READ_ONLY_PER_DOOR = ("VerifyType",)


class DoorParamError(ValueError):
    """A request the whitelist refuses; the message is safe to show."""


def param_for(key: str) -> tuple[DoorParam, int | None] | None:
    """The whitelist entry for ``key`` and its door number (None for the
    panel-wide keys), or None when ``key`` is not writable."""
    match = _DOOR_KEY.fullmatch(key)
    if match:
        return _BY_NAME[match.group(2)], int(match.group(1))
    param = _BY_NAME.get(key)
    if param is not None and not param.per_door:
        return param, None
    return None


def key_label(key: str) -> str:
    """"Punch interval, door 1"; the key itself when it is not whitelisted."""
    found = param_for(key)
    if found is None:
        return key
    param, door = found
    return param.label if door is None else f"{param.label}, door {door}"


def read_keys(lock_count: int) -> list[str]:
    """Every key a door read asks for: the panel-wide ones, then per door the
    writable keys and the display-only VerifyType."""
    keys = [*READ_ONLY_BASE, *(p.name for p in PANEL_WIDE)]
    for door in range(1, lock_count + 1):
        keys.extend(f"Door{door}{p.name}" for p in PER_DOOR)
        keys.extend(f"Door{door}{name}" for name in READ_ONLY_PER_DOOR)
    return keys


def writable_keys(lock_count: int) -> list[str]:
    return [key for key in read_keys(lock_count) if param_for(key) is not None]


def parse_params(raw: dict) -> dict[str, int]:
    """GETPARAM strings to ints. A value that is not a plain integer is
    dropped rather than guessed."""
    parsed: dict[str, int] = {}
    for key, value in raw.items():
        text = str(value).strip()
        if _INTEGER.fullmatch(text):
            parsed[str(key)] = int(text)
    return parsed


def lock_count_of(params: dict | None) -> int | None:
    """The door count from a stored read, or None before the first read."""
    if not isinstance(params, dict):
        return None
    value = params.get("LockCount")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= MAX_LOCK_COUNT else None


def validate_request(request: object, lock_count: int | None) -> dict[str, int]:
    """The one check every door write goes through (KTD8).

    ``lock_count`` comes from the last read; None (never read) refuses any
    write. Returns a new ``{key: int}``; an empty map is "Read from device".
    Raises DoorParamError naming the first bad key."""
    if not isinstance(request, dict):
        raise DoorParamError("door request must be a map of keys to whole numbers")
    clean: dict[str, int] = {}
    for key, value in request.items():
        if not isinstance(key, str):
            raise DoorParamError("door parameter names must be text")
        if key in NEVER_WRITABLE or _VERIFY_TYPE.fullmatch(key):
            raise DoorParamError(f"{key} is never written from the dashboard")
        found = param_for(key)
        if found is None:
            raise DoorParamError(f"{key} is not a door setting the dashboard can change")
        param, door = found
        if lock_count is None:
            raise DoorParamError("read the door settings from the device before changing them")
        if door is not None and door > lock_count:
            raise DoorParamError(f"{key}: this panel has {lock_count} door(s)")
        # bool is an int subclass; JSON true must not pass as 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise DoorParamError(f"{key_label(key)} must be a whole number")
        if not param.minimum <= value <= param.maximum:
            raise DoorParamError(
                f"{key_label(key)} must be {param.minimum}-{param.maximum}"
                + (f" {param.unit}" if param.unit else "")
            )
        clean[key] = int(value)
    return clean
