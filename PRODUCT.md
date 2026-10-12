# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Four roles:

- **employee** checks their own punches, effective hours and progress against the daily target. Lands on Today.
- **manager** does the same, plus sees and exports the hours of everyone who reports to them, directly or indirectly. Lands on Team.
- **hr** sees and exports everyone, manages people (roster import, add and edit, managers, roles up to hr), and reads device health. Nobody takes backups by hand: Turso keeps point-in-time history and a nightly export lands in R2. Lands on Everyone.
- **admin** has everything hr has, plus grants admin, adds, edits, retires and re-enables devices, and sets the daily target and HRMS API keys.

Each person has at most one manager. Nobody can see someone outside their scope; such a request looks exactly like a missing record.

## Product Purpose

Turn raw eSSL door-scanner punches into gross and effective hours per employee, so staff can tell during the day whether they have met the 8-hour target instead of finding out a week later from a manually calculated shortfall. Effective hours (gross minus breaks) is the number that counts; a day that cannot be paired into complete in/out segments is labelled incomplete and contributes zero, never a guessed time.

## Positioning

One system understands both eSSL device families — the X990 attendance protocol and the C3 access-controller protocol — and computes hours from their raw punches on every read, so a correction to the hours rule fixes every past day at once. Everything runs on one Linux host in the office: a collector that talks to the devices over the private network, the dashboard, and a nightly read-only export to Cloudflare R2. The database itself lives in Turso (hosted libSQL); each service keeps a local replica. People reach the dashboard from anywhere through a Cloudflare Tunnel; a read-only API feeds the customer's HRMS.

## Operating Context

One Docker Compose stack on an office Linux host with routes to `192.168.1.201` (Uppal) and `192.168.1.202` (Gachibowli); device protocols are used only from that private network. Data lives in Turso; each service reads from its own embedded replica on a local volume, so reads keep working when the internet drops (writes show an offline page, and the collector spools punches until Turso is back). The dashboard has no published port; it is reachable only through the tunnel. Employees sign in with a work email and an emailed six-digit code — there is no password.

## Capabilities and Constraints

- Seeded devices: Uppal X990 (`192.168.1.201`) and Gachibowli C3-400 (`192.168.1.202`). The devices table is authoritative: an admin adds devices of either family on the Devices page, and retires or re-enables them. A retired device is not polled and keeps its punches. Two active devices never share an IP:port, and device ids are immutable and never reused.
- X990 collection supports the standard ZK protocol with TCP and UDP fallback; its communication key is intentionally not assumed. As of 2026-09-10 the device refuses TCP 4370 while its port-80 web interface answers — the signature of ADMS/cloud-push mode — which needs an operator with device or web-console access to confirm; it cannot be diagnosed further from this codebase.
- C3-400 collection uses the PullSDK protocol on port 4370; its communication password is intentionally not assumed. Its realtime event queue holds only about 30 records in memory and is consumed once read, so the collector must stay running continuously.
- Punches originate only from these devices. No screen, role, or API creates, edits, or deletes a punch — a broken day is shown as incomplete, not corrected in the app.
- Hours are always recomputed from stored punches, never stored as a derived value.
- Device deletion/clearing/reset operations are out of scope and must never be exposed. Retire is the only way to stop polling a device.
- Communication passwords can be set by an admin on the Devices page; Turso, R2, tunnel and email credentials come only from the server's `.env` (mode 600), never browser fields or source code.
- Only the nightly export holds an R2 token, scoped to the one export bucket (R2 has no write-only permission; bucket lock protects old exports). It reads Turso with a read-only token.

## Evidence on Hand

- Live network evidence on 2026-09-10: the X990 host answered ping and exposed HTTP on port 80, while TCP 4370 did not accept a connection; the C3-400 accepted TCP connections on port 4370 and, live-tested, correctly reported direction bytes matching the KTD4 alternation model.
- Ten real employees' captured punches confirmed clean in/out alternation, including short breaks of 5-10 minutes — the evidence that effective-hours accounting is possible at all.
- No brand assets were supplied. Sample data used anywhere in the UI is clearly labelled as sample.

## Product Principles

- Punches are the record; hours are derived, never hand-entered.
- An incomplete day is shown honestly as incomplete, never guessed into a number.
- Make device state and recovery actions obvious to the admin, without ever exposing a destructive control.
- Treat credentials, network access, and employee punch data as operational secrets.
- Be honest when firmware-specific fields cannot be normalized or a device's behavior cannot be confirmed remotely.

## Accessibility & Inclusion

Use keyboard-accessible controls, visible focus states, responsive layouts down to 375px, and WCAG AA text contrast in light and dark themes. Charts carry a text title and an adjacent data table, and status is never conveyed by color alone.
