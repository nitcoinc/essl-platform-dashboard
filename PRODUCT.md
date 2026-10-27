# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Four roles:

- **employee** checks their own punches, effective hours and progress against the daily target, and reports a problem with one of their own last 7 days (from an alert email link or their Day view). Lands on Today.
- **manager** does the same, plus sees and exports the hours of everyone who reports to them, directly or indirectly. Gets one FYI email when a report files a fix request; never approves and never gets alerts. Lands on Team.
- **hr** sees and exports every employee, manages the Directory (roster file import, add, edit, archive and restore, managers, roles up to hr), approves or rejects fix requests (never their own), and reads device health. Nobody takes backups by hand: a scrubbed snapshot of the database lands in R2 every 15 minutes, plus one kept per day. Lands on Employees.
- **admin** has everything hr has, plus grants admin, adds, edits, retires and re-enables devices, and sets the daily target, attendance alert mode and hours, and HRMS API keys. An admin approves fix requests only when no active HR user other than the requester exists.

Vocabulary: **Employees** is the HR/admin hours list (managers see the same page as **Team**); **Directory** is the list of people under People. Never "Everyone" or "Roster" for either; "roster" means only the import file.

Each person has at most one manager. Nobody can see someone outside their scope; such a request looks exactly like a missing record.

## Product Purpose

Turn raw eSSL door-scanner punches into gross and effective hours per employee, so staff can tell during the day whether they have met the 8-hour target instead of finding out a week later from a manually calculated shortfall. Effective hours (gross minus breaks) is the number that counts; a day that cannot be paired into complete in/out segments is labelled incomplete and contributes zero, never a guessed time.

## Positioning

One system understands both eSSL device families — the X990 attendance protocol and the C3 access-controller protocol — and computes hours from their raw punches on every read, so a correction to the hours rule fixes every past day at once. Everything runs on one Linux host in the office: a collector that talks to the devices over the private network, the dashboard, and a backup service that snapshots the database to Cloudflare R2 every 15 minutes. The database is one local SQLite file on the host, shared by the collector and the dashboard. People reach the dashboard from anywhere through a Cloudflare Tunnel; a read-only API feeds the customer's HRMS.

## Operating Context

One Docker Compose stack on an office Linux host with routes to `192.168.1.201` (Uppal) and `192.168.1.202` (Gachibowli); device protocols are used only from that private network. Data lives in one local SQLite file on the host's data folder, so everything except emailed sign-in codes and the off-site backup keeps working when the internet drops, and a punch shows on the dashboard as soon as the collector stores it. The dashboard has no published port; it is reachable only through the tunnel. Employees sign in with a work email and an emailed six-digit code — there is no password.

## Capabilities and Constraints

