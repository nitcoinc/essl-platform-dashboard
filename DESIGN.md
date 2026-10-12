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
- **Hours** (`ui.hours`): always `7h 42m`.

## Shell

Sidebar (232px) with brand, role-aware navigation and the signed-in person (initials avatar, name, role) above a full-width labelled "Sign out" button (secondary). That is the only sign-out on desktop; there is no account menu. Top-right of the page, the shell bar holds, for admin only, the sync-health indicator (link to Sync health: dot + icon + text, `--ok`/`--warn`/`--bad` tints for the worst of three signals: web replica sync, collector poll and spool, nightly export; per-signal detail in `title` and the accessible name; a local-database instance shows a neutral "Local database" chip), then the theme switch: a Light / Dark / System segmented control. The switch is a POST form to `/preferences/theme` with `csrf` and a local `next`; a small inline script applies the choice without a reload. Sign-out is always a POST form with the `csrf` field; one `nav` landmark, one `main`, a skip link. Entries follow the plan's Navigation-by-role table and appear only once their route exists. Under 768px the sidebar becomes a top bar with a `<details>` menu disclosure whose panel holds the nav, the sync-health indicator and theme switch, then the person and the labelled "Sign out"; the shell bar is hidden. A `csrf-token` meta tag and `hx-headers` on `body` let htmx requests pass `_require_csrf` (header `X-CSRF-Token` or form field `csrf`).

## Rules (anti-slop)

- No gradients, glows, glass, blur or colored shadows. (The one exception is the legend's hatch swatch, a repeating stripe that mirrors the chart's incomplete pattern.)
- No cards inside cards. A section is a heading plus content, separated by space or a 1px rule.
- No emoji, no unicode glyphs as icons. Icons are Lucide, 16px, 1.75 stroke.
- No metric tiles unless a decision hangs on the number.
- No eyebrow labels above headings, no pill buttons, no colored side borders.
- Plain copy in the product's own words. Incomplete days say "Incomplete", never a guessed number.
- Motion is 150ms or less, for state feedback only, and is removed under `prefers-reduced-motion`.
- Focus is always visible: 2px accent outline, 2px offset.
