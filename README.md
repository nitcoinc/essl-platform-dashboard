<div align="center">

# eSSL Attendance Hub

**Door-scanner punches in, working hours out.**
A small, self-hosted attendance dashboard for eSSL X990 and C3-400 devices, backed by Turso and served through Cloudflare.

<sub>Built and maintained by the engineering team at <b>NITCO Inc.</b></sub>

![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.1-000000?logo=flask)
![Turso libSQL](https://img.shields.io/badge/database-Turso%20libSQL-4FF8D2)
![Docker Compose](https://img.shields.io/badge/deploy-Docker%20Compose-2496ED?logo=docker&logoColor=white)
![Cloudflare Tunnel](https://img.shields.io/badge/edge-Cloudflare%20Tunnel-F38020?logo=cloudflare&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

<img src="docs/screenshots/today.png" alt="Today page: progress ring, first in and last out, attendance log" width="860">

</div>

---

## What it does

- **Collects** punches from two door devices over the office LAN, every minute, even while the internet is down (offline spool, replayed later).
  - Uppal: eSSL X990 at `192.168.1.201`
  - Gachibowli: eSSL C3-400 at `192.168.1.202`
- **Computes** gross and effective hours, breaks and target status per person per day. Hours are recomputed from stored punches on every read.
- **Shows** each person their day and week; managers see their reports, HR and admins see everyone, with CSV/Excel export.
- **Flags ID problems** (punches nobody on the roster owns, people with no device ID) and fixes them in one click.
- **Feeds Frappe HR** (Employee Checkin push) and offers a read-only HRMS API.
- **Signs in** by emailed one-time code; no passwords. Cloudflare Turnstile guards the sign-in form.

Punches come only from the devices. No screen, role or API creates, edits or deletes a punch.

## Screenshots

| Today (live, inside) | Week |
| --- | --- |
| <img src="docs/screenshots/today.png" width="420" alt="Today"> | <img src="docs/screenshots/week.png" width="420" alt="Week"> |
| **Everyone** (HR, admin) | **ID issues** (HR, admin) |
| <img src="docs/screenshots/everyone.png" width="420" alt="Everyone"> | <img src="docs/screenshots/id-issues.png" width="420" alt="ID issues"> |
| **Devices** (admin) | **Sign in** |
| <img src="docs/screenshots/devices.png" width="420" alt="Devices"> | <img src="docs/screenshots/signin.png" width="420" alt="Sign in"> |
| **Dark mode** | **Phone** |
| <img src="docs/screenshots/today-dark.png" width="420" alt="Today, dark mode"> | <img src="docs/screenshots/today-mobile.png" width="200" alt="Today on a phone"> |

<sub>Screenshots use a demo database with fictional people.</sub>

## Quick start

On the office host, once the [prerequisites](#prerequisites) are ready:

```bash
git clone <this repo> /opt/essl && cd /opt/essl
./deploy/install.sh
```

The installer walks you through everything:

1. Checks Docker, the compose plugin and the CPU architecture, and says exactly what to run if one is missing.
2. Creates `.env` (mode 600) and **asks for each missing value**: device IPs and passwords, Turso URL and tokens, R2 keys and a mail provider. A Tunnel token is optional (see [Tunnel](#1-tunnel)). Secrets are typed hidden.
3. Generates a one-time **setup token** for the first administrator.
4. Builds the images and checks the data volume.
5. **Tests both door devices** from inside the container (`Network` and `Protocol` must PASS).
6. Starts the stack, waits until the web app answers, and prints the setup token and next steps.

Then open your public hostname, go to `/setup`, enter the token, and import the roster from **People → Import roster**.

| Command | What it does |
| --- | --- |
| `./deploy/install.sh` | Full install **or update** (pulls the latest code first). Safe to re-run; it only fills empty values. |
| `./deploy/install.sh --check` | All checks and the build, starts nothing. Good before a change window. |
| `./deploy/install.sh --yes` | Never prompts; fails on missing values (scripted hosts). |
| `./deploy/install.sh --skip-devices` | Skips the device test (e.g. away from the office LAN). |
| `./deploy/install.sh --no-pull` | Installs the code already checked out (no `git pull`). |
| `./deploy/install.sh --skip-backup` | Skips the pre-update R2 snapshot. |

### Updating

Run the same command: `./deploy/install.sh`. On an existing install it:

1. Fast-forwards to the latest `main`, then re-runs the new installer. It refuses to pull over local edits to tracked files.
2. Backs up `.env` to `.env.bak.<time>` (keeps the last 5) and adds any settings the new release introduced, with their defaults.
3. Uploads a fresh database snapshot to R2 before the schema can change (the nightly export, run once now). If that fails it asks before continuing.
4. Builds, restarts only changed containers, waits for the web app, and prunes old images. If the web app does not come up, it prints the `git checkout <previous>` command to roll back and exits non-zero.

A lock file stops two runs overlapping. The very first time you move to this installer from an older checkout, run `git pull` once by hand.

## Prerequisites

| You need | Why | Where it comes from |
| --- | --- | --- |
| **Linux host, x86_64**, on the office LAN | Runs the stack; must reach both devices on TCP 4370 | Any recent Ubuntu or Debian. The `libsql` client ships x86_64 wheels only |
| **Docker Engine + compose plugin** | Runs every service | `curl -fsSL https://get.docker.com \| sh` then `sudo usermod -aG docker "$USER" && newgrp docker` |
| **Turso database** + two tokens | The database (hosted libSQL) | [Turso setup](#turso-setup), done once from a laptop |
| **Cloudflare account** with your domain on its DNS | Tunnel (no open ports), Turnstile, R2 | [Cloudflare setup](#cloudflare-setup) |
| **Cloudflare Tunnel**: one already on the host, or a token for the bundled one | The only way in from the internet | [Tunnel](#1-tunnel) |
| **R2 bucket** + bucket-scoped API token | Nightly export copy | [R2 bucket](#2-r2-bucket-nightly-export) |
| **Mail provider**: Resend or Cloudflare Email Sending | Sends sign-in codes | [Sign-in email](#3-sign-in-email) |
| Device **IP addresses** and **comm passwords** | Pulling punches | Device menu / ZKAccess; `ESSL_X990_IP`, `ESSL_X990_PASSWORD`, `ESSL_C3_IP`, `ESSL_C3_PASSWORD`. Addresses in this README (`192.168.1.201`, `192.168.1.202`) are examples |
| *Optional:* Turnstile keys, Frappe API key | Bot check on sign-in; Frappe HR push | [Protection](#4-protection-bots-and-ddos), [Frappe HR integration](#frappe-hr-integration) |

No Python is needed on the host: the installer and the device test run inside the containers.

### Requirements at a glance

| | |
| --- | --- |
| Runtime | Python 3.12 (in the image), Flask 3.1, gunicorn, htmx |
| Database | Turso (libSQL) with an embedded replica per service |
| Device libraries | `pyzk` (X990), `zkaccess-c3` plus a built-in chunked reader (C3-400) |
| Host resources | Small: web + collector use about 140 MB RAM together; 1 vCPU, 1 GB RAM, 2 GB disk is plenty |
| Network | Outbound HTTPS (Turso, Cloudflare); LAN TCP 4370 to both devices; **no inbound ports** |
| Browsers | Current Chrome, Edge, Safari, Firefox; works on phones |

## Contents

1. [How it runs](#how-it-runs)
2. [Turso setup](#turso-setup)
3. [Cloudflare setup](#cloudflare-setup)
4. [Install on the Linux host](#install-on-the-linux-host)
5. [First administrator](#first-administrator)
6. [Fixing ID issues](#fixing-id-issues)
7. [Fix the Uppal address](#fix-the-uppal-address)
   - [Add, retire or re-enable a device](#add-retire-or-re-enable-a-device)
   - [Sign-in branding](#sign-in-branding)
8. [Cutover from the Postgres stack](#cutover-from-the-postgres-stack)
9. [Sync health and offline behaviour](#sync-health-and-offline-behaviour)
10. [Nightly export](#nightly-export)
11. [Recovery](#recovery)
12. [Turso plan limits](#turso-plan-limits)
13. [Roles](#roles)
14. [Roster columns](#roster-columns)
15. [Device setup and troubleshooting](#device-setup-and-troubleshooting)
16. [Frappe HR integration](#frappe-hr-integration)
17. [Configuration reference](#configuration-reference)
18. [Development and tests](#development-and-tests)

## How it runs

Everything is one `docker-compose.yml` on one x86_64 host that can reach both device subnets. The database lives in **Turso** (hosted libSQL). Each app service keeps its own embedded replica file on the `essl-data` volume, reads from it locally and sends writes to Turso.

```
 office LAN                         office host (docker compose)                      internet
 ----------                         ----------------------------                      --------
 X990  192.168.1.201 --TCP 4370--+
                              +--> collector --- writes / sync ------------------+
 C3-400 192.168.1.202 -TCP 4370--+     |  collector-replica.db                       |
                                    |  collector-spool.db  (punches while offline)|
                                    |  collector-status.json                      v
                                    |                                       +-----------+
                                    |   web (gunicorn) --- writes / sync -->|   Turso   |
 browsers <-- Cloudflare Tunnel <-- cloudflared --> web                     |  primary  |
                                         web-replica.db                     +-----------+
                                                                                  ^
                                    nightly-export --- read-only sync ------------+
                                         export-replica.db
                                         |  02:00 IST, gzip, 1 bucket
                                         +-------------------------------> R2 nightly/
```

| Service | Job |
| --- | --- |
| `collector` | Polls the devices every `ESSL_COLLECTOR_INTERVAL_SECONDS` and writes punches. Boots and polls without the internet; a punch write that fails goes to the local spool and is replayed in order later. Syncs its replica every `TURSO_COLLECTOR_SYNC_SECONDS`. |
| `web` | Flask + gunicorn (`-w 1 --threads 4`). The screens and the read-only HRMS API. Owns the schema. Syncs its replica every `TURSO_WEB_SYNC_SECONDS`, so collector punches show up within about a minute. Published on host loopback only (`127.0.0.1:ESSL_WEB_PORT`, default 8080). |
| `nightly-export` | Once a night, syncs a read-only replica, strips sessions and login codes, integrity-checks, gzips and puts it to R2 under `nightly/`. |
| `cloudflared` | *Optional* bundled Cloudflare Tunnel (compose profile `tunnel`). Skip it when the host already runs cloudflared. |

Every process holds one libSQL connection for its life (one replica file per process), so a process always reads its own writes. Status files on the shared volume (`collector-status.json`, `export-status.json`) feed the admin sync-health indicator even when Turso is unreachable. All containers run as UID/GID 10001. Postgres is gone from the runtime, and so are continuous file backups: Turso's point-in-time recovery plus the nightly export replace them.

## Turso setup

Do this once, from an **operator laptop**, not the office host. The host never needs the Turso CLI or a platform token.

1. Install the CLI and sign in: see https://docs.turso.tech/cli/introduction, then `turso auth login`.
2. Create the database in the location nearest India. List locations with `turso db locations` and pick the closest one:

   ```bash
   turso db create essl --location <code>      # for the cutover, use --from-file instead (below)
   turso db show essl --url                    # -> TURSO_DATABASE_URL
   ```

3. Create the two database tokens:

   ```bash
   turso db tokens create essl                 # read-write -> TURSO_AUTH_TOKEN (collector, web)
   turso db tokens create essl --read-only     # read-only  -> TURSO_EXPORT_AUTH_TOKEN (nightly-export)
   ```

   Keep them only in the host's `.env` (mode 600) and your password manager. `install.sh` refuses to start if the two are the same.
4. **Optional dev branch for testing.** Never point dev or a scratch host at production. Branch a copy instead:

   ```bash
   turso db create essl-dev --from-db essl
   turso db show essl-dev --url
   turso db tokens create essl-dev
   ```

   Use these values on the scratch host or in a dev shell. Destroy it with `turso db destroy essl-dev` when done.
5. Set a **usage alert** in the Turso dashboard (see [Turso plan limits](#turso-plan-limits)).

## Cloudflare setup

Do this once, before touching the host. You need a Cloudflare account whose DNS hosts your company domain.

### 1. Tunnel

Pick one:

- **Host already runs cloudflared** (recommended if you have one): add a public hostname, e.g. `attendance.yourcompany.com`, service `HTTP`, URL `localhost:8080` (`ESSL_WEB_PORT`). Leave `TUNNEL_TOKEN` empty; the bundled container stays off.
- **Bundled tunnel**: **Zero Trust → Networks → Tunnels → Create a tunnel**, type Cloudflared. Put its token in `TUNNEL_TOKEN` (the installer then sets `COMPOSE_PROFILES=tunnel`). Public hostname URL: `web:8000`.

The web port binds to `127.0.0.1` only, and compose sets `ESSL_BEHIND_TUNNEL=1`, so per-IP sign-in limits use `CF-Connecting-IP`. Never expose the port beyond loopback while that flag is set; behind a different proxy, set `ESSL_BEHIND_TUNNEL=0`.

#### LAN access (optional)

To also open the dashboard at `http://<server-ip>:8080` inside the office, set in `.env`:

```
ESSL_WEB_BIND=0.0.0.0
ESSL_FORCE_HTTPS_COOKIES=false
```

then `docker compose up -d web`. Trade-offs: sign-in cookies are no longer HTTPS-only (also through the tunnel), and anyone on the LAN can send a fake `CF-Connecting-IP` to dodge per-IP sign-in limits. Keep the host firewall closed to 8080 from outside the office.

### 2. R2 bucket (nightly export)

You need four values. There is **no URL to copy**: the app builds the endpoint `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` itself.

| `.env` key | Where to find it |
|---|---|
| `R2_ACCOUNT_ID` | R2 overview page, right sidebar "Account ID" (also the hex string in the dashboard URL) |
| `R2_BUCKET` | Bucket **name** you typed at creation, e.g. `essl-attendance` (not a bucket ID) |
| `R2_EXPORT_ACCESS_KEY_ID` | "Access Key ID" shown after creating the API token |
| `R2_EXPORT_SECRET_ACCESS_KEY` | "Secret Access Key" shown once after creating the token -- copy it then |

Steps:

1. **R2 → Create bucket**, e.g. `essl-attendance`. Keep it private (no public access, no custom domain).
2. **Bucket → Settings → Object lifecycle rules → Add rule**: prefix `nightly/`, delete after 90 days (your retention). Without it exports pile up forever.
3. **Optional: Bucket → Settings → Bucket lock** on `nightly/` for your retention period. Even a leaked token then cannot delete or overwrite recent exports.
4. **R2 → Manage R2 API Tokens → Create API token**: permission **Object Read & Write**, **Specify bucket** → only this bucket, TTL forever. Copy the Access Key ID and Secret Access Key. Ignore the "Token value" and the S3 endpoint shown there; the app needs neither.

R2 has no write-only permission, so this token can also read and delete objects in this one bucket. It lives only in the `nightly-export` container. Exports include `devices.comm_password` (a restore needs it), so keep the bucket private and use bucket lock if you want old exports tamper-proof.

### 3. Sign-in email

Codes go out through **Cloudflare Email Sending** by default, with **Resend** as a fallback.

- Cloudflare Email Sending needs the **Workers Paid** plan ($5/month) and is in beta. Onboard the sending domain under **Email → Email Sending** (it adds SPF/DKIM records on Cloudflare DNS). Create an API token with the Email Sending permission. Set `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_FROM_ADDRESS`.
- Resend fallback: verify the domain in Resend, then set `RESEND_API_KEY` and `RESEND_FROM_ADDRESS`.

With both configured, Cloudflare wins unless `ESSL_MAIL_PROVIDER=resend`. Switching is an env change plus `docker compose up -d web`. A send failure is logged, never shown to the person signing in, so the sign-in page cannot reveal who is on the roster.

### 4. Protection (bots and DDoS)

The app has **no open port**: `cloudflared` dials out to Cloudflare, so every request reaches it through Cloudflare's network, and the host's IP is never published. That gives you, with no setup:

- **DDoS protection** (L3/L4 and HTTP), always on, every plan, unmetered.
- **TLS** at Cloudflare's edge.

Add these in the dashboard for your domain (all available on the Free plan):

1. **Turnstile (bot check on sign-in).** **Turnstile → Add widget**: name `essl-attendance`, hostname `attendance.yourcompany.com`, mode **Managed**. Copy the **Site key** into `TURNSTILE_SITE_KEY` and the **Secret key** into `TURNSTILE_SECRET_KEY`. The sign-in and first-run setup forms then show the check, and the server verifies it with Cloudflare before looking up the email or sending any code. Most people see no puzzle at all. Without both keys the check is off (fine for local testing only).
2. **Rate limiting.** **Security → WAF → Rate limiting rules → Create rule**: when *URI Path* equals `/signin` and *Method* is `POST`, 5 requests per 1 minute per IP, action **Block** for 10 minutes. This sits in front of the app's own per-IP and per-email sign-in limits.
3. **Bot Fight Mode.** **Security → Bots → Bot Fight Mode: On.** Challenges known bad bots site-wide. Leave the HRMS API (`/api/v1`, Bearer keys) reachable: if Frappe pulls start failing with 403, add a WAF *Skip* rule for path starting with `/api/v1`.
4. **Optional: restrict to India.** **Security → WAF → Custom rules**: *Country* not in `India` → **Managed Challenge**. Skip it if staff travel or use VPNs.
5. **Optional, strongest: Cloudflare Access.** **Zero Trust → Access → Applications → Self-hosted**, domain `attendance.yourcompany.com`, policy *Emails ending in* `@yourcompany.com` (free up to 50 users). Nobody reaches even the sign-in page without first proving a company email to Cloudflare. Add a *Bypass* policy for `/api/v1/*` if Frappe pulls from outside.

## Install on the Linux host

```bash
# 1. Docker Engine + compose plugin (skip if installed)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER" && newgrp docker

# 2. Code
git clone <this repo> /opt/essl && cd /opt/essl

# 3. Install (asks for anything missing, tests the devices, starts the stack)
./deploy/install.sh
```

Prefer editing by hand? Run `./deploy/install.sh --yes` once to create `.env`, fill every `# REQUIRED` value and one mail provider (`nano .env`), then run `./deploy/install.sh` again.

The installer stops on a non-x86_64 host, checks every required value (Turso URL format, separate read-only export token), builds, tests the devices and starts. There is no first-install data step: Turso is the source of truth, and each service builds its replica from it on first start.

Check it is healthy:

```bash
docker compose ps                       # collector, web, nightly-export, cloudflared running
docker compose logs -f collector        # one line per device per poll
docker compose run --rm --no-deps collector python diagnose.py   # device test again, any time
```

Open `https://attendance.yourcompany.com`.

**Updating:** `./deploy/install.sh` (it pulls, backs up and restarts; see [Updating](#updating)).

## First administrator

An empty database has no one to email a code to. Pick one way:

- **Setup page (default).** The installer generates `ESSL_SETUP_TOKEN` and prints it. Open `/setup`, enter the token, your name and work email (and badge number if you scan in). The page returns 404 once any admin exists. Unset the token afterwards.
- **Env only.** Set `ESSL_BOOTSTRAP_ADMIN_EMAIL` to an address already on the roster. While no admin exists, the web service promotes that row on boot.

Then sign in, open **People → Import roster** and upload the roster. Everyone else is managed from **People**.

## Fixing ID issues

Hours count only when the ID on the door device matches a person on the roster. **People → ID issues** (HR and admin; the People menu shows a red count) lists every mismatch in plain words and fixes it in place:

| Issue | Means | One-click fix |
| --- | --- | --- |
| **Not on roster** | Someone punches with an ID nobody on the roster has; their hours are lost | **Link to *name*** (suggested when names match), **Add as new person** (prefilled), or pick anyone from the dropdown |
| **No device ID** | A person's ID is still their email, so no punch can match | **Use NP0xxx** (suggested) or type it in **Set ID** |
| **Never punched** | Nobody has scanned with this person's ID | Same as above: correct the ID |
| **Name differs** | Device and roster name the same ID differently | **Same person** (hides it) or **Fix ID** |
| **Shared card** | Temporary cards in `ESSL_C3_UNLINKED_PINS` | Nothing; never counted |

Nothing is stored about an issue: the list is rebuilt from the roster and the punches on every load. Fix the roster (here, on the person form, or by re-importing) and the row disappears; that person's past punches count immediately, no re-collection needed. Linking an ID shows how many past punches it brought in.

## Fix the Uppal address

The Uppal X990 is at `192.168.1.201`. Older installs stored `192.168.1.200`. `ESSL_X990_IP` only seeds a device row on first insert; after that the stored row wins, so editing `.env` does not fix a live row.

1. Sign in as admin and open **Devices**.
2. Uppal → **Edit connection** → IP `192.168.1.201`, port `4370` → **Save connection**.
3. The card shows **Change pending** until the collector's next poll, then **Change applied**. No restart.

Devices is admin-only; HR handles ID problems from **People → ID issues**.

## Add, retire or re-enable a device

The devices table is the source of truth; `.env` IPs only seed the first two rows. On **Devices** (admin):

- **Add device**: site, family (X990 attendance or C3-400 access controller), IP, port and an optional comm password (numeric for the X990). The id is generated from site and family (`kondapur-c3-400`, then `-2`, `-3` on a clash), never changes and is never reused. The card reads **Pending** until the collector picks it up, about 6 minutes (replica sync plus one poll).
- **Retire device** (asks to confirm): sets `retired_at`. The collector stops polling it, it never trips the stale-collector warning, and its punches stay on every day view. Retired cards collapse under **Retired**.
- **Re-enable**: clears `retired_at`; polling resumes on the same id.

Two active devices can't share an IP:port. Adding an address that belongs to a retired device offers **Re-enable** instead of a new id, so history stays under one id. There is no delete.

## Sign-in branding

On **Settings → Sign-in branding** (admin) upload a logo and pick the sign-in background: **Default**, **Brand colour** (hex) or **Uploaded image**. The sign-in, code and setup pages show them in light and dark; the card itself stays solid so its text keeps AA contrast, and dark theme dims a background image with a scrim.

- Formats: PNG, JPEG or WebP, checked by file signature (the extension is ignored). SVG is refused.
- Caps: logo 512 KB, background 1.5 MB (plus the global `ESSL_MAX_UPLOAD_MB`).
- Images live in the database (`branding_assets`, schema v5), so every replica and the nightly export carry them. They are served publicly at `/brand/logo` and `/brand/background` with an ETag and a 5-minute cache; the page adds `?v=<hash>` so a change shows at once.
- **Reset** puts either asset back to the default. With nothing uploaded, sign-in looks as before. Email keeps `static/email-logo.png`.

## Cutover from the Postgres stack

The old stack (Postgres + collector) keeps running until step 5. The C3-400 realtime queue holds only about 30 events and does not replay, so the gap between old and new collectors must stay short. Bulk data goes up as a file; only the gap is written row by row (KTD17).

1. **Rehearse** the whole sequence on a scratch host against a Turso dev branch or scratch database, and time it. Run the [restore drill](#restore-drill) once before go-live.
2. **Schedule an off-hours window** (no one at the doors), longer than the rehearsal.
3. **Full migrate to a local file while the old collector still runs.** On the operator laptop (or any machine that reaches the old Postgres). The `libsql` package ships Linux x86_64 wheels only; on another OS run these steps inside `docker run --rm -it --platform linux/amd64 -v "$PWD":/app -w /app python:3.12 bash`:

   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt "psycopg[binary]"
   .venv/bin/python scripts/migrate_postgres.py --source postgresql://user:pass@oldhost/essl --target essl-turso.db
   turso db create essl --from-file essl-turso.db --location <code>
   ```

   Note the time you started. The script prints per-table counts and a device/settings diff, exits non-zero on any mismatch, and prints the `turso db create` command. Sessions and login codes are not copied, so everyone signs in once more. Then create the tokens ([Turso setup](#turso-setup) step 3).
4. **Start the new stack**: fill `.env`, run `./deploy/install.sh`.
5. **Stop the old collector** as soon as `docker compose logs collector` shows the new one polling both devices. Do not run both against the C3 for long: each read drains its queue.
6. **Backfill the gap** with delta mode, using the start time from step 3. Web and collector must be stopped:

   ```bash
   docker compose stop collector web        # on the host
   ```

   Then on the operator laptop (same venv as step 3):

   ```bash
   export TURSO_DATABASE_URL=libsql://essl-<org>.turso.io TURSO_AUTH_TOKEN=<read-write token>
   .venv/bin/python scripts/migrate_postgres.py --source postgresql://user:pass@oldhost/essl \
       --punches-since "2026-10-09 18:00:00" --replica-path ./migrate-replica.db
   ```

   Back on the host: `docker compose up -d`. Punches are deduplicated by fingerprint, so overlap is harmless. Delete `migrate-replica.db*` afterwards.
7. **Keep the old Postgres stack stopped but intact for 14 days.** That is the rollback path: stop the new collector, start the old stack. After 14 clean days, remove it.

Then fix the Uppal row if it was migrated as `.42`.

## Sync health and offline behaviour

### Sync-health indicator

Admins see an indicator top right on every page. It links to **Sync health** (`/health`, admin only, 404 for everyone else) and shows the worst of three signals, each read from the shared volume so it still loads when Turso is down:

| Signal | Green | Amber | Red |
| --- | --- | --- | --- |
| Web replica sync | last OK within 5 min | within 60 min | older, or never |
| Collector | polled within 10 min, spool empty | punches spooled | no poll for 10 min, a dead-lettered punch, or the oldest spooled punch older than 60 min |
| Nightly export | last OK within 2 days | within 7 days | older, or none yet |

The page also lists the last 14 exports. A local-database instance (dev) shows a neutral "Local database" chip.

### When the internet or Turso is down

- **Reads keep working.** Every page and the `/api/v1` pulls are served from the web replica, at most one sync interval stale when the outage began.
- **Writes show an offline page (HTTP 503)**, never an error: sign-in code requests, roster edits, role changes, device and settings edits. Already signed-in people stay signed in. Retry once the indicator is green.
- **The collector keeps polling.** Punches go to `/data/collector-spool.db` and replay oldest-first once Turso is back; duplicates are ignored. A punch that keeps failing while syncs succeed moves to the spool's `dead` table (indicator red); it is kept, never dropped. Device-status, heartbeat and Frappe writes are skipped and recomputed next cycle.
- **The export** records a failed night and retries the next night.
- **First boot with no internet and no replica:** every page shows the offline page until the first sync.

### Stuck replicas rebuild themselves

A libsql 0.1.11 replica can stop advancing after Turso rolls to a new internal generation (seen live after a large first load), while its sync still reports success. After every sync, web and collector compare the newest punch and latest device poll in their replica with what Turso reported at the previous check. Two lagging checks in a row delete the replica file and pull a fresh copy (seconds for ~60k punches); the time shows as **Last replica rebuild** on Sync health. The nightly export always starts from a fresh replica. A rebuild now and then is harmless; one on every check means something else is wrong — check the logs for `behind primary`.

## Nightly export

`nightly-export` runs at `ESSL_EXPORT_HOUR` (default `2`, i.e. 02:00 IST). It syncs a read-only replica, copies it, deletes `sessions` and `login_codes`, runs `VACUUM` and `PRAGMA integrity_check`, gzips it and puts it to `R2_BUCKET` as `nightly/<timestamp>.db.gz`. Each run is appended to `/data/export-status.json` (last 30 kept) and shows on Sync health.

Run one now (e.g. before a large roster import, or to test the token):

```bash
docker compose run --rm nightly-export python -m essl_hub.nightly_export --once
```

It exits 0 on success and non-zero on failure. Logs: `docker compose logs nightly-export`.

## Recovery

Turso is the source of truth. There is no in-app restore. Pick a path, run it from the operator laptop, then repoint the host.

### Turso unreachable, data intact

Nothing to do. Reads continue, writes show the offline page, the collector spools. Once Turso is back the spool replays and the indicator turns green. If the collector shows dead-lettered punches, check `docker compose logs collector`; the rows stay in the spool's `dead` table.

### Path 1: Turso point-in-time recovery (preferred)

Restores to any moment inside your plan's PITR window (check it on your plan, see [Turso plan limits](#turso-plan-limits)):

```bash
turso db create essl-restored --from-db essl --timestamp 2026-10-09T18:00:00+05:30
```

### Path 2: a nightly export from R2

Use when the moment you need is outside the PITR window, or the Turso database itself is gone. Download `nightly/<timestamp>.db.gz` from the R2 dashboard, then:

```bash
gunzip <timestamp>.db.gz
turso db create essl-restored --from-file <timestamp>.db --location <code>
```

Exports exclude sessions and login codes, so everyone signs in again.

### Then, for either path

1. Mint new tokens for the new database: `turso db tokens create essl-restored` and `turso db tokens create essl-restored --read-only`.
2. Stop the stack: `docker compose down`.
3. Delete the old replicas but **keep the spool** (it may hold punches not yet in Turso):

   ```bash
   docker compose run --rm --no-deps --entrypoint sh web -c 'rm -f /data/*-replica.db*'
   ```

4. Update `.env`: `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `TURSO_EXPORT_AUTH_TOKEN`.
5. Start the stack: `docker compose up -d`. The replicas rebuild from the new database and the collector replays its spool into it.
6. Check the indicator turns green and recent hours look right. Keep the old database until you are sure, then `turso db destroy`.

### Restore drill

Do it once before cutover, then quarterly.

1. Run an export now (`--once`, above) and confirm the object under `nightly/` in R2 and an OK row on Sync health.
2. Restore it with path 2 to `essl-drill`, and do a path 1 restore to `essl-drill-pitr` from an hour ago.
3. On a scratch host, copy `.env`, point it at `essl-drill` with fresh tokens, leave `TUNNEL_TOKEN` empty, `docker compose up -d`. Confirm sign-in works and yesterday's hours match production.
4. Record the times. Destroy both drill databases and the scratch host. Never point a scratch stack at the production database.

## Turso plan limits

Turso plans cap rows read and written, storage, embedded-replica sync bandwidth and the point-in-time recovery window. These change; **check the current numbers on https://turso.tech/pricing** before cutover, and write the PITR window of your plan here: `PITR window: ____`.

What drives usage here:

- Replica syncs: web every `TURSO_WEB_SYNC_SECONDS` (60), collector every `TURSO_COLLECTOR_SYNC_SECONDS` (300), export once a night. Raising them cuts sync traffic; the cost is staler reads.
- Writes: punches, a device row on change or every `ESSL_HEARTBEAT_WRITE_SECONDS`, sign-ins and admin edits. Small at about 100 users.
- The nightly export reads the whole database once.

Set a **usage alert** in the Turso dashboard well below your plan's limits, and check usage monthly. If you hit a limit, writes can be refused: the indicator and the offline page show it.

## Roles

| Capability | employee | manager | hr | admin |
| --- | --- | --- | --- | --- |
| Own Today and Week | yes | yes | yes | yes |
| See others' hours | - | own reports, direct and indirect | everyone | everyone |
| Export CSV / Excel | - | own reports | everyone | everyone |
| Roster template and import | - | - | yes | yes |
| Add or edit a person, set manager | - | - | yes, except an admin's email or device user ID | yes |
| Set role employee / manager / hr | - | - | yes, not on admins | yes |
| Grant or revoke admin | - | - | - | yes, never the last admin |
| Devices | - | - | - | yes |
| ID issues (People → ID issues) | - | - | yes | yes |
| Edit a device connection | - | - | - | yes |
| Sync health (`/health`) | - | - | - | yes |
| Daily target, HRMS API keys (**Settings**) | - | - | - | yes |

| Role | Lands on | Sidebar |
| --- | --- | --- |
| employee | Today | Today, Week |
| manager | Team | Today, Week, Team |
| hr | Everyone | Today, Week, Everyone, People (with ID issues) |
| admin | Everyone | Today, Week, Everyone, People (with ID issues), Devices, Settings (plus the sync-health indicator) |

Asking for someone you may not see returns 404, the same as an id that does not exist. `/admin` redirects to **Devices**; `/devices/ids` redirects to **People → ID issues**.

## Attendance export

**Team / Everyone → Export hours** downloads up to 93 days for the people you can see (everyone, or only those ticked). Every sheet starts with the range and the work-day window (e.g. 06:00 to 06:00 next day, IST), and the whole export comes from one punch query.

Excel (`/export.xlsx`) has three sheets:

- **Daily Detail**: one row per person per work day. Emp ID, Name, Manager, Site(s), Date, Weekday, First In, Last Out, Punches, Gross, Break, Effective, Effective (h), Status, Shortfall vs target, Remarks. First In and Last Out are full datetimes shown `hh:mm:ss` (a night shift's Last Out falls on the next calendar date); durations are exact-second `[h]:mm:ss` values. Header and Emp ID/Name are frozen, autofilter is on. First In is the start of the first paired (or open) entry segment and Last Out the end of the last paired segment, so a stray swipe outside them does not move either; no entry segment leaves First In blank. Status is Met, Short, Met · review / Short · review (an unmatched swipe: paired time counts as Effective, Remarks names the swipe, matrix code R), Missing exit / Missing entry / Incomplete, Open, Absent or Weekend off. Last Out, durations and Shortfall stay blank where they would be invented (incomplete; Shortfall also for open and absent days); review days leave Gross and Break blank, since an unmatched mid-day swipe would otherwise read as break.
- **Monthly Matrix**: people × work days, each cell a code plus effective `H:MM`: P present, A absent, WO weekend off (Sat/Sun), R review, I incomplete, O open, blank before the person was added. Weekends are shaded. Totals: Present, Absent, Review, Incomplete, Total effective, Days below target.
- **Person Summary**: days present, average first in and average effective (complete days only), days below target, incomplete and review days, expected hours (weekdays × daily target) and actual hours.

CSV (`/export.csv`) is Daily Detail only: UTF-8 with a BOM so Excel opens it on double-click, `HH:MM` times and durations, plus the decimal Effective (h) column. Text that a spreadsheet would run as a formula is prefixed with `'` in every sheet and the CSV.

## Roster columns

Upload `.csv` (UTF-8) or `.xlsx` on **People → Import roster**. Download the template there. The first row is the header; column names are case-insensitive. Rows are matched on device user ID, so uploading the same file twice changes nothing. Up to 5,000 rows and `ESSL_MAX_UPLOAD_MB` (default 5 MB).

| Column | Also accepted | Required | Meaning |
| --- | --- | --- | --- |
| `name` | | yes | Full name as shown in the hub |
| `work email` | `email` | yes | The address the person signs in with |
| `device user id` | `device_user_id`, `id` | yes | The user ID enrolled on the door device |
| `manager email` | `manager` | no | Work email of their manager. A blank cell clears the manager. Leave the column out to keep current managers. Unknown, self or cyclic managers are skipped with a row warning; the rest of the row is kept. |
| `role` | | no | `employee`, `manager` or `hr`. Blank keeps the current role. Admin is granted only on People and is never changed by a roster. |

Only an admin's upload may change an existing admin's email or device user ID.

## Device setup and troubleshooting

### X990 (Uppal)

1. **Menu → Comm → Ethernet**: IP `192.168.1.201`, correct mask and gateway, TCP port `4370`. Disable DHCP or reserve the address.
2. **Menu → Comm → PC Connection**: put the Comm Key digits in `ESSL_X990_PASSWORD` (`0` when disabled). It must be numeric.
3. `.venv/bin/python diagnose.py uppal-x990`.

If it stops reporting:

1. **Wrong IP or port**: fix it on **Devices** (see [Fix the Uppal address](#fix-the-uppal-address)). `.env` only seeds a new row.
2. **`comm password for Uppal must be a number`**: set the digits in `ESSL_X990_PASSWORD`, or on Devices tick **Clear the stored password and use the server's default**.
3. **`authentication failed`**: the Comm Key does not match. Re-read it on the device.
4. **TCP 4370 refuses while ping answers**: check **Menu → Comm → Cloud Server**. ADMS/push mode closes the pull port; it needs someone at the device to change.
5. **Timeouts**: the host cannot reach `192.168.1.201`. `diagnose.py` `Network` FAIL means routing or firewall on the host.
6. **Devices says "Not checked recently"**: the collector has not written for more than two heartbeats. `docker compose ps collector` and `docker compose logs collector`.

### C3-400 (Gachibowli)

1. POWER lit, RUN flashing, Ethernet LINK lit.
2. In ZKAccess/eSSL software confirm IP `192.168.1.202`, port `4370` and the communication password. Put it in `ESSL_C3_PASSWORD`.
3. Pause other access-control software during the first test, then `diagnose.py gachibowli-c3-400`.

The C3 sends big tables in chunks, which the bundled library cannot read, so the collector downloads them itself (`read_c3_table` in `essl_hub/collectors.py`, checked against the panel's CRC). With `ESSL_C3_MODE=auto` it pulls the full history (about 30,000 events, 80 seconds on the tested `AC Ver 18.1.2`) at start and every `ESSL_C3_HISTORY_HOURS`, and reads the live queue on the polls between. Only access-granted swipes count; denied swipes, the exit button and door alarms are skipped. The panel allows one session at a time, so pause ZKAccess or other tools while the collector runs.

**IDs.** The C3 stores bare PINs (`9`); the X990 and the roster use `NP0009`. `ESSL_C3_ID_FORMAT` (default `NP{pin:04d}`) maps one to the other. Shared cards listed in `ESSL_C3_UNLINKED_PINS` (default `1,2`, the Temporary cards) are stored as `C3-1`, `C3-2` and never count for anyone. Punches stored before this mapping are fixed with `python scripts/remap_c3_ids.py --apply`.

Never factory-reset a device to test connectivity.

## Frappe HR integration

Frappe HR expects biometric logs to be **pushed** into its Employee Checkin doctype; Auto Attendance on each Shift Type then marks attendance. The collector does this, like Frappe's official biometric-attendance-sync-tool:

1. In Frappe HR set **Employee → Attendance Device ID** to each person's eSSL device user ID.
2. Create an API key/secret for an integration user (Employee Checkin create, Shift Type write).
3. Enable **Auto Attendance** on Shift Types and assign shifts.
4. Set the `FRAPPE_*` variables on the collector and restart it.

Each cycle (at most every `FRAPPE_SYNC_INTERVAL_SECONDS`) new punches are POSTed to `/api/method/hrms.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field` with `employee_field_value`=device user ID, `timestamp`, `device_id`, and `log_type` IN/OUT (entry/exit; omitted when direction is unknown, so Frappe alternates). Per-punch state lives in `frappe_sync_log` (schema v2):
- `synced`: created, or Frappe reported a duplicate.
- `skipped`: no Employee with that Attendance Device ID / inactive employee / missing ID. Not retried; to retry after fixing the Employee, delete its rows: `DELETE FROM frappe_sync_log WHERE status='skipped';`
- `failed`: other 4xx validation error, not retried.
- Network errors, 5xx, 401/403/429: nothing recorded, run stops, retried next cycle.

After a run with nothing left pending, each `FRAPPE_SHIFT_TYPES` entry gets `last_sync_of_checkin` = oldest successful device poll (only moves forward), so Auto Attendance does not mark absences for punches not yet collected. Last run status is in `settings.frappe_last_sync` (JSON).

### Pull alternative
`GET /api/v1/checkins?since=<local ISO datetime>&limit=<1..1000, default 500>` with `Authorization: Bearer <key>` returns records shaped as the method's arguments (`employee_field_value`, `employee_fieldname`, `timestamp`/`time`, `device_id`, `log_type`). `since` is exclusive; pass back `next_since`. Pages never split one second. A Frappe-side scheduled job can feed each record to `add_log_based_on_employee_field`.

## Configuration reference

All settings live in `.env`; `.env.example` documents each one. The daily target and API keys are set on **Settings**, not in env.

| Variable | Default | Used by |
| --- | --- | --- |
| `TURSO_DATABASE_URL` | required (unset = local mode, dev only) | collector, web, nightly-export |
| `TURSO_AUTH_TOKEN` | required, read-write | collector, web |
| `TURSO_EXPORT_AUTH_TOKEN` | required, read-only | nightly-export |
| `TURSO_WEB_SYNC_SECONDS` / `TURSO_COLLECTOR_SYNC_SECONDS` | `60` / `300` | passed as `TURSO_SYNC_SECONDS` to web / collector |
| `ESSL_DB_PATH` | set per service in compose (`/data/<service>-replica.db`) | replica file; local database file in dev |
| `ESSL_SPOOL_PATH` | `/data/collector-spool.db` | collector |
| `ESSL_COLLECTOR_STATUS_PATH`, `ESSL_EXPORT_STATUS_PATH` | next to `ESSL_DB_PATH` | collector, nightly-export, web (reads) |
| `R2_ACCOUNT_ID`, `R2_BUCKET` | required | nightly-export |
| `R2_EXPORT_ACCESS_KEY_ID`, `R2_EXPORT_SECRET_ACCESS_KEY` | required, scoped to the one bucket | nightly-export |
| `ESSL_EXPORT_HOUR` | `2` (IST) | nightly-export |
| `ESSL_WEB_PORT` / `ESSL_WEB_BIND` | `8080` / `127.0.0.1` | web host port; bind `0.0.0.0` for LAN access |
| `TUNNEL_TOKEN`, `COMPOSE_PROFILES` | optional; token + `tunnel` turns on the bundled cloudflared | cloudflared |
| `ESSL_COLLECTOR_INTERVAL_SECONDS` | `60` | collector, Devices page |
| `ESSL_HEARTBEAT_WRITE_SECONDS` | `300` | collector, Devices staleness warning |
| `ESSL_X990_IP` / `_PORT` / `_PASSWORD` | `192.168.1.201` / `4370` / `0` | collector (seeds the row once) |
| `ESSL_C3_IP` / `_PORT` / `_PASSWORD` | `192.168.1.202` / `4370` / blank | collector (seeds the row once) |
| `ESSL_C3_MODE`, `ESSL_C3_HISTORY_HOURS`, `ESSL_C3_RTLOG_BATCH` | `auto`, `6`, `250` | collector |
| `ESSL_C3_ID_FORMAT`, `ESSL_C3_UNLINKED_PINS` | `NP{pin:04d}`, `1,2` | collector: C3 PIN to roster ID |
| `ESSL_MAIL_PROVIDER` | auto | web: `cloudflare`, `resend`, or `console` (local testing only: prints the code to the server log) |
| `CLOUDFLARE_API_TOKEN` / `_ACCOUNT_ID` / `_FROM_ADDRESS` | | web |
| `RESEND_API_KEY` / `RESEND_FROM_ADDRESS` | | web |
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | recommended; both set = bot check on sign-in and setup | web |
| `ESSL_FORCE_HTTPS_COOKIES` | `true` | web (`false` only for local http) |
| `ESSL_MAX_UPLOAD_MB` | `5` | web |
| `ESSL_SETUP_TOKEN`, `ESSL_BOOTSTRAP_ADMIN_EMAIL` | unset | web, first admin only |
| `ESSL_BEHIND_TUNNEL` | `1` | web (`0` behind a non-Cloudflare proxy) |

## Development and tests

Dev and tests run in **local mode**: with `TURSO_DATABASE_URL` unset, the app opens `ESSL_DB_PATH` as a plain local libSQL file, no Turso account needed.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ESSL_DB_PATH=./data/essl.db ESSL_FORCE_HTTPS_COOKIES=false
.venv/bin/python -m essl_hub.collector_main             # collector
.venv/bin/flask --app 'essl_hub.web:create_app()' run   # dashboard on :5000
```

To try replica mode, point `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` at a dev branch ([Turso setup](#turso-setup) step 4), never at production.

```bash
.venv/bin/python -m pytest -q
```

The suite needs no network or database server. `tests/test_libsql_contract.py` pins the libSQL behaviour the app depends on; its Turso group runs only when `TURSO_TEST_URL` and `TURSO_TEST_TOKEN` (plus `TURSO_TEST_RO_TOKEN` for the read-only check) point at a scratch database, and is skipped otherwise (as in CI). `psycopg` is only for the one-time migration script.

---

## About

eSSL Attendance Hub is built and maintained by the team at **NITCO Inc.** It started as an internal tool for our Hyderabad offices (Uppal and Gachibowli) and is shared in the hope that it helps other teams running eSSL / ZKTeco door devices.

- **Issues and ideas:** open a GitHub issue with steps to reproduce, the device model and firmware, and the relevant `docker compose logs` lines (remove tokens and personal data first).
- **Contributions:** pull requests are welcome. Run `.venv/bin/python -m pytest -q` before opening one; the suite needs no network or device.
- **Security:** please report vulnerabilities privately to the maintainers rather than in a public issue.

Released under the [MIT License](LICENSE).

eSSL and ZKTeco are trademarks of their respective owners; this project is not affiliated with or endorsed by them. Third-party components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

<div align="center"><sub>© 2026 NITCO Inc. · Made in Hyderabad</sub></div>