- Seeded devices: Uppal X990 (`192.168.1.201`) and Gachibowli C3-400 (`192.168.1.202`). The devices table is authoritative: an admin adds devices of either family on the Devices page, and retires or re-enables them. A retired device is not polled and keeps its punches. Two active devices never share an IP:port, and device ids are immutable and never reused.
- X990 collection supports the standard ZK protocol with TCP and UDP fallback; its communication key is intentionally not assumed. As of 2026-09-10 the device refuses TCP 4370 while its port-80 web interface answers — the signature of ADMS/cloud-push mode — which needs an operator with device or web-console access to confirm; it cannot be diagnosed further from this codebase.
- C3-400 collection uses the PullSDK protocol on port 4370; its communication password is intentionally not assumed. Its realtime event queue holds only about 30 records in memory and is consumed once read, so the collector must stay running continuously.
- Punches originate only from these devices. No screen, role, or API creates, edits, or deletes a punch. A broken day is shown as incomplete until HR approves a fix the employee asked for; HR never edits a day without a request.
- An approved fix is an **overlay**: a correction row beside the punches (add a swipe, ignore a swipe, or set a swipe's direction) that every hours read path merges in, so views, exports and the hours API agree. Device punches stay untouched, and Frappe HR and `/api/v1/checkins` receive only raw punches, never fixes.
- Hours are always recomputed from stored punches, never stored as a derived value.
- Device deletion/clearing/reset operations are out of scope and must never be exposed. Retire is the only way to stop polling a device.
- Communication passwords can be set by an admin on the Devices page; R2, tunnel and email credentials come only from the server's `.env` (mode 600), never browser fields or source code.
- Only the backup service holds an R2 token, scoped to the one backup bucket (R2 has no write-only permission). The dashboard and the alerts service hold only mail-provider credentials; the collector holds none. Snapshots never contain sessions, sign-in codes, report links or queued emails.

## People, device IDs and archive

- Each person has one **main device ID** and any number of **extra IDs** (for example the same person enrolled as `NP0009` on the X990 and `9` on a C3 without zero padding). Punches under any of them count as that person's. An ID belongs to at most one person, main or extra, across both lists.
- Typed IDs are trimmed and upper-cased. An ID whose digits are all zero (`0`, `00`, `NP0000`) is refused: devices use 0 for "no user", so it would collect unrelated punches.
- Linking an unknown ID from ID issues adds it as an extra ID when the person's main ID already has punches; otherwise it replaces the main ID.
- **Archive** is for people who have left. An archived person leaves every working view, is signed out and cannot sign in; their punches, past hours and IDs are kept, exports and the hours API still include them for ranges where they punched, and their IDs never show as unknown. Archiving is refused for the last admin or anyone who still has reports. A roster import never edits or silently restores an archived person. Restore brings them back unchanged.

## Break rules

- Every In and Out swipe is a real record and always counts. A swipe with no direction first fills a missing alternation (Out, swipe, Out: the swipe is In; In, swipe, In: it is Out) and is marked "inferred".
- Otherwise a direction-less swipe, or a same-direction re-swipe, sooner than its own device's quick-swipe window after the previous counted swipe is a duplicate: stored and shown "not counted" with the swipe it repeats and the gap, never paired. The window is set per device (0–300 s, 0 counts every swipe, default 60); a change re-scores every past day.
- A gap between two paired stretches is a **break** only when it is a clean out-then-in. A gap holding an unpaired swipe is **unclear**: totalled separately, never counted as break, so gross = effective + breaks + unclear.
- The C3 records event 20 ("Too Short Punch Interval") when a swipe comes too soon after the previous one at the same door; the door does not open, but the person was there. The collector stores it as attendance with the note "interval too short". Recommend setting the C3 door **Punch Interval** to 1 second or less so real swipes are not denied; admins can change it from the Devices page.
- Interlock and anti-passback denials count only when a C3's "Wait for door close" is unchecked, from that moment on, flagged "door denied" and kept out of Frappe.

## Missed-punch alerts and fix requests

- A separate **alerts** service learns each active person's normal day from their own last 8 weeks, rebuilt weekly: their longest break (limit at least 90 minutes, at most 4 hours) and when they usually leave. Days with unclear gaps, unpaired swipes or a fix request never train it. Deterministic rules and robust statistics only; no LLM, and no attendance data leaves the host for detection.
- **Daytime email:** someone who came in today and has been out 15 minutes past their limit, before their usual leaving time and inside the alert hours (default 09:00-20:00). **Next-morning email:** yesterday ended on an In, had an Out with no In, two Ins, or an unclear gap. No email of any kind before 10 punched days; no daytime email before 10 clean days. At most one of each per person per day, never to someone who did not swipe.
- Never alert on stale collector data; a burst over 5 people or 20% of those in is held as a likely outage. A day with an unconfirmed C3 direction guess waits for the history read.
- **Modes:** off, shadow (record, send nothing; the default) and live, set by an admin. Live needs `ESSL_PUBLIC_URL` and at least one HR user.
- Emails are warm and factual, assume the device missed a swipe, show only times and the out-stretch, and never reveal the person's limit or use words like violation, anomaly, flagged or suspicious.
- **Fix requests:** at most one pending request per person per day, up to 10 lines and a 500-character note. "Everything's correct" and "I left for the day" close the day with no HR step. HR decides each line, may adjust its time or direction, and must give a note to reject. Every decision is audited. Report links are random, stored hashed, bound to one person and day, expire 7 days after it, and do nothing on GET.
- A short public notice at `/privacy` explains what is used, why, who sees it, how long it is kept, and that alerts are never used for discipline.

## Evidence on Hand

- Live network evidence on 2026-09-10: the X990 host answered ping and exposed HTTP on port 80, while TCP 4370 did not accept a connection; the C3-400 accepted TCP connections on port 4370 and, live-tested, correctly reported direction bytes matching the KTD4 alternation model.
- Ten real employees' captured punches confirmed clean in/out alternation, including short breaks of 5-10 minutes — the evidence that effective-hours accounting is possible at all.
- NITCO brand assets (wordmark, N mark, wave art) were supplied and are used on the sign-in pages and as the favicon. Sample data used anywhere in the UI is clearly labelled as sample.

## Product Principles

- Punches are the record; hours are derived, never hand-entered. A correction is an approved, audited overlay, never an edited punch.
- An incomplete day is shown honestly as incomplete, never guessed into a number.
- Make device state and recovery actions obvious to the admin, without ever exposing a destructive control.
- Treat credentials, network access, and employee punch data as operational secrets.
- Be honest when firmware-specific fields cannot be normalized or a device's behavior cannot be confirmed remotely.

## Accessibility & Inclusion

Use keyboard-accessible controls, visible focus states, responsive layouts down to 375px, and WCAG AA text contrast in light and dark themes. Charts carry a text title and an adjacent data table, and status is never conveyed by color alone.
