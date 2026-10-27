"""Subject, text and HTML for the missed-punch alert and fix-request emails.

Pure functions: the caller decides recipients and sends via mail.send_email.
Tone (KTD10): warm and plain, blame the reader, show only times and the
out-stretch, never anything about a person's normal pattern.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import NamedTuple

from .mail import BRAND_BLUE, LOGO_CID, dedupe_recipients

PRODUCT_NAME = "eSSL Attendance Hub"
NO_ACTION_LINE = "No action needed if this is right."

# Morning issue kinds (U4 maps its findings onto these).
MISSING_FINAL_OUT = "missing_final_out"
OUT_WITHOUT_IN = "out_without_in"
DOUBLE_IN = "double_in"
UNCLEAR_GAP = "unclear_gap"

# Fix line kinds, as filed on the report form.
MISSED_IN = "missed_in"
MISSED_OUT = "missed_out"
FLIP = "flip"
IGNORE = "ignore"

APPROVED = "approved"
PARTIAL = "partial"
REJECTED = "rejected"


class RenderedEmail(NamedTuple):
    subject: str
    text: str
    html: str


@dataclass(frozen=True)
class MorningIssue:
    """One problem on a day.

    missing_final_out: start = the last swipe-in.
    out_without_in: start = the lone swipe-out.
    double_in: start, end = the two swipe-ins.
    unclear_gap: start, end = the span we could not read.
    """

    kind: str
    start: time | datetime | None = None
    end: time | datetime | None = None


@dataclass(frozen=True)
class FixLine:
    """One requested change. direction is the corrected one ("in"/"out") for a flip.

    approved is None while pending, then True/False once HR decides.
    """

    kind: str
    at: time | datetime | None = None
    direction: str | None = None
    approved: bool | None = None


# --- Formatting -------------------------------------------------------------


def fmt_day(day: date | datetime) -> str:
    """"Mon, 26 Oct"."""
    return f"{day.strftime('%a')}, {day.day} {day.strftime('%b')}"


def fmt_time(value: time | datetime | None) -> str:
    return "" if value is None else value.strftime("%H:%M")


def fmt_duration(minutes: int) -> str:
    """"45 min", "2 h", "1 h 45 min"."""
    minutes = max(0, int(minutes))
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours} h" if not rest else f"{hours} h {rest} min"


def _one_line(value: str) -> str:
    """Names land in subjects; a stray newline must not become a header break."""
    return " ".join((value or "").split())


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


# --- Layout -----------------------------------------------------------------


def _button(label: str, url: str) -> str:
    return (
        '<tr><td style="padding:8px 32px 8px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td bgcolor="{BRAND_BLUE}" style="background:{BRAND_BLUE};border-radius:8px;">'
        f'<a href="{_e(url)}" style="display:inline-block;padding:12px 22px;'
        "font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;"
        f'border-radius:8px;">{_e(label)}</a>'
        "</td></tr></table></td></tr>"
    )


def _paragraphs(paragraphs: Iterable[str]) -> str:
    """Each item is already-escaped HTML."""
    return "".join(
        f'<p style="margin:0 0 12px;font-size:15px;line-height:1.5;color:#4a4a55;">{p}</p>'
        for p in paragraphs
    )


def _bullets(items: Iterable[str]) -> str:
    """Each item is already-escaped HTML."""
    rows = "".join(
        f'<li style="margin:0 0 6px;">{item}</li>' for item in items
    )
    return (
        '<ul style="margin:0 0 12px;padding-left:20px;font-size:15px;'
        f'line-height:1.5;color:#17171c;">{rows}</ul>'
    )


def _layout(
    title: str,
    body: str,
    *,
    button: tuple[str, str] | None = None,
    after_button: str = "",
    footer: str = "",
) -> str:
    """Same table layout as mail.render_html, plus colour-scheme hints."""
    button_row = _button(*button) if button else ""
    after_row = (
        f'<tr><td style="padding:8px 32px 0;">{after_button}</td></tr>'
        if after_button
        else ""
    )
    footer_row = (
        '<tr><td style="padding:16px 32px 28px;font-size:13px;line-height:1.5;'
        f'color:#6b6b76;">{footer}</td></tr>'
        if footer
        else '<tr><td style="padding:0 0 20px;"></td></tr>'
    )
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{_e(title)}</title>
</head><body style="margin:0;padding:0;background:#f1f3f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f3f8;padding:32px 12px;">
<tr><td align="center">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="max-width:480px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;font-family:Segoe UI,Helvetica,Arial,sans-serif;">
<tr><td align="center" bgcolor="{BRAND_BLUE}" style="background:{BRAND_BLUE};padding:28px 24px;">
<img src="cid:{LOGO_CID}" width="180" alt="NITCO" style="display:block;width:180px;max-width:100%;height:auto;border:0;">
</td></tr>
<tr><td style="padding:32px 32px 8px;color:#17171c;">
<h1 style="margin:0 0 12px;font-size:20px;font-weight:600;">{_e(title)}</h1>
{body}
</td></tr>
{button_row}
{after_row}
{footer_row}
</table>
</td></tr></table>
</body></html>"""


