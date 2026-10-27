---
name: eSSL Attendance Hub
description: A calm, server-rendered internal tool for attendance hours. Neutral surfaces, one accent, numbers first.
colors:
  # Light theme, the default (dark values in static/styles.css under [data-theme="dark"])
  gray-0: "#ffffff"
  gray-25: "#fafafb"
  gray-50: "#f4f4f6"
  gray-100: "#ebebef"
  gray-200: "#dedee4"
  gray-300: "#c9c9d1"
  gray-500: "#6b6b76"
  gray-600: "#55555f"
  gray-900: "#17171c"
  ledger-blue: "#3451c7"
  ledger-blue-dark-theme: "#8197f5"
typography:
  family: "Inter (self-hosted variable, static/fonts), system-ui fallback"
  scale: [12, 13, 14, 16, 20, 28]
  weights: [400, 500, 600]
  numerals: tabular-nums
spacing: [4, 8, 12, 16, 20, 24, 32, 40, 48]
rounded:
  sm: "4px"
  control: "6px"
  container: "8px"
controls:
  height-desktop: "34px (28px small)"
  height-touch: "44px below 768px"
breakpoint: "768px"
---

# Design System: eSSL Attendance Hub

## Intent

An internal tool people open several times a day to answer one question: have I (or has my team) done the hours. The interface stays quiet so the numbers carry the page. Reference points: Linear, Vercel dashboard, macOS settings. Not a marketing surface.

## Tokens

All tokens live on `:root` (light, the default) in `static/styles.css`, redefined for dark under `:root[data-theme="dark"]` and, for `data-theme="system"`, under `prefers-color-scheme: dark` (keep the two dark blocks identical). The server renders `<html data-theme>` from the `theme` cookie (`light|dark|system`, 1 year, SameSite=Lax), so there is no flash; light when unset. Every token used in CSS is defined in both themes. Never hard-code a color in a template; use a token or a component class.

- **Neutral ramp:** `--gray-0` ... `--gray-900`, mapped to roles `--bg`, `--surface`, `--surface-sunken`, `--sidebar`, `--border`, `--border-strong`, `--border-input`, `--text`, `--text-secondary`, `--text-tertiary`.
- **Accent - Ledger blue** (`#3451c7` light, `#8197f5` dark): `--accent`, `--accent-hover`, `--accent-subtle`. Used only for the primary action, links, focus rings, today's row tint and the chart "met" fill (`--chart-met`, with its lighter tint `--chart-short` for short days). Nothing else is blue.
- **Status:** `--ok-*`, `--warn-*`, `--bad-*`, `--info-*`, `--neutral-*` (text, bg, dot). Appear only inside chips.
- **Chart roles:** `--chart-met`, `--chart-short`, `--chart-target`, `--chart-incomplete`, `--chart-grid`, `--chart-today` (plus legacy `--chart-worked`, `--chart-live`, `--chart-break`). Charts are SVG computed in Python and read these tokens.
- **Week chart:** bars on a fixed 0-10h scale (grows in 2h steps), faint gridlines, hour ticks in a right gutter where the dashed amber target is labelled at its end. Bars have rounded tops and a flat base, solid `--chart-met` when the target is met, `--chart-short` when short. An incomplete day is a short hatched, dashed-outline marker labelled "Incomplete", never a height. Value labels sit above the bar and move above the target line rather than touch it. Today gets a `--chart-today` column band and a bold day name. Each column carries a hover `<title>`.
- **Day timeline:** closed segments use `--chart-met` on a met day and `--chart-short` otherwise; the open segment is `--chart-met`; an orphan entry is a thin `--chart-incomplete` tick.
- **Type:** `--text-12/13/14/16/20/28`. Body 14px. Page title 20px/600. Big figures 28px/600. Weights 400/500/600 only.
- **Space:** `--s1`..`--s12` on a 4px grid.

Contrast (checked): body text 17:1, secondary 7.4:1, tertiary 4.8:1, accent on white 6.7:1, every chip text on its tint above 6:1, in both themes. All pass WCAG AA.

## Components

Markup helpers are Jinja macros in `templates/_ui.html`; icons are a Lucide sprite in `templates/_icons.html`.

