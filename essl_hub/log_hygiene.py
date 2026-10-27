"""Keep report tokens (KTD7) out of logs: a filter that redacts the
``/fix/<token>`` path from any record, installed on the access loggers."""

from __future__ import annotations

import logging
import re

_TOKEN_PATH = re.compile(r"/fix/[^/\s?\"']+")


class RedactReportTokens(logging.Filter):
    """Access logs (werkzeug's dev server, gunicorn when an access log is
    configured) print request paths; a report token must never land there."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a malformed record is not ours to fix
            return True
        if "/fix/" in message:
            record.msg = _TOKEN_PATH.sub("/fix/[redacted]", message)
            record.args = ()
        return True


def install_access_log_redaction() -> None:
    """Add the filter to werkzeug's and gunicorn's access loggers."""
    for name in ("werkzeug", "gunicorn.access"):
        logging.getLogger(name).addFilter(RedactReportTokens())