def _alert_footer_html(privacy_url: str) -> str:
    return (
        f'<a href="{_e(privacy_url)}" style="color:#6b6b76;">How we use attendance data</a>'
        f"<br>{_e(PRODUCT_NAME)}"
    )


def _alert_footer_text(privacy_url: str) -> str:
    return f"How we use attendance data: {privacy_url}\n\n{PRODUCT_NAME}"


def _small(text_html: str) -> str:
    return f'<p style="margin:0;font-size:14px;line-height:1.5;color:#4a4a55;">{text_html}</p>'


def _note_block(label: str, note: str) -> str:
    return (
        f'<p style="margin:0 0 6px;font-size:14px;color:#6b6b76;">{_e(label)}</p>'
        '<p style="margin:0 0 12px;padding:10px 12px;background:#f5f6fa;border-left:3px solid '
        f'{BRAND_BLUE};font-size:15px;line-height:1.5;color:#17171c;white-space:pre-wrap;">'
        f"{_e(note)}</p>"
    )


# --- Live check -------------------------------------------------------------


def live_check(
    *,
    employee_name: str,
    work_day: date,
    out_at: time | datetime,
    out_minutes: int,
    report_url: str,
    privacy_url: str,
) -> RenderedEmail:
    """Sent during the day when someone has read as out for a long stretch."""
    day = fmt_day(work_day)
    subject = f"Attendance check for {day} – a swipe may be missing"
    since = fmt_time(out_at)
    ago = fmt_duration(out_minutes)
    name = _one_line(employee_name)

    lead = f"Your last swipe today was a swipe-out at {since}, {ago} ago."
    device = (
        "If you're back inside, the reader may have missed your swipe back in. "
        "Let us know and HR will correct your hours."
    )
    text = (
        f"Hi {name},\n\n{lead}\n\n{device}\n\n"
        f"Report a missed swipe: {report_url}\n\n"
        f"{NO_ACTION_LINE}\n\n{_alert_footer_text(privacy_url)}\n"
    )
    body = _paragraphs(
        [
            f"Hi {_e(name)},",
            f"Your last swipe today was a swipe-out at <strong>{_e(since)}</strong>, "
            f"<strong>{_e(ago)}</strong> ago.",
            _e(device),
        ]
    )
    page = _layout(
        f"Attendance check for {day}",
        body,
        button=("Report a missed swipe", report_url),
        after_button=_small(_e(NO_ACTION_LINE)),
        footer=_alert_footer_html(privacy_url),
    )
    return RenderedEmail(subject, text, page)


# --- Morning check ----------------------------------------------------------


