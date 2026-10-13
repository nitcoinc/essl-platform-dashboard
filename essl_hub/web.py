from __future__ import annotations

import logging
import threading
from pathlib import Path

from flask import Flask, Response, g, jsonify, render_template, request

from . import access, auth, config, turnstile
from .config import DEVICES
from .db import SETUP_COMMANDS, BusyError, Database, DatabaseMissing, OfflineError
from .routes import BLUEPRINTS
from .routes import people as people_routes
from .routes.common import SESSION_COOKIE, _htmx_partial, _theme, current_sync_health

_logger = logging.getLogger(__name__)


def _prepare_database(database: Database) -> None:
    """Boot steps on the local file: the web app is the schema owner (KTD8),
    so it migrates and seeds, then checks the bootstrap admin. A busy or
    unavailable database skips a step with a log line so serving still starts."""
    try:
        database.initialize(DEVICES)
    except OfflineError as exc:
        _logger.warning("Database unavailable; schema migration and seeding skipped: %s", exc)
    # Web process only: the collector never grants roles or reads the bootstrap
    # address, so it stays independent of dashboard configuration (R10, R14).
    try:
        database.ensure_bootstrap_admin(config.bootstrap_admin_email())
    except OfflineError as exc:
        _logger.warning("Database unavailable; bootstrap admin check skipped: %s", exc)


def create_app(database: Database | None = None) -> Flask:
    root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
    )
    max_upload_mb = config.max_upload_mb()
    app.config["MAX_CONTENT_LENGTH"] = max_upload_mb * 1024 * 1024
    # Local file only, never created here (KTD5): a missing file serves the
    # not-set-up page until a backup command makes it.
    database = database or Database(must_exist=True)
    ready = {"done": False}
    ready_lock = threading.Lock()

    def ensure_prepared() -> bool:
        """Run the boot steps once the file exists; False while it is missing."""
        if ready["done"]:
            return True
        with ready_lock:
            if not ready["done"]:
                try:
                    _prepare_database(database)
                except DatabaseMissing:
                    return False
                ready["done"] = True
        return True

    if not ensure_prepared():
        _logger.warning(
            "Database %s not set up; serving the setup page until one of these runs: %s",
            getattr(database, "path", "?"), "; ".join(SETUP_COMMANDS),
        )
    app.extensions["essl_database"] = database
    app.extensions["essl_setup_token"] = config.setup_token()
    app.extensions["essl_max_upload_mb"] = max_upload_mb
    app.extensions["essl_turnstile"] = turnstile.config_from_env()

    @app.context_processor
    def turnstile_widget() -> dict:
        cfg = app.extensions["essl_turnstile"]
        return {"turnstile_site_key": cfg.site_key if cfg else None}

    @app.context_processor
    def nav_helpers() -> dict:
        # The shell links only to pages that exist, so a role's sidebar grows
        # as its pages land without template edits.
        return {"has_endpoint": lambda name: name in app.view_functions, "theme": _theme()}

    @app.context_processor
    def id_issue_badge() -> dict:
        # HR and admin see how many ID issues wait on the People nav entry.
        employee = g.get("employee")
        if not employee or employee["role"] not in access.EDITOR_ROLES:
            return {}
        # An htmx fragment has no nav, so skip the punch scan behind the count.
        if _htmx_partial():
            return {}
        return {"nav_issue_count": people_routes.issue_count()}

    @app.context_processor
    def sync_health_indicator() -> dict:
        # Admin only (R4); status files are re-read per page, which is cheap.
        employee = g.get("employee")
        if not employee or employee["role"] != "admin":
            return {}
        return {"sync_health": current_sync_health(database)[0]}

    def not_set_up() -> tuple[Response | str, int]:
        if request.path == "/api/health":
            return jsonify({"ok": False, "error": "database not set up"}), 503
        return render_template("offline.html", state="missing", commands=SETUP_COMMANDS), 503

    @app.errorhandler(DatabaseMissing)
    def database_missing(exc: DatabaseMissing) -> tuple[Response | str, int]:
        _logger.warning("Database missing: %s", exc)
        return not_set_up()

    @app.errorhandler(BusyError)
    def busy(exc: BusyError) -> tuple[str, int]:
        # Another process held the write lock past busy_timeout (KTD6).
        _logger.warning("Database busy: %s", exc)
        return render_template("offline.html", state="busy"), 503

    @app.errorhandler(OfflineError)
    def offline(exc: OfflineError) -> tuple[str, int]:
        # Database temporarily unavailable: an outage, not a bug.
        _logger.warning("Database unavailable: %s", exc)
        return render_template("offline.html"), 503

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # The favicon keeps its own long cache (U1).
        if request.endpoint != "auth.favicon":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.before_request
    def require_database():
        # Registered before load_employee so a missing file never reaches it.
        if request.endpoint == "static" or ensure_prepared():
            return None
        return not_set_up()

    @app.before_request
    def load_employee() -> None:
        token = request.cookies.get(SESSION_COOKIE)
        session = auth.resolve_session(database, token)
        g.employee = session["employee"] if session else None
        g.csrf_token = session["csrf_token"] if session else None

    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
    return app