- **Buttons** (`ui.button`): `primary`, `secondary`, `tertiary`, `danger`; 34px tall, 6px radius, 44px under 768px. One primary per view. Labels name the action ("Save target", not "Submit").
- **Chips** (`ui.chip`, legacy `.status-chip.status-*`): 22px, 4px radius, a 6px dot plus a text label. Color never carries meaning alone.
- **Tables** (`ui.table`, `.hours-table`): one bordered container, sticky muted header, 1px row dividers, numeric columns `.num` right-aligned with tabular numerals. Under 768px rows stack into label/value pairs.
- **Fields** (`ui.field`): label above input, hint below, `aria-invalid` plus `ui.inline_error` on failure. Errors say what went wrong and how to fix it.
- **Empty state** (`ui.empty_state`): icon, one line on what is missing, one on what to do, optional action.
- **Quick-view drawer** (`dialog#quick-view` in `base.html`, `static/quick-view.js`): a right-side sheet (full width under 768px) opened by plain clicks on a person's name on Employees and People › Directory; modified clicks and no-JS fall through to the full page. Sticky header with name and Close, identity facts, the Day strip, today's `_day_live` region, footer actions pinned at the bottom. Neutral `--shadow-card`, slide-in of 150ms or less, none under reduced motion. Esc, Close and backdrop close it; focus returns to the name.
- **Day strip** (`.day-strip`): 7 work days as a row of `.day-chip` links (weekday, date number, a status icon in the dot (Lucide check for punched, minus for no punches) plus a word; when the strip is narrower than 520px, as on phones and in the drawer, only the icon shows and the word stays in the `aria-label`; the icon is a 10px glyph at 2.5 stroke inside a 14px dot, an exception to the 16px icon size), on Today, Day and in the drawer. It ends today, or a few days after an older selected day, and pages a week at a time with 44px *Earlier week* / *Later week* arrows (disabled at today and at the person's first punch). A **Go to date** picker sits below (a GET form on Day, an in-place swap in the drawer), bounded by the first punch day and today. Days before the first punch say so instead of looking empty. Arrows and chips share one row at 375px. The selected day carries `aria-current="date"`.
- **Day bar** (`_day_breaks.html`, `.day-bar`): a CSS-flex bar of one day, segment widths proportional to time. Inside stretches are solid `--chart-met`; breaks are hatched (45deg `--bar-break-stripe` stripes on `--surface-sunken`, 1px `--border-strong` inset); unclear gaps are a 2px dotted `--bar-unclear-edge` outline on `--surface`; an open stretch runs to now. Breaks and unclear gaps carry a small `.bar-num` badge, numbered from 1 within their kind, matching the list below. Start and end times sit under it in `.bar-axis`. Pattern, not just color, separates the kinds.
- **Break view** (`.day-breaks`): a summary line (Inside, Breaks total · count, Unclear in `--warn-text` when present), then the day bar, which carries the detail: hatched breaks show "number · duration", dotted unclear gaps "Unclear · duration" (shortened or dropped when the segment is too narrow). Ticks mark every boundary; time labels sit under them (first In at the left edge, last Out or "now" at the right, then break edges, longest gap first), and a label that would collide is dropped, with a separate set at phone width. The bar's `aria-label` lists inside, each break's times and unclear gaps. Below, **All punches (n)** is always listed (plain heading, no disclosure), with chips for *not counted* (duplicate, with muted reason text beside it: "same as In 09:00:00, 20s earlier"), *inferred* (direction filled from neighbours), *unpaired*, *interval too short* and *door denied (interlock / anti-passback)*. No break table.
- **Device card settings** (`devices.html`): *Swipe policy* is a disclosure with the window input (presets Off / 5 / 10 / 60 s as buttons with JS, a datalist without) and, on a C3, the *Wait for door close* checkbox with a one-line read of the panel's sensor and interlock state. *Door settings* (C3 only) show the last read as label/value lists, one `<details>` per door, and a state line: Not read yet (edit form hidden, **Read from device**), Pending (requested time; "Collector has not picked this up" after two poll intervals), Applied, or Failed / Expired with per-key results and **Dismiss**. Changes and **Restore previous values** go through `device_door_confirm.html`, listing old → new.
- **Archive tab** (`people_archive.html`, `_people_tabs.html`): People has subtabs Directory, ID issues, Archive. Archive is a plain table (name, device IDs, archived date, last punch) with **Restore** per row; archiving lives as a tertiary action at the bottom of the person form behind a confirm, never in a row. Empty state: "No one is archived".
- **Hours** (`ui.hours`): always `7h 42m`.
- **Navigation section** (`active_section`, `essl_hub/web.py` `nav_section`): every page highlights its sidebar section, worked out from the endpoint and whether the viewed person is you, not from exact endpoints. People sub-pages, the door confirm, fix review pages, your Week, someone else's day (Team/Employees) and Health (Settings) all have a current item. The People subtab (Directory, ID issues, Archive) carries its own `aria-current`.
- **Summary line** (`#team-count`, Team/Employees): one aria-live line, "7 people: 4 inside · 2 met · 1 short · 1 no punches". Each person is counted once by today's status, zero parts are dropped, and htmx search swaps a recounted line out of band.
- **Archived punches** (`punch_archives.html`, ID issues): the admin action sits on Not-in-Directory and Shared-card cards. It opens an inline confirm with from/to dates, an optional reason and the resolved range and count; Confirm is disabled at 0. The list shows active archives (Restore for admins) above a muted restored section. Empty state: "No archived punches".
- **Report form** (`fix_report.html`, `_fix_report_form.html`): one partial in two shells: the emailed link renders inside `_auth_base.html` (`.report-card`, no nav), the signed-in entry inside `base.html`. Header "Your swipes on <day>" plus one line saying the device records never change. Each swipe is a row (time, direction, chips) with two checkboxes, "This was the other direction" and "Ignore this swipe"; then an "Add a missed swipe" group of time + In/Out rows; then an optional note. One primary, **Send to HR**; below a rule, **Everything's correct** and **I left for the day** as secondaries. Fields use `ui.field` errors; touch targets 44px under 768px; works without JS. State banners above the form: with HR (pending lines), decided (chip Approved / Partly approved / Not approved plus HR's note), closed ("You told us ..."). The footer links "How we use attendance data" (`/privacy`, same auth shell). An expired link gets one plain page with a sign-in link.
- **Fix review** (`fix_review.html`, `fix_review_detail.html`, `fix_review_confirm.html`, HR): a list with pending requests first, `ui.empty_state` when none, and a nav count badge with an accessible name, like ID issues. The detail page shows the day's raw swipes and each requested line with editable time and direction and an approve/reject choice (default approve); the note is required when any line is rejected. Warnings ("Day changed since report", "Swipe no longer exists") are `.device-warning`-style text with an icon, never color alone. Submitting goes to a confirm page summarising each decision with before → after hours, as in the door-settings confirm. Someone viewing their own request sees it read-only.
- **Alert emails** (`essl_hub/alert_mail.py`): the sign-in email's table layout, header logo (`cid:nitco-logo`, Resend only; the Cloudflare provider sends the plain-text part alone) and `color-scheme` meta, plus a plain-text part with the same link. Subject is date first and factual. Body: a greeting, what the device recorded (times and the out-stretch only), one button (**Report a missed swipe**, or **Review request** for HR), then "No action needed if this is right" and a footer link to the privacy notice. The manager FYI has no button. Tone is warm and plain, no humor; it blames the reader, never the person.
- **Attendance alerts panel** (`settings.html`, admin): mode select (Off / Shadow — log only / Live — send emails) with a hint that fix-request emails go out in every mode, alert start and end hours, morning email hour, one primary **Save alert settings**. A refusal (live without `ESSL_PUBLIC_URL` or an HR user) is a field error on the mode select. Below: "N of M people have enough history" and when baselines were last rebuilt, then a `ui.table` of the last 7 days per site with Daytime and Next morning cells reading "2 would send, 1 sent, 1 held". A fresh install shows `ui.empty_state` ("Baselines not built yet").