def issue_sentence(issue: MorningIssue) -> str:
    start, end = fmt_time(issue.start), fmt_time(issue.end)
    if issue.kind == MISSING_FINAL_OUT:
        at = f" at {start}" if start else ""
        return (
            f"Your last swipe was a swipe-in{at}, with no swipe-out after it. "
            "The reader may have missed your swipe-out."
        )
    if issue.kind == OUT_WITHOUT_IN:
        at = f" at {start}" if start else ""
        return (
            f"There's a swipe-out{at} with no swipe-in before it. "
            "The reader may have missed your swipe-in."
        )
    if issue.kind == DOUBLE_IN:
        both = f", at {start} and {end}" if start and end else ""
        return (
            f"There are two swipe-ins in a row{both}. "
            "The reader may have missed a swipe-out between them."
        )
    if issue.kind == UNCLEAR_GAP:
        span = f" between {start} and {end}" if start and end else " for part of the day"
        return (
            f"We couldn't tell whether you were in or out{span}. "
            "A swipe may have been missed."
        )
    return "One of your swipes may have been missed."


def _morning_tail(issues: Sequence[MorningIssue]) -> str:
    kinds = {issue.kind for issue in issues}
    if kinds == {MISSING_FINAL_OUT}:
        return "your swipe-out wasn't recorded"
    if kinds == {OUT_WITHOUT_IN}:
        return "your swipe-in wasn't recorded"
    return "a swipe may be missing"


def morning_check(
    *,
    employee_name: str,
    work_day: date,
    issues: Sequence[MorningIssue],
    report_url: str,
    privacy_url: str,
    sent_on: date | None = None,
) -> RenderedEmail:
    """Sent the next morning when a day's swipes don't add up.

    sent_on defaults to the day after work_day; when the gap is longer (a
    weekend), the subject names the date instead of saying "Yesterday".
    """
    day = fmt_day(work_day)
    yesterday = sent_on is None or sent_on - timedelta(days=1) == work_day
    label = f"Yesterday's attendance ({day})" if yesterday else f"Your attendance on {day}"
    subject = f"{label}: {_morning_tail(issues)}"
    name = _one_line(employee_name)
    sentences = [issue_sentence(issue) for issue in issues] or [
        issue_sentence(MorningIssue(kind=""))
    ]
    intro = (
        f"Something in your swipes on {day} doesn't line up:"
        if len(sentences) == 1
        else f"A few things in your swipes on {day} don't line up:"
    )
    ask = "If a swipe was missed, tell us what happened and HR will correct your hours."

    bullet_text = "\n".join(f"- {s}" for s in sentences)
    text = (
        f"Hi {name},\n\n{intro}\n\n{bullet_text}\n\n{ask}\n\n"
        f"Report a missed swipe: {report_url}\n\n"
        f"{NO_ACTION_LINE}\n\n{_alert_footer_text(privacy_url)}\n"
    )
    body = (
        _paragraphs([f"Hi {_e(name)},", _e(intro)])
        + _bullets(_e(s) for s in sentences)
        + _paragraphs([_e(ask)])
    )
    page = _layout(
        f"Your attendance on {day}",
        body,
        button=("Report a missed swipe", report_url),
        after_button=_small(_e(NO_ACTION_LINE)),
        footer=_alert_footer_html(privacy_url),
    )
    return RenderedEmail(subject, text, page)


# --- Fix requests -----------------------------------------------------------


def line_sentence(line: FixLine) -> str:
    at = fmt_time(line.at)
    if line.kind == MISSED_IN:
        return f"Missed swipe-in at {at}"
    if line.kind == MISSED_OUT:
        return f"Missed swipe-out at {at}"
    if line.kind == FLIP:
        target = {"in": "a swipe-in", "out": "a swipe-out"}.get(
            (line.direction or "").lower(), "the other direction"
        )
        return f"The {at} swipe was {target}"
    if line.kind == IGNORE:
        return f"Ignore the swipe at {at}"
    return f"Change to the swipe at {at}"


def request_recipients(
    manager_email: str | None, hr_emails: Iterable[str]
) -> tuple[list[str], list[str]]:
    """Split notification recipients into (HR variant, manager variant).

    A manager who is also on the HR list gets one copy, the HR variant.
    """
    hr = dedupe_recipients(hr_emails)
    hr_keys = {address.casefold() for address in hr}
    manager = dedupe_recipients([manager_email or ""])
    return hr, [m for m in manager if m.casefold() not in hr_keys]


