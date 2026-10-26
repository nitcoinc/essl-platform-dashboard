"""Attendance alert settings (R15): mode, alert hours and the morning send hour.

Stored as plain rows in ``settings``. ``read`` never raises on a bad stored
value: anything unparseable or out of range falls back to its default, so a
hand-edited row cannot stop the alerts service. ``parse`` and ``write`` are
for the admin settings page and refuse bad input with a message.
"""

from __future__ import annotations

from dataclasses import dataclass

from .punch_alerts import DEFAULT_ALERT_END_HOUR, DEFAULT_ALERT_START_HOUR

MODE_KEY = "alerts_mode"
START_HOUR_KEY = "alerts_start_hour"
END_HOUR_KEY = "alerts_end_hour"
MORNING_HOUR_KEY = "alerts_morning_hour"
# Written by the alerts service after each weekly baseline run, so the settings
# page can show a rebuild time even when the snapshot was empty.
BASELINES_AT_KEY = "alerts_baselines_at"

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_LIVE = "live"
MODES = (MODE_OFF, MODE_SHADOW, MODE_LIVE)

# KTD11: a fresh install computes and logs before anyone switches to live.
DEFAULT_MODE = MODE_SHADOW
DEFAULT_START_HOUR = DEFAULT_ALERT_START_HOUR
DEFAULT_END_HOUR = DEFAULT_ALERT_END_HOUR
DEFAULT_MORNING_HOUR = 9


class SettingsError(ValueError):
    """Input the settings page refuses; the message is shown to the admin."""


@dataclass(frozen=True)
class AlertSettings:
    mode: str = DEFAULT_MODE
    start_hour: int = DEFAULT_START_HOUR
    end_hour: int = DEFAULT_END_HOUR
    morning_hour: int = DEFAULT_MORNING_HOUR

    @property
    def detecting(self) -> bool:
        """Shadow and live both run detection; off runs none."""
        return self.mode != MODE_OFF


def read(database) -> AlertSettings:
    """Current settings, every bad or missing value replaced by its default.
    An alert-hours pair that is not start < end falls back as a pair."""
    mode = (database.setting(MODE_KEY) or "").strip().lower()
    if mode not in MODES:
        mode = DEFAULT_MODE
    start = _stored_hour(database.setting(START_HOUR_KEY), DEFAULT_START_HOUR)
    end = _stored_hour(database.setting(END_HOUR_KEY), DEFAULT_END_HOUR)
    if start >= end:
        start, end = DEFAULT_START_HOUR, DEFAULT_END_HOUR
    morning = _stored_hour(database.setting(MORNING_HOUR_KEY), DEFAULT_MORNING_HOUR)
    return AlertSettings(mode, start, end, morning)


def parse(
    mode: object, start_hour: object, end_hour: object, morning_hour: object
) -> AlertSettings:
    """Validate form input; raises SettingsError naming the first problem."""
    mode_value = validate_mode(mode)
    start = validate_hour(start_hour, "Alert start hour")
    end = validate_hour(end_hour, "Alert end hour")
    validate_hours_range(start, end)
    morning = validate_hour(morning_hour, "Morning email hour")
    return AlertSettings(mode_value, start, end, morning)


def validate_mode(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value not in MODES:
        raise SettingsError("Choose off, shadow or live for attendance alerts.")
    return value


def validate_hour(raw: object, label: str = "Hour") -> int:
    """A whole hour 0-23."""
    try:
        hour = int(str(raw).strip())
    except (TypeError, ValueError):
        raise SettingsError(f"{label} must be a whole hour from 0 to 23.") from None
    if not 0 <= hour <= 23:
        raise SettingsError(f"{label} must be a whole hour from 0 to 23.")
    return hour


def validate_hours_range(start_hour: int, end_hour: int) -> None:
    if start_hour >= end_hour:
        raise SettingsError("Alert hours must start before they end.")


def write(database, settings: AlertSettings) -> None:
    """Persist all four values (validated again, so no caller can store a
    pair ``read`` would discard)."""
    checked = parse(settings.mode, settings.start_hour, settings.end_hour, settings.morning_hour)
    database.set_setting(MODE_KEY, checked.mode)
    database.set_setting(START_HOUR_KEY, str(checked.start_hour))
    database.set_setting(END_HOUR_KEY, str(checked.end_hour))
    database.set_setting(MORNING_HOUR_KEY, str(checked.morning_hour))


def _stored_hour(raw: str | None, default: int) -> int:
    try:
        hour = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return hour if 0 <= hour <= 23 else default