## Shell

Sidebar (232px) with brand, role-aware navigation and the signed-in person (initials avatar, name, role) above a full-width labelled "Sign out" button (secondary). That is the only sign-out on desktop; there is no account menu. Top-right of the page, the shell bar holds, for admin only, the sync-health indicator (link to Sync health: dot + icon + text, `--ok`/`--warn`/`--bad` tints for the worst of its signals: collector poll and spool, backups, and any active device whose last poll failed; per-signal detail in `title` and the accessible name; backups with no R2 configured show a neutral "Off" chip that never sets the tone), then the theme switch: a Light / Dark / System segmented control. The switch is a POST form to `/preferences/theme` with `csrf` and a local `next`; a small inline script applies the choice without a reload. Sign-out is always a POST form with the `csrf` field; one `nav` landmark, one `main`, a skip link. Entries follow the plan's Navigation-by-role table and appear only once their route exists. Under 768px the sidebar becomes a top bar with a `<details>` menu disclosure whose panel holds the nav, the sync-health indicator and theme switch, then the person and the labelled "Sign out"; the shell bar is hidden. A `csrf-token` meta tag and `hx-headers` on `body` let htmx requests pass `_require_csrf` (header `X-CSRF-Token` or form field `csrf`).

## Rules (anti-slop)

- No gradients, glows, glass, blur or colored shadows. (Exceptions: the legend's hatch swatch and the day bar's break segments, repeating stripes that mirror the chart's incomplete pattern; and the brand surface below.)
- No cards inside cards. A section is a heading plus content, separated by space or a 1px rule.
- No emoji, no unicode glyphs as icons. Icons are Lucide, 16px, 1.75 stroke.
- No metric tiles unless a decision hangs on the number.
- No eyebrow labels above headings, no pill buttons, no colored side borders.
- Plain copy in the product's own words. Incomplete days say "Incomplete", never a guessed number.
- Motion is 150ms or less, for state feedback only, and is removed under `prefers-reduced-motion`.
- Focus is always visible: 2px accent outline, 2px offset.

## Brand surface

The sign-in, code, setup and offline pages (`_auth_base.html`) carry the NITCO brand: a wave-art panel (`static/brand/wave.*`) with the white wordmark and "Attendance Hub", beside a form column that uses the normal tokens. Here only, the supplied gradient art, a fixed navy darkening overlay for text legibility, and the brand's own navy and red are allowed; they are brand values, not app tokens, and never appear inside the app shell. The favicon set lives in `static/brand/`.
