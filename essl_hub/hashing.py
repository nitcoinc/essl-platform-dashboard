"""One hex-SHA-256 implementation.

Session tokens, login-code and pending tokens, API keys and punch
fingerprints all need the same thing: the hex SHA-256 of a UTF-8 string.
They used to each write `hashlib.sha256(value.encode("utf-8")).hexdigest()`
inline, which is five copies of one expression with no reason to differ.

This module deliberately imports nothing from the package, so the collector can
use it without pulling in `auth` (which imports the mailer and its HTTP client).
"""

from __future__ import annotations

import hashlib


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
