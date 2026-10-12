"""Sign-in, sign-out and the one-time first-run setup."""

from __future__ import annotations

import secrets

from flask import Blueprint, Response, abort, current_app, g, redirect, render_template, request, send_from_directory, url_for

from .. import auth, turnstile
from .common import (
    PENDING_COOKIE,
    PENDING_MAX_AGE,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    THEME_COOKIE,
    THEME_MAX_AGE,
    THEMES,
    _login_required,
    _require_csrf,
    _safe_next,
    _client_ip,
    _home_for,
    _secure_cookies,
    database,
)

bp = Blueprint("auth", __name__)


def _start_session(outcome, redirect_to: str) -> Response:
    response = redirect(redirect_to)
    response.set_cookie(
        SESSION_COOKIE,
        outcome.session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=_secure_cookies(),
    )
    response.delete_cookie(PENDING_COOKIE)
    return response


def _set_pending_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        PENDING_COOKIE,
        token,
        max_age=PENDING_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=_secure_cookies(),
    )


def _setup_available() -> bool:
    # One-time by construction: the route disappears as soon as any admin
    # exists, so it cannot be used to add admins later, only to create the
    # first one (or to recover after the last admin is lost).
    return current_app.extensions["essl_setup_token"] is not None and not database.has_admin()
def _bot_check_failed() -> bool:
    """Turnstile gate for the public POSTs; skipped when not configured."""
    cfg = current_app.extensions.get("essl_turnstile")
    if cfg is None:
        return False
    token = request.form.get(turnstile.RESPONSE_FIELD) or ""
    return not turnstile.verify(cfg, token, _client_ip())


BOT_CHECK_MESSAGE = "We could not confirm you are human. Complete the check and try again."

# --- Sign-in -----------------------------------------------------

@bp.get("/signin")
def signin() -> str | Response:
    if g.employee is not None:
        return redirect(_home_for(g.employee))
    return render_template("signin_email.html")


@bp.post("/signin")
def signin_submit() -> Response:
    # R11: this response is identical in shape whether or not the email is
    # on the roster, and whether or not sending the code succeeded -- a
    # mail failure is logged inside auth.request_code, never reported
    # here, so neither case is distinguishable from the outside.
    email = (request.form.get("email") or "").strip()
    if _bot_check_failed():
        # Before any roster lookup or mail, so bots cannot trigger sends.
        return Response(
            render_template("signin_email.html", email=email, bot_error=BOT_CHECK_MESSAGE), 400
        )
    response = Response(render_template("signin_code.html", email=email))
    if email:
        result = auth.request_code(database, email, requested_ip=_client_ip())
        _set_pending_cookie(response, result.pending_token)
    return response


@bp.post("/signin/verify")
def signin_verify() -> str | Response:
    pending_token = request.cookies.get(PENDING_COOKIE) or ""
    code = (request.form.get("code") or "").strip()
    outcome = auth.verify_code(database, pending_token=pending_token, code=code)
    if not outcome.ok:
        message = {
            "expired": "That code has expired. Request a new one.",
            "locked": "Too many wrong attempts. Request a new code.",
        }.get(outcome.error, "That code is not correct.")
        return render_template("signin_code.html", error=message, error_kind=outcome.error)

    return _start_session(outcome, _home_for(outcome.employee))


# --- First-run setup (one-time) -----------------------------------

@bp.get("/setup")
def setup() -> str | Response:
    if not _setup_available():
        abort(404)
    return render_template("setup.html", error=None)


@bp.post("/setup")
def setup_submit() -> str | Response:
    if not _setup_available():
        abort(404)
    supplied = request.form.get("setup_token") or ""
    if _bot_check_failed():
        abort(403)
    if not secrets.compare_digest(supplied, current_app.extensions["essl_setup_token"] or ""):
        abort(403)

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    device_user_id = (request.form.get("device_user_id") or "").strip()

    error = None
    if not name:
        error = "Enter your name."
    elif "@" not in email or email.startswith("@") or email.endswith("@"):
        error = "Enter a work email address."
    if error is None:
        # An admin without a badge still needs a unique device_user_id; the
        # email is unique and can never match a real device badge.
        device_user_id = device_user_id or email
        clash = database.employee_by_device_user_id(device_user_id)
        if clash is not None and clash["email"].lower() != email.lower():
            error = (
                f"Device user ID {device_user_id} already belongs to "
                f"{clash['name']}. Use a different one."
            )
    if error is not None:
        return render_template(
            "setup.html",
            error=error,
            name=name,
            email=email,
            device_user_id=device_user_id,
        )

    database.create_first_admin(
        name=name, email=email, device_user_id=device_user_id
    )
    return redirect(url_for("auth.signin"))


@bp.post("/signout")
def signout() -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth.revoke_session(database, token)
    response = redirect(url_for("auth.signin"))
    response.delete_cookie(SESSION_COOKIE)
    return response


@bp.post("/preferences/theme")
@_login_required
def set_theme() -> Response:
    _require_csrf()
    choice = request.form.get("theme", "")
    if choice not in THEMES:
        abort(400)
    response = redirect(_safe_next(request.form.get("next"), url_for("auth.index")))
    response.set_cookie(
        THEME_COOKIE, choice, max_age=THEME_MAX_AGE, samesite="Lax", secure=_secure_cookies()
    )
    return response


FAVICON_MAX_AGE = 7 * 24 * 3600


@bp.get("/favicon.ico")
def favicon() -> Response:
    # Public: browsers ask for /favicon.ico before anyone signs in.
    response = send_from_directory(
        current_app.static_folder, "brand/favicon.ico", mimetype="image/x-icon", max_age=FAVICON_MAX_AGE
    )
    response.headers["Cache-Control"] = f"public, max-age={FAVICON_MAX_AGE}"
    return response


@bp.get("/")
def index() -> Response:
    if g.employee is None:
        return redirect(url_for("auth.signin"))
    return redirect(_home_for(g.employee))