def new_request(
    *,
    employee_name: str,
    work_day: date,
    lines: Sequence[FixLine],
    note: str | None = None,
    review_url: str | None = None,
    for_hr: bool = False,
) -> RenderedEmail:
    """Notification of a new fix request.

    for_hr (or a review_url): the variant for the people who decide. With a
    review_url it carries a "Review request" button; without one (no
    ESSL_PUBLIC_URL) it points to Fix requests in the dashboard instead.
    Otherwise: the manager FYI variant, which states HR will decide.
    """
    for_hr = for_hr or bool(review_url)
    day = fmt_day(work_day)
    name = _one_line(employee_name)
    subject = f"Fix request from {name} for {day}"
    intro = f"{name} asked for a fix to their attendance on {day}:"
    sentences = [line_sentence(line) for line in lines]
    note = (note or "").strip()
    if review_url:
        closing = "Please review it and approve or reject each change."
    elif for_hr:
        closing = (
            "Please review it and approve or reject each change. "
            "Open Fix requests in the dashboard to review it."
        )
    else:
        closing = (
            "This is for your information only. HR will review the request "
            "and decide; there's nothing you need to do."
        )

    text_parts = [f"{intro}\n\n" + "\n".join(f"- {s}" for s in sentences)]
    if note:
        text_parts.append(f"Their note:\n{note}")
    text_parts.append(closing)
    if review_url:
        text_parts.append(f"Review request: {review_url}")
    text_parts.append(PRODUCT_NAME)
    text = "\n\n".join(text_parts) + "\n"

    body = _paragraphs([_e(intro)]) + _bullets(_e(s) for s in sentences)
    if note:
        body += _note_block("Their note", note)
    body += _paragraphs([_e(closing)])
    page = _layout(
        f"Fix request from {name}",
        body,
        button=("Review request", review_url) if review_url else None,
        footer=_e(PRODUCT_NAME),
    )
    return RenderedEmail(subject, text, page)


# --- Decision ---------------------------------------------------------------


_DECISION_SUBJECT = {
    APPROVED: "was approved",
    PARTIAL: "was partly approved",
    REJECTED: "wasn't approved",
}


def decision(
    *,
    employee_name: str,
    work_day: date,
    outcome: str,
    lines: Sequence[FixLine],
    hr_note: str | None = None,
    day_url: str | None = None,
) -> RenderedEmail:
    """HR's decision, sent to the employee. outcome is approved/partial/rejected."""
    if outcome not in _DECISION_SUBJECT:
        raise ValueError(f"Unknown fix outcome: {outcome!r}")
    day = fmt_day(work_day)
    name = _one_line(employee_name)
    subject = f"Your attendance fix for {day} {_DECISION_SUBJECT[outcome]}"
    if outcome == APPROVED:
        summary = f"HR approved your fix for {day}. Your hours now include it."
    elif outcome == PARTIAL:
        summary = (
            f"HR approved part of your fix for {day}. "
            "Your hours now include the approved changes."
        )
    else:
        summary = f"HR didn't approve your fix for {day}, so your hours stay as they were."

    def status(line: FixLine) -> str:
        if line.approved is None:
            return line_sentence(line)
        return f"{line_sentence(line)}: {'approved' if line.approved else 'not approved'}"

    sentences = [status(line) for line in lines]
    hr_note = (hr_note or "").strip()

    text_parts = [f"Hi {name},", summary]
    if sentences:
        text_parts.append("\n".join(f"- {s}" for s in sentences))
    if hr_note:
        text_parts.append(f"Note from HR:\n{hr_note}")
    if day_url:
        text_parts.append(f"View your day: {day_url}")
    text_parts.append(PRODUCT_NAME)
    text = "\n\n".join(text_parts) + "\n"

    body = _paragraphs([f"Hi {_e(name)},", _e(summary)])
    if sentences:
        body += _bullets(_e(s) for s in sentences)
    if hr_note:
        body += _note_block("Note from HR", hr_note)
    page = _layout(
        f"Your fix for {day}",
        body,
        button=("View your day", day_url) if day_url else None,
        footer=_e(PRODUCT_NAME),
    )
    return RenderedEmail(subject, text, page)
