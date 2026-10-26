from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class DeviceConfig:
    id: str
    site: str
    model: str
    ip: str
    port: int
    protocol: str
    password_env: str
    password: str | None = None
    # KTD5: set while "Wait for door close" is unchecked; C3 door-denied
    # swipes (24/25) at or after it are captured. None = drop them.
    door_denied_since: datetime | None = None
    # KTD6: the pending door-parameter request and its guard timestamp, as
    # read at the start of the cycle. A dict, so kept out of the hash.
    door_request: dict | None = field(default=None, hash=False)
    door_requested_at: datetime | None = None

# The two device protocols the collector speaks.
X990_PROTOCOL = "zk-attendance"
C3_PROTOCOL = "c3-pullsdk"


# Seed-only: inserted into the devices table when missing, never overwritten.
# The database is authoritative; admins add and retire devices there.
DEVICES = (
    DeviceConfig(
        id="uppal-x990",
        site="Uppal",
        model="X990",
        ip=os.getenv("ESSL_X990_IP", "192.168.1.201"),
        port=int(os.getenv("ESSL_X990_PORT", "4370")),
        protocol=X990_PROTOCOL,
        password_env="ESSL_X990_PASSWORD",
    ),
    DeviceConfig(
        id="gachibowli-c3-400",
        site="Gachibowli",
        model="C3-400",
        ip=os.getenv("ESSL_C3_IP", "192.168.1.202"),
        port=int(os.getenv("ESSL_C3_PORT", "4370")),
        protocol=C3_PROTOCOL,
        password_env="ESSL_C3_PASSWORD",
    ),
)


def password_for(device: DeviceConfig) -> str | None:
    if device.password not in (None, ""):
        return device.password
    value = os.getenv(device.password_env) if device.password_env else None
    return value if value not in (None, "") else None


def numeric_comm_password(site: str, password: str) -> int:
    """The X990 driver accepts only a numeric comm key. The collector and the
    admin form share this one rule so the two cannot drift apart."""
    try:
        return int(password)
    except ValueError:
        raise ValueError(f"comm password for {site} must be a number") from None


def collector_interval_seconds() -> int:
    return int(os.getenv("ESSL_COLLECTOR_INTERVAL_SECONDS", "60"))


def heartbeat_write_seconds() -> int:
    """KTD11: an unchanged device row is still rewritten after this many
    seconds (default 900) so Devices can show "last checked"."""
    return int(os.getenv("ESSL_HEARTBEAT_WRITE_SECONDS", "900"))


def db_path() -> str:
    """Filesystem path of the SQLite database file (ESSL_DB_PATH)."""
    return os.getenv("ESSL_DB_PATH") or "/data/essl.db"


def spool_path() -> str:
    """Collector punch spool file (ESSL_SPOOL_PATH, KTD9); defaults next to the database."""
    return os.getenv("ESSL_SPOOL_PATH") or os.path.join(
        os.path.dirname(db_path()), "collector-spool.db"
    )


def collector_status_path() -> str:
    """Collector status JSON (ESSL_COLLECTOR_STATUS_PATH, KTD12); defaults next to the database."""
    return os.getenv("ESSL_COLLECTOR_STATUS_PATH") or os.path.join(
        os.path.dirname(db_path()), "collector-status.json"
    )


def _positive_int_env(name: str, default: int) -> int:
    value = (os.getenv(name) or "").strip()
    if not value:
        return default
    try:
        number = int(value)
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number") from None
    if number < 1:
        raise RuntimeError(f"{name} must be at least 1")
    return number


def backup_minutes() -> int:
    """Minutes between R2 snapshots (ESSL_BACKUP_MINUTES, R5)."""
    return _positive_int_env("ESSL_BACKUP_MINUTES", 15)


def backup_keep_hours() -> int:
    """Hours of recent/ snapshots kept (ESSL_BACKUP_KEEP_HOURS, R6)."""
    return _positive_int_env("ESSL_BACKUP_KEEP_HOURS", 48)


def backup_keep_days() -> int:
    """Days of nightly/ snapshots kept (ESSL_BACKUP_KEEP_DAYS, R6)."""
    return _positive_int_env("ESSL_BACKUP_KEEP_DAYS", 30)


def bootstrap_admin_email() -> str | None:
    """The work email promoted to admin on web boot while no admin exists.

    Admin access is just the `employees.role` column, so a fresh deployment
    needs one known address to own the console before a UI exists to grant it.
    Unset (or not yet on the roster) means the boot promotes nobody.
    """
    value = os.getenv("ESSL_BOOTSTRAP_ADMIN_EMAIL")
    return value if value not in (None, "") else None


def setup_token() -> str | None:
    """Enables the one-time `/setup` page that creates the first admin.

    Unset means the route does not exist. At least 16 characters, because it is
    the only thing standing between the public internet and the first admin
    while the database has none.
    """
    value = os.getenv("ESSL_SETUP_TOKEN")
    if value in (None, ""):
        return None
    if len(value) < 16:
        raise RuntimeError("ESSL_SETUP_TOKEN must be at least 16 characters long")
    return value


def mail_provider() -> str:
    """Which transport `essl_hub.mail` sends through: ``resend``, ``cloudflare``
    or ``console`` (local development only: prints the code to the server log).

    Set ``ESSL_MAIL_PROVIDER`` explicitly when both providers are configured.
    Otherwise the credentials present decide: Cloudflare's pair wins over
    Resend's single key, and Resend is the fallback so the sender can raise a
    missing-credential error naming what is actually absent.
    """
    value = (os.getenv("ESSL_MAIL_PROVIDER") or "").strip().lower()
    if value:
        if value not in ("resend", "cloudflare", "console"):
            raise RuntimeError(
                "ESSL_MAIL_PROVIDER must be 'resend', 'cloudflare' or 'console'"
            )
        return value
    if os.getenv("CLOUDFLARE_API_TOKEN") and os.getenv("CLOUDFLARE_ACCOUNT_ID"):
        return "cloudflare"
    return "resend"


def public_url() -> str | None:
    """The dashboard's public base URL (``ESSL_PUBLIC_URL``), used to build the
    absolute links in alert and fix-request emails. Background services have no
    request to derive it from. Unset means emails with links cannot go live.
    """
    value = (os.getenv("ESSL_PUBLIC_URL") or "").strip().rstrip("/")
    if not value:
        return None
    if not value.startswith(("https://", "http://")):
        raise RuntimeError("ESSL_PUBLIC_URL must start with https:// or http://")
    return value


def hr_reply_to() -> str | None:
    """Reply-To address for alert and fix-request emails (``ESSL_HR_REPLY_TO``)."""
    value = (os.getenv("ESSL_HR_REPLY_TO") or "").strip()
    return value or None


TIMEZONE_NAME = "Asia/Kolkata"

# Roster uploads are rate-limited at the HTTP layer independently of the row
# ceiling inside parse_xlsx (which bounds decompressed .xlsx content, R18).
_MAX_UPLOAD_MB_DEFAULT = 5


def max_upload_mb() -> int:
    value = os.getenv("ESSL_MAX_UPLOAD_MB")
    if value in (None, ""):
        return _MAX_UPLOAD_MB_DEFAULT
    try:
        parsed = int(value)
    except ValueError:
        raise RuntimeError("ESSL_MAX_UPLOAD_MB must be a whole number of megabytes") from None
    if parsed < 1:
        raise RuntimeError("ESSL_MAX_UPLOAD_MB must be at least 1")
    return parsed
