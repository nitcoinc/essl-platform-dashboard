<div align="center">

# eSSL Attendance Hub

**Door-scanner punches in, working hours out.**
A small, self-hosted attendance dashboard for eSSL X990 and C3-400 devices, backed by a local SQLite database with 15-minute backups to Cloudflare R2, served through Cloudflare.

<sub>Built and maintained by the engineering team at <b>NITCO Inc.</b></sub>

![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.1-000000?logo=flask)
![SQLite](https://img.shields.io/badge/database-SQLite%20(libSQL)-003B57?logo=sqlite&logoColor=white)
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
- **Shows** each person their day and week; managers see their reports, HR and admins see all employees, with CSV/Excel export.
- **Flags ID problems** (punches nobody in the Directory owns, people with no device ID) and fixes them in one click, including adding a second device ID to the same person.
- **Archives** people who have left: they drop out of every working view and cannot sign in, while their punches and past hours stay.
- **Feeds Frappe HR** (Employee Checkin push) and offers a read-only HRMS API.
- **Signs in** by emailed one-time code; no passwords. Cloudflare Turnstile guards the sign-in form.
- **Catches missed swipes** by learning each person's normal day and emailing them when a day looks wrong; they report what happened and HR approves the fix ([Missed-punch alerts and fix requests](#missed-punch-alerts-and-fix-requests)).

Punches come only from the devices. No screen, role or API creates, edits or deletes a punch. An approved fix request is stored beside the punches and changes the computed hours; the device punch itself is never touched.

## Screenshots

| Today (live, inside) | Week |
| --- | --- |
| <img src="docs/screenshots/today.png" width="420" alt="Today"> | <img src="docs/screenshots/week.png" width="420" alt="Week"> |
| **Employees** (HR, admin) | **ID issues** (HR, admin) |
| <img src="docs/screenshots/employees.png" width="420" alt="Employees"> | <img src="docs/screenshots/id-issues.png" width="420" alt="ID issues"> |
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
2. Creates `.env` (mode 600) and **asks for each missing value**: device IPs and passwords, R2 keys and a mail provider. A Tunnel token is optional (see [Tunnel](#1-tunnel)). Secrets are typed hidden.
3. Generates a one-time **setup token** for the first administrator.
4. Builds the images, prepares the data folder (owner 10001, mode 700) and, on a fresh install, creates the database (`backup init`).
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
3. Uploads a fresh database snapshot to R2 before the schema can change (`backup now --allow-older-schema`: the new image is built by then, so the database may be one schema step behind). If that fails it asks before continuing. On an install still on Turso it stops here and prints the [upgrade steps](#upgrading-from-turso).
4. Builds, restarts only changed containers, waits for the web app, and prunes old images. If the web app does not come up, it prints the `git checkout <previous>` command to roll back and exits non-zero.

A lock file stops two runs overlapping. The very first time you move to this installer from an older checkout, run `git pull` once by hand.

## Prerequisites

| You need | Why | Where it comes from |
| --- | --- | --- |
| **Linux host, x86_64**, on the office LAN | Runs the stack; must reach both devices on TCP 4370 | Any recent Ubuntu or Debian. The `libsql` client ships x86_64 wheels only |
| **Docker Engine + compose plugin** | Runs every service | `curl -fsSL https://get.docker.com \| sh` then `sudo usermod -aG docker "$USER" && newgrp docker` |
| **Cloudflare account** with your domain on its DNS | Tunnel (no open ports), Turnstile, R2 | [Cloudflare setup](#cloudflare-setup) |
| **Cloudflare Tunnel**: one already on the host, or a token for the bundled one | The only way in from the internet | [Tunnel](#1-tunnel) |
| **R2 bucket** + bucket-scoped API token | Off-site backups every 15 minutes | [R2 bucket](#2-r2-bucket-backups) |
| **Mail provider**: Resend or Cloudflare Email Sending | Sends sign-in codes | [Sign-in email](#3-sign-in-email) |
| Device **IP addresses** and **comm passwords** | Pulling punches | Device menu / ZKAccess; `ESSL_X990_IP`, `ESSL_X990_PASSWORD`, `ESSL_C3_IP`, `ESSL_C3_PASSWORD`. Addresses in this README (`192.168.1.201`, `192.168.1.202`) are examples |
| *Optional:* Turnstile keys, Frappe API key | Bot check on sign-in; Frappe HR push | [Protection](#4-protection-bots-and-ddos), [Frappe HR integration](#frappe-hr-integration) |

No Python is needed on the host: the installer and the device test run inside the containers.

### Requirements at a glance

| | |
| --- | --- |
| Runtime | Python 3.12 (in the image), Flask 3.1, gunicorn, htmx |
| Database | One local SQLite file (WAL), shared by web and collector; snapshots to R2 every 15 min |
| Device libraries | `pyzk` (X990), `zkaccess-c3` plus a built-in chunked reader (C3-400) |
| Host resources | Small: web + collector use about 140 MB RAM together; 1 vCPU, 1 GB RAM, 2 GB disk is plenty. Data folder on a native Linux filesystem |
| Network | Outbound HTTPS (Cloudflare); LAN TCP 4370 to both devices; **no inbound ports** |
| Browsers | Current Chrome, Edge, Safari, Firefox; works on phones |

## Contents

1. [How it runs](#how-it-runs)
2. [Backups](#backups)
3. [Cloudflare setup](#cloudflare-setup)
4. [Install on the Linux host](#install-on-the-linux-host)
5. [First administrator](#first-administrator)
6. [Fixing ID issues](#fixing-id-issues) (including archiving old or test card punches)
   - [Archiving people](#archiving-people)
   - [Breaks and duplicate swipes](#breaks-and-duplicate-swipes)
7. [Fix the Uppal address](#fix-the-uppal-address)
   - [Add, retire or re-enable a device](#add-retire-or-re-enable-a-device)
   - [Sign-in branding](#sign-in-branding)
8. [Cutover from the Postgres stack](#cutover-from-the-postgres-stack)
9. [Sync health and offline behaviour](#sync-health-and-offline-behaviour)
10. [Recovery](#recovery)
11. [R2 plan limits](#r2-plan-limits)
12. [Upgrading from Turso](#upgrading-from-turso)
13. [Roles](#roles)
14. [Missed-punch alerts and fix requests](#missed-punch-alerts-and-fix-requests)
15. [Roster columns](#roster-columns)
16. [Device setup and troubleshooting](#device-setup-and-troubleshooting)
17. [Frappe HR integration](#frappe-hr-integration)
18. [Configuration reference](#configuration-reference)
19. [Development and tests](#development-and-tests)

## How it runs

Everything is one `docker-compose.yml` on one x86_64 host that can reach both device subnets. The database is **one local SQLite file** (`essl.db`, WAL mode) in the data folder, shared by the web app, the collector and the alerts service. Every read and write is local, so a punch shows on the dashboard as soon as the collector stores it. The `backup` service copies a scrubbed snapshot to Cloudflare R2 every 15 minutes; that is the off-site copy.

```
 office LAN                         office host (docker compose)                 internet
 ----------                         ----------------------------                 --------
 X990  192.168.1.201 --TCP 4370--+
                                 +--> collector --writes--+
 C3-400 192.168.1.202 -TCP 4370--+                        v
                                              /data (ESSL_DATA_DIR or volume essl-data)
                                                essl.db  (+ -wal, -shm)
                                                collector-spool.db, *-status.json
                                                          ^       |
 browsers <-- Cloudflare Tunnel <-- cloudflared --> web --+       |
                                                                  |  every 15 min: snapshot,
                                              backup ------------+  scrub, check, gzip
                                                 +------------------------------> R2 recent/, nightly/
                                              alerts --reads/writes essl.db, sends queued email--> Resend / Cloudflare
```

| Service | Job |
| --- | --- |
| `collector` | Polls the devices every `ESSL_COLLECTOR_INTERVAL_SECONDS` and writes punches to `essl.db`. Polls even when the internet is down. A punch write that fails (database busy or not set up) goes to the local spool and is replayed in order later. |
| `web` | Flask + gunicorn (`-w 1 --threads 4`). The screens and the read-only HRMS API. Owns the schema. Published on host loopback only (`127.0.0.1:ESSL_WEB_PORT`, default 8080). |
| `backup` | Every `ESSL_BACKUP_MINUTES` (15) takes a snapshot with SQLite's backup API, deletes sessions, login codes, report links and queued emails, runs `VACUUM` and `integrity_check`, gzips it and uploads it to R2. Also the admin CLI (`python -m essl_hub.backup <command>`, see [Backups](#backups)). The only service with R2 credentials. |
| `alerts` | Rebuilds each person's baseline weekly, runs the daytime check every 5 minutes and the next-morning check from the morning email hour, and sends every queued email (alerts, fix requests, decisions); the web only queues. Waits for schema v9 and never creates or migrates `essl.db`. Needs the mail variables and `ESSL_PUBLIC_URL`. See [Missed-punch alerts](#missed-punch-alerts-and-fix-requests). |
| `cloudflared` | *Optional* bundled Cloudflare Tunnel (compose profile `tunnel`). Skip it when the host already runs cloudflared. |

All four app services mount the same data folder at `/data`, so `essl.db`, its `-wal` and `-shm`, the collector spool and the status files (`collector-status.json`, `backup-status.json`) sit side by side. Web, collector and alerts **never create** `essl.db`: until an admin runs `init`, `restore` or `import-turso`, the web app shows a "Database not set up" page with the three commands and the collector spools and waits. All containers run as UID/GID 10001.

### Data folder (`ESSL_DATA_DIR`)

Set `ESSL_DATA_DIR` in `.env` to keep the database in a host folder you choose; leave it empty to use the Docker named volume `essl-data`.

- **Absolute path** only, e.g. `/srv/essl-data`. Compose mounts the folder, not the file, so `-wal` and `-shm` sit next to the database.
- **Native Linux filesystem only** (ext4, xfs, btrfs). NFS, SMB/CIFS and Docker Desktop file sharing break SQLite locking and can corrupt the file.
- **Owner `10001:10001`, mode `700`.** `./deploy/install.sh` sets both. By hand: `sudo mkdir -p /srv/essl-data && sudo chown 10001:10001 /srv/essl-data && sudo chmod 700 /srv/essl-data`.

Changing `ESSL_DATA_DIR` on a running install points the stack at a different (likely empty) folder: the web app then shows "Database not set up". Stop the stack and move `essl.db` (with any `-wal`/`-shm`) across first, or `restore` into the new folder.

## Backups

The `backup` service writes to one private R2 bucket ([R2 bucket](#2-r2-bucket-backups)):

- `recent/<timestamp>.db.gz`: every snapshot, kept for `ESSL_BACKUP_KEEP_HOURS` (48).
- `nightly/<date>.db.gz`: the first successful snapshot after `ESSL_EXPORT_HOUR` (02:00 IST) each day, kept for `ESSL_BACKUP_KEEP_DAYS` (30).
- `latest.json`: which database owns the bucket (`database_id`), the newest key, punch count and time.

The service prunes both prefixes itself; no R2 lifecycle rule is needed. A cycle whose snapshot is byte-identical to the last upload skips the upload. Snapshots never contain sessions or sign-in codes, so a restore signs everyone out. Each cycle is logged to `backup-status.json` and shows on the health page.

**Database ID guard.** `init` and `import-turso` give the database a random `database_id`; a snapshot carries it, so `restore` keeps it. If `latest.json` in R2 names a different database (a new install pointed at an old bucket, or a mistyped `ESSL_DATA_DIR`), uploads stop and the health page turns red with "different database". Then either `restore` (take the bucket's database) or `adopt` (make this database the owner). A database with no `database_id` (e.g. built by the Postgres migration script) is also blocked until `adopt`.

### Admin commands

Run on the host, from the repo folder:

```bash
docker compose run --rm backup python -m essl_hub.backup <command>
```

| Command | What it does |
| --- | --- |
| `init` | Creates a new empty database with a new `database_id`. New installs only (the installer runs it). |
| `restore [--key <key>]` | Downloads the latest snapshot (or the exact R2 key, e.g. `recent/20261014T101500.db.gz`), verifies it and moves it into place. Onto an empty data folder only; to swap a live database use `--replace --yes` ([below](#restoring-over-a-live-database)). |
| `fetch --out <folder> [--key <key>]` | Writes a scrubbed, inert copy of a snapshot into `<folder>` for looking at production data elsewhere ([below](#looking-at-production-data-on-a-dev-box)). Never touches the live data folder. |
| `list` | Lists snapshots in R2. |
| `now` | Runs one backup cycle now (e.g. before a large roster import). Exit 0 on success. |
| `status` | Shows the local `database_id`, the one in R2 `latest.json` and whether they match, plus the last and next cycle. |
| `adopt [--yes]` | Makes this database the bucket's owner (asks you to type `adopt`). |
| `import-turso` | One-time copy from Turso during the upgrade ([Upgrading from Turso](#upgrading-from-turso)). |

`init`, `restore` (without `--replace`) and `import-turso` refuse while `essl.db`, `essl.db-wal` or `essl.db-shm` exists: a leftover WAL would replay another database's pages onto the new file. Stop the stack and move all three aside first. Every command and cycle holds a lock on `/data/backup.lock`, so `now` cannot collide with the running loop.

### Restoring over a live database

`restore` refuses to touch an existing database. To roll production back to a snapshot (a bad import, wrong fixes, a corrupted file), use `--replace --yes` with the stack stopped:

```bash
docker compose stop web collector alerts
docker compose run --rm backup python -m essl_hub.backup list                 # pick a key
docker compose run --rm backup python -m essl_hub.backup restore --key recent/<ts>.db.gz --replace --yes
docker compose up -d web collector alerts
```

What it does, in order: downloads and verifies the snapshot (it must not be from a newer schema); proves nothing else has the database open (it refuses if web, collector or alerts, or anything else, still holds it, even idle); takes a snapshot of the **live** state to R2; moves `essl.db`, `-wal` and `-shm` aside to `essl.db.pre-restore-<time>` (mode 0600, never deleted or overwritten); installs the snapshot. If the install fails, the old files are put back. If the live file cannot be snapshotted (R2 down, or it belongs to a different database than the bucket), the command stops and changes nothing; `--skip-pre-snapshot` goes ahead anyway, with the old files still kept aside.

After it: everyone signs in again; **roles, archives, fix requests and corrections made after the snapshot are rolled back**, so re-check departed or demoted accounts and any approved fixes. The punches themselves come back from the devices (each returns its whole log on every poll). Keep the `pre-restore` files until you are sure: they hold everything written after the snapshot, including the audit trail of fixes. A snapshot from an older schema migrates when web or collector starts; `backup now` fails with "behind" until then. To roll back the other way, restore the snapshot the command just took of the live state.

### Looking at production data on a dev box

Run `fetch` on the production host, where the R2 keys already are, then copy the file to the dev machine; the dev machine never needs R2 credentials.

```bash
mkdir -p fetched
docker compose run --rm -u "$(id -u):$(id -g)" -v "$PWD/fetched:/out" backup \
  python -m essl_hub.backup fetch --out /out            # newest snapshot; add --key recent/<ts>.db.gz for another
scp fetched/essl-fetched-<timestamp>.db dev-box:        # then delete it from the host
```

The copy is made on a temporary file inside the output folder and renamed into place only after it passes checks. Device communication passwords, queued door-parameter requests, HRMS API keys, sessions, sign-in codes, report links and the mail outbox are removed (with `secure_delete` and a `VACUUM`, so old pages do not keep them). It gets a new `database_id` and a `fetched_copy` marker, so `backup` refuses to upload from it, `adopt` refuses it, and the collector and alerts services refuse to start on it. **Employee names, emails and punches stay in the copy**: treat it as production data (encrypted disk, delete it when done) and run only the web app on it.

To use it: point a dev web instance at the file (`ESSL_DB_PATH=/path/essl-fetched-<timestamp>.db`) with `ESSL_MAIL_PROVIDER=console`, so a sign-in code is printed in the server log instead of being emailed to a real person, and no Frappe settings. Sign in with an admin email from the copy. A fetched copy of an older schema migrates when the web app opens it.

**R2 is optional in compose, required by the installer.** With the `R2_*` values empty, the stack runs normally, nothing is copied off-site, and the health page shows Backups **Off**. `./deploy/install.sh` asks for all four values and will not continue without them; run without R2 only for dev or a scratch host.

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

### 2. R2 bucket (backups)

You need four values. There is **no URL to copy**: the app builds the endpoint `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` itself.

| `.env` key | Where to find it |
|---|---|
| `R2_ACCOUNT_ID` | R2 overview page, right sidebar "Account ID" (also the hex string in the dashboard URL) |
| `R2_BUCKET` | Bucket **name** you typed at creation, e.g. `essl-attendance` (not a bucket ID) |
| `R2_EXPORT_ACCESS_KEY_ID` | "Access Key ID" shown after creating the API token |
| `R2_EXPORT_SECRET_ACCESS_KEY` | "Secret Access Key" shown once after creating the token -- copy it then |

Steps:

1. **R2 → Create bucket**, e.g. `essl-attendance`. Keep it private (no public access, no custom domain).
2. No lifecycle rule is needed: the backup service deletes `recent/` and `nightly/` objects past `ESSL_BACKUP_KEEP_HOURS` / `ESSL_BACKUP_KEEP_DAYS` itself. Do not add bucket lock on these prefixes, or pruning fails.
3. **R2 → Manage R2 API Tokens → Create API token**: permission **Object Read & Write**, **Specify bucket** → only this bucket, TTL forever. Copy the Access Key ID and Secret Access Key. Ignore the "Token value" and the S3 endpoint shown there; the app needs neither.

R2 has no write-only permission, so this token can also read and delete objects in this one bucket. It lives only in the `backup` container. Snapshots include `devices.comm_password` (a restore needs it), so keep the bucket private.

### 3. Sign-in email

Codes go out through **Cloudflare Email Sending** by default, with **Resend** as a fallback.

- Cloudflare Email Sending needs the **Workers Paid** plan ($5/month) and is in beta. Onboard the sending domain under **Email → Email Sending** (it adds SPF/DKIM records on Cloudflare DNS). Create an API token with the Email Sending permission. Set `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_FROM_ADDRESS`.
- Resend fallback: verify the domain in Resend, then set `RESEND_API_KEY` and `RESEND_FROM_ADDRESS`.

With both configured, Cloudflare wins unless `ESSL_MAIL_PROVIDER=resend`. Switching is an env change plus `docker compose up -d web`. A send failure is logged, never shown to the person signing in, so the sign-in page cannot reveal who is in the Directory.

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

The installer stops on a non-x86_64 host, checks every required value, builds, prepares the data folder, creates the database on a fresh install (`backup init`), tests the devices and starts.

Check it is healthy:

```bash
docker compose ps                       # collector, web, backup (and cloudflared) running
docker compose logs -f collector        # one line per device per poll
docker compose run --rm --no-deps collector python diagnose.py   # device test again, any time
```

Open `https://attendance.yourcompany.com`.

**Updating:** `./deploy/install.sh` (it pulls, backs up and restarts; see [Updating](#updating)).

## First administrator

An empty database has no one to email a code to. Pick one way:

- **Setup page (default).** The installer generates `ESSL_SETUP_TOKEN` and prints it. Open `/setup`, enter the token, your name and work email (and badge number if you scan in). The page returns 404 once any admin exists. Unset the token afterwards.
- **Env only.** Set `ESSL_BOOTSTRAP_ADMIN_EMAIL` to an address already in the Directory. While no admin exists, the web service promotes that row on boot.

Then sign in, open **People → Import roster** and upload the roster. Everyone else is managed from **People**.

## Fixing ID issues

Hours count only when the ID on the door device matches a person's main or extra device ID in the Directory. **People → ID issues** (HR and admin; the People menu shows a red count) lists every mismatch in plain words and fixes it in place:

| Issue | Means | One-click fix |
| --- | --- | --- |
| **Not in Directory** | Someone punches with an ID nobody in the Directory has; their hours are lost | **Link to *name*** (suggested when names match), **Add as new person** (prefilled), or pick anyone from the dropdown. Linking adds the ID as an extra ID when the person's main ID already has punches, otherwise it replaces the main ID; the preview says which |
| **No device ID** | A person's ID is still their email, so no punch can match | **Use NP0xxx** (suggested) or type it in **Set ID** |
| **Never punched** | Nobody has scanned with this person's ID | Same as above: correct the ID |
| **Name differs** | Device and Directory name the same ID differently | **Same person** (hides it) or **Fix ID** |
| **Shared card** | Temporary cards in `ESSL_C3_UNLINKED_PINS` | Nothing; never counted |

Nothing is stored about an issue: the list is rebuilt from the Directory and the punches on every load. Archived people's IDs stay mapped, so their old punches never show as unknown. Fix the Directory (here, on the person form, or by re-importing) and the row disappears; that person's past punches count immediately, no re-collection needed. Linking an ID shows how many past punches it brought in.

**Device IDs.** Each person has one main device ID and any number of extra IDs (People → *name* → Other device IDs, or edit the main ID inline on the Directory row). Punches under any of them count. An ID belongs to one person only. Typed IDs are trimmed and upper-cased, and an all-zero ID (`0`, `NP0000`) is refused because devices use 0 for "no user".

### Archiving punches of old or test cards

Temporary, visitor and test cards leave **Not in Directory** issues whose punches count for nobody. An admin can hide them instead of linking them:

1. **Archive punches** on the issue card (admins only; HR does not see it).
2. Optionally pick a **from** and **to** date and a reason.
3. The preview shows exactly how many punches will be hidden and the resolved range, then **Confirm**.

**Rules:**
- No to date means "up to now".
- An explicit to date includes that whole work day.
- Swipes made after you archive always stay visible, so a card handed to a new person is never hidden.
- Only **Not in Directory** and **Shared card** issues can be archived. An ID that belongs to someone is refused; use a fix request instead.

Archiving never edits or deletes a punch. It stores a rule, and every read hides the punches it covers: hours, Team, People, exports, `/api/v1/hours`, `/api/v1/checkins`, Frappe pending sync, alerts and baselines, the history pager, ID issues and the unknown-ID notices. The collector still sees every row, so the devices' resent history is never inserted again. The device card's *Punches stored* still counts them. Archiving is not retroactive for consumers: punches already sent to Frappe stay there, and an API client whose `since` cursor is past an archived or restored range does not see those punches change, so re-pull with an earlier `since` after a restore. A device ID with an active archive cannot be given to a person by CSV import or the inline Device ID edit; use the person's page, which asks first, or restore the archive.

**People → ID issues → Archived punches** lists every archive: who, when, range, reason and how many punches it hides now. HR can see it; admins can **Restore**, which brings the punches back everywhere at once.

**Frappe and checkins after restore:**
- Punches already sent to Frappe stay there.
- Restored punches with no sync record are pushed on the next sync.
- Clients pulling `/api/v1/checkins` that already paged past a restored time will not see it again.

If you later link an archived ID to a person, the link and person forms warn that its archived punches stay hidden until restored.

### Archiving people

For someone who has left, open their person page and use **Archive person** at the bottom. They leave Employees, Team and the Directory, are signed out and cannot sign in. Punches, past hours and device IDs are kept; exports and the hours API still include them for ranges where they punched. Archiving is refused for the last admin and for anyone who still has reports (reassign them first). **People → Archive** lists archived people with **Restore**. A roster import rejects rows that match an archived person; restore them first.

### Breaks and duplicate swipes

- Every In and Out swipe counts (the door is magnetic; punching both ways is compulsory). A swipe with no direction first fills a missing alternation: between Out and Out it is In, between In and In it is Out, shown with an *inferred* chip.
- Otherwise a direction-less swipe, or a same-direction re-swipe, that comes sooner than its device's **quick-swipe window** after the person's previous counted swipe is a duplicate: kept in **All punches** as *not counted*, with the reason beside it ("same as In 09:00:00, 20s earlier"), never paired.
- The quick-swipe window is set per device on **Devices → Swipe policy** (existing devices start at 60). Each swipe is judged by its own device's window. Hours are computed on read, so a change re-scores every past day for that device, reports included. The C3 records its directions, so it takes 0–300 s (0 counts every swipe; suggested 10 s). The X990 records none: hours alternate In and Out from its swipes, so one re-swipe the window lets through flips every later swipe that day. It takes 30–300 s (suggested 60 s), and a stored value below 30 s scores as 30 s until saved. Wrong In/Out on X990 days after a window under 30 s: set it to 60 s; nothing else needs repair (see *Wrong In/Out on the X990* below).
- **Wrong In/Out on the X990.** Punches are stored exactly as the device sent them and directions are worked out on read, so there is nothing to repair in the database: set the Uppal X990 window to 60 s and every past day re-scores. Two things are not recomputed: approved fixes (HR **Fixes**) keep the direction HR chose, so re-check any approved while the window was small, and missed-punch alert emails already sent stay sent. The Devices page audit shows when the window changed.
- A gap between paired stretches is a **break** only when it is a clean out then in. A gap holding an unpaired swipe is **unclear**: shown dotted on the day bar and totalled apart, never counted as break.
- C3 event 20 swipes (*interval too short*) count as attendance; see [C3-400](#c3-400-gachibowli).

**Release note.** Hours are computed on read, so after this release hours on past days change for days with duplicate or unpaired swipes (exports, Week, the hours API and statuses included), and again once the next C3 history pull backfills event 20 swipes. Punches themselves are untouched; clean days are unchanged.

## Fix the Uppal address

The Uppal X990 is at `192.168.1.201`. Older installs stored `192.168.1.200`. `ESSL_X990_IP` only seeds a device row on first insert; after that the stored row wins, so editing `.env` does not fix a live row.

1. Sign in as admin and open **Devices**.
2. Uppal → **Edit connection** → IP `192.168.1.201`, port `4370` → **Save connection**.
3. The card shows **Change pending** until the collector's next poll, then **Change applied**. No restart.

Devices is admin-only; HR handles ID problems from **People → ID issues**.

## Add, retire or re-enable a device

The devices table is the source of truth; `.env` IPs only seed the first two rows. On **Devices** (admin):

- **Add device**: site, family (X990 attendance or C3-400 access controller), IP, port and an optional comm password (numeric for the X990). The id is generated from site and family (`kondapur-c3-400`, then `-2`, `-3` on a clash), never changes and is never reused. The card reads **Pending** until the collector picks it up on its next poll, within about 2 minutes.
- **Retire device** (asks to confirm): sets `retired_at`. The collector stops polling it, it never trips the stale-collector warning, and its punches stay on every day view. Retired cards collapse under **Retired**.
- **Re-enable**: clears `retired_at`; polling resumes on the same id.
- **Swipe policy**: the quick-swipe window (see [Breaks and duplicate swipes](#breaks-and-duplicate-swipes)) and, on a C3, **Wait for door close**.
- **Door settings** (C3 only): read, change, restore or dismiss the panel's door parameters; see [C3-400](#c3-400-gachibowli).

Two active devices can't share an IP:port. Adding an address that belongs to a retired device offers **Re-enable** instead of a new id, so history stays under one id. There is no delete.

## Sign-in branding

On **Settings → Sign-in branding** (admin) upload a logo and pick the sign-in background: **Default**, **Brand colour** (hex) or **Uploaded image**. The sign-in, code and setup pages show them in light and dark; the card itself stays solid so its text keeps AA contrast, and dark theme dims a background image with a scrim.

- Formats: PNG, JPEG or WebP, checked by file signature (the extension is ignored). SVG is refused.
- Caps: logo 512 KB, background 1.5 MB (plus the global `ESSL_MAX_UPLOAD_MB`).
- Images live in the database (`branding_assets`, schema v5), so every backup and restore carries them. They are served publicly at `/brand/logo` and `/brand/background` with an ETag and a 5-minute cache; the page adds `?v=<hash>` so a change shows at once.
- **Reset** puts either asset back to the default. With nothing uploaded, sign-in looks as before. Email keeps `static/email-logo.png`.

## Cutover from the Postgres stack

The old stack (Postgres + collector) keeps running until step 5. The C3-400 realtime queue holds only about 30 events and does not replay, so the gap between old and new collectors must stay short (KTD17).

1. **Rehearse** the whole sequence on a scratch host and time it. Run the [restore drill](#restore-drill) once before go-live.
2. **Schedule an off-hours window** (no one at the doors), longer than the rehearsal.
3. **Full migrate to a local file while the old collector still runs.** On any Linux x86_64 machine that reaches the old Postgres (the `libsql` package ships Linux x86_64 wheels only; elsewhere use `docker run --rm -it --platform linux/amd64 -v "$PWD":/app -w /app python:3.12 bash`):

   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt "psycopg[binary]"
   .venv/bin/python scripts/migrate_postgres.py --source postgresql://user:pass@oldhost/essl --target essl-migrated.db
   ```

   Note the time you started. The script prints per-table counts and a device/settings diff and exits non-zero on any mismatch. Sessions and login codes are not copied, so everyone signs in once more.
4. **Start the new stack**: fill `.env`, run `./deploy/install.sh --check`, copy `essl-migrated.db` into the data folder as `essl.db` (owner 10001, mode 600, no `-wal`/`-shm` beside it), run `./deploy/install.sh`, then `docker compose run --rm backup python -m essl_hub.backup adopt` so backups accept the file.
5. **Stop the old collector** as soon as `docker compose logs collector` shows the new one polling both devices. Do not run both against the C3 for long: each read drains its queue.
6. **Backfill the gap** with delta mode into the live file, using the start time from step 3. Web and collector must be stopped:

   ```bash
   docker compose stop collector web
   docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts" backup sh -c \
     'pip install --user "psycopg[binary]" && python scripts/migrate_postgres.py \
        --source postgresql://user:pass@oldhost/essl --target /data/essl.db \
        --punches-since "2026-10-09 18:00:00"'
   docker compose up -d
   ```

   Punches are deduplicated by fingerprint, so overlap is harmless.
7. **Keep the old Postgres stack stopped but intact for 14 days.** That is the rollback path: stop the new collector, start the old stack. After 14 clean days, remove it.

Then fix the Uppal row if it was migrated as `.42`.

## Sync health and offline behaviour

### Sync-health indicator

Admins see an indicator top right on every page. It links to **System health** (`/health`, admin only, 404 for everyone else) and shows the worst of its signals, each read from the data folder:

| Signal | Green | Amber | Red |
| --- | --- | --- | --- |
| Collector | polled within 10 min, spool empty | punches spooled | no poll for 10 min, a dead-lettered punch, or the oldest spooled punch older than 60 min |
| Backups | last backup within 2 intervals (30 min) | within 6 intervals (90 min) | older, none yet, 3 failed cycles in a row, or blocked by a different database |
| Devices | every active device answered its last poll | - | an active device failed its last poll (named, with the date it last connected); the Devices nav entry shows the same count |

With no R2 configured, Backups shows a neutral **Off** chip that never colours the indicator. The page also lists the last 14 backup cycles and the recovery commands.

### When the internet is down

- **Everything local keeps working**: every page, every edit, the `/api/v1` pulls and the collector. Data is never more than a write away.
- **Sign-in codes can't be emailed**, so new sign-ins wait; people already signed in stay signed in.
- **Backups pause.** Each failed upload is logged and retried next cycle; the Backups signal turns amber, then red. Nothing is lost locally, and the next successful cycle uploads a current snapshot.
- **Frappe push** retries next cycle.

### When the database is busy or missing

- **Busy**: the web app and the collector share one file. A write that waits more than 5 s for the other process gets a "Busy, try again" page (HTTP 503) in the web app; in the collector the punches go to the spool and replay oldest-first. Duplicates are ignored. A punch that keeps failing for another reason moves to the spool's `dead` table (indicator red); it is kept, never dropped.
- **Missing** (`essl.db` not there, e.g. a wrong `ESSL_DATA_DIR`): the web app shows "Database not set up" with the `init`, `restore` and `import-turso` commands; the collector spools and logs the same commands; the backup service waits. Nothing creates an empty database by accident.

## Recovery

The local file is the source of truth; R2 holds the last 48 h of 15-minute snapshots and 30 daily ones. With uploads succeeding, a total loss of the host costs at most the edits since the last snapshot. Punches heal themselves: the X990 returns its whole log on every poll and the C3 history is re-read at start and every 6 h.

### Restore from R2

```bash
docker compose stop web collector backup
# move the current files aside (keep them until you are sure)
docker compose run --rm --no-deps --entrypoint sh backup -c \
  'mkdir -p /data/old && mv /data/essl.db /data/essl.db-wal /data/essl.db-shm /data/old/ 2>/dev/null; true'
docker compose run --rm backup python -m essl_hub.backup list            # pick a key, or take the latest
docker compose run --rm backup python -m essl_hub.backup restore         # or: restore --key recent/<ts>.db.gz
docker compose up -d
```

`restore` verifies the snapshot (integrity check, schema version) before moving it into place and keeps its `database_id`, so backups resume without `adopt`. **Keep the spool** (`collector-spool.db`): it may hold punches not yet written. Everyone signs in again (sessions are not in snapshots); API keys and revocations come back as of the snapshot.

### Backups blocked: "different database"

The bucket's `latest.json` names another database. Decide which is right:

- The bucket is right (this host has a wrong or new file): stop the stack, move the files aside, `restore`.
- This database is right (e.g. you rebuilt the bucket, or loaded a file from the Postgres migration): `docker compose run --rm backup python -m essl_hub.backup adopt`.

### Restore drill

Do it once after the upgrade, then quarterly, on a **scratch host** (never on production):

1. On production: `docker compose run --rm backup python -m essl_hub.backup now`, then `list`; confirm the new `recent/` key and an OK row on the health page.
2. On the scratch host: copy `.env`, then edit it so the copy can never touch production: leave `TUNNEL_TOKEN` empty, **blank `ESSL_X990_IP`, `ESSL_C3_IP` and every `FRAPPE_*`** (no door reads, no pushes to Frappe), set `ESSL_MAIL_PROVIDER=console`, and point `R2_*` at the same bucket with **read-only R2 credentials** (or remove the write credentials). Then `docker compose build` and `docker compose run --rm backup python -m essl_hub.backup restore --drill`. `--drill` gives the restored file a new random `database_id`, so even with write credentials it can never pass the bucket guard and upload.
3. Start **only the web app**: `docker compose up -d web`. Never start `backup` (or `collector`) on the scratch host. Sign in and check yesterday's hours match production.
4. Record the time it took, then destroy the scratch host.

## R2 plan limits

R2's free tier covers 10 GB stored, 1 million Class A (write/list) and 10 million Class B (read) operations a month, with no egress fees. These change; check https://developers.cloudflare.com/r2/pricing/ .

What this app uses, measured on a 44.5 MB database (11 MB gzipped):

- **Storage**: about 48 h × 4 snapshots/h × 11 MB ≈ 2.1 GB in `recent/`, plus 30 × 11 MB ≈ 330 MB in `nightly/`. Grows with the database; lower `ESSL_BACKUP_KEEP_HOURS` if it ever nears the cap.
- **Operations**: about 96 uploads plus a few lists and deletes a day, roughly 3,000 Class A a month.
- **Uploads skip** when a snapshot is identical to the last one.

Set a billing alert on the Cloudflare account if you leave the free tier.

## Upgrading from Turso

Earlier releases kept the database in Turso with an embedded replica per service and a nightly export. This release keeps it locally and backs it up to R2 every 15 minutes. `./deploy/install.sh` detects an old install (no `essl.db`, but `*-replica.db` files or `TURSO_DATABASE_URL` set), builds the new images, starts nothing and prints these steps.

Until you upgrade, `docker compose stop nightly-export` stops the nightly full Turso download (the biggest use of the Turso sync quota); R2 snapshots pause until the upgrade.

1. Stop the stack: `docker compose stop`.
2. Optional: set `ESSL_DATA_DIR` in `.env` and prepare the folder ([Data folder](#data-folder-essl_data_dir)). The installer does this for you.
3. Pull the new release and build it: `git pull && docker compose build`.
4. Copy the database out of Turso once (keep `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in `.env` for this step):

   ```bash
   docker compose run --rm backup python -m essl_hub.backup import-turso
   ```

   This is one full download (about 45 MB). It checks the file, compares the punch count with Turso, assigns a new `database_id` and moves it into place as `essl.db`.
5. Start the new stack, which also removes the old `nightly-export` container:

   ```bash
   docker compose up -d --build --remove-orphans
   ```

6. Check the health page. Backups turn green after the first cycle, and `docker compose run --rm backup python -m essl_hub.backup list` shows the first `recent/` object. Everyone signs in again.
7. After **a week** of clean backups:
   - Delete the Turso database (`turso db destroy essl`). It still holds old sessions and sign-in codes.
   - Remove the `TURSO_*` values from `.env`.
   - Delete the old replica files: `docker compose run --rm --no-deps --entrypoint sh backup -c 'rm -f /data/*-replica.db*'` (if you moved to `ESSL_DATA_DIR`, run it against the old volume instead).

Rolling back to the previous release after the cutover needs Turso, so keep the Turso database for that week. Existing `nightly/` objects from the old export stay readable by `restore --key` and are pruned after `ESSL_BACKUP_KEEP_DAYS`; restoring one needs `adopt` afterwards (they carry no `database_id`).

## Roles

| Capability | employee | manager | hr | admin |
| --- | --- | --- | --- | --- |
| Own Today and Week | yes | yes | yes | yes |
| See others' hours | - | own reports, direct and indirect | all employees | all employees |
| Export CSV / Excel | - | own reports | all employees | all employees |
| Roster template and import | - | - | yes | yes |
| Add, edit, archive or restore a person, set manager | - | - | yes, except an admin's email or device user ID | yes |
| Set role employee / manager / hr | - | - | yes, not on admins | yes |
| Grant or revoke admin | - | - | - | yes, never the last admin |
| Devices | - | - | - | yes |
| ID issues (People → ID issues) | - | - | yes | yes |
| Edit a device connection | - | - | - | yes |
| Sync health (`/health`) | - | - | - | yes |
| Daily target, attendance alerts, HRMS API keys (**Settings**) | - | - | - | yes |
| Report a problem on own day (last 7 days) | yes | yes | yes | yes |
| Approve or reject fix requests | - | - | yes, never their own | yes, never their own |
| Fix punches directly (day page → **Fix punches**) | - | - | - | yes, never their own |

| Role | Lands on | Sidebar |
| --- | --- | --- |
| employee | Today | Today, Week |
| manager | Team | Today, Week, Team |
| hr | Employees | Today, Week, Employees, People (Directory, ID issues, Archive) |
| admin | Employees | Today, Week, Employees, People (Directory, ID issues, Archive), Devices, Settings (plus the sync-health indicator) |

**Fix requests.** HR or an admin approves every fix request, on its own review page. Each approval records who decided it, their role at that moment and the note (the correction record cannot be edited afterwards). When an employee files one, their manager gets one FYI email listing the lines (no button, HR decides) and every HR user gets the same with a **Review request** link. If the requester is the only HR user, admins other than the requester are emailed instead; admins can open and decide any request from the review list either way. Nobody approves their own request.

**Direct fixes (admin).** On another person's day, **Fix punches** lets an admin count a punch as In or Out, ignore it, or add a missed one, with a required reason and a before/after hours preview. Nothing in the device records changes: each fix is a correction stored with the admin's name and role at that moment, the time, the reason and the path (`direct`, or `request` when HR or an admin approved an employee's request). The correction record cannot be edited or deleted. An admin cannot fix their own punches this way; they file a report like anyone else and HR or another admin decides it. Frappe HR still receives the device's own direction, so a corrected day can differ from Frappe.

Asking for someone you may not see returns 404, the same as an id that does not exist. `/admin` redirects to **Devices**; `/devices/ids` redirects to **People → ID issues**.

## Missed-punch alerts and fix requests

A missed swipe looks like a long break: someone forgets to swipe, or the Gachibowli reader misses a tap and records the next one the wrong way round. The `alerts` service notices and emails the person; they say what happened; HR approves the fix. No AI is involved: the checks are plain rules over each person's own swipe times.

**Each person's normal day.** Every Sunday at 02:00 IST (and on the service's first start) the service looks at each active person's last 8 weeks and works out how long their longest break usually is and when they usually leave. Their personal limit is well above their usual longest break, never under 90 minutes and never over 4 hours. Days with unclear gaps, unpaired swipes or a fix request are left out, so a bad day never becomes "normal". Nobody gets any email until they have 10 days with swipes in those 8 weeks, and no daytime email until 10 of them are clean.

**The two emails.**

- **Daytime ("out too long"):** checked every 5 minutes inside the alert hours (default 09:00-20:00). Sent when someone who came in today has been out 15 minutes past their limit, and it is still before their usual leaving time.
- **Next morning:** after the morning email hour (default 09:00), about yesterday, when the day ends on an In with no Out, has an Out with no In, two Ins in a row, or an unclear gap. A day with a C3 direction still being guessed waits for the next history read.

At most one of each per person per work day, and never to someone who did not swipe that day. Both skip while the collector's data is stale (no poll for 10 minutes, punches still spooled, or a device that can't be reached). If one check would email more than 5 people, or 20% of those in, it looks like an outage, not missed swipes: those people are recorded as *held* and nobody is emailed. Emails show only times and the length of the out-stretch, never anyone's limit, and assume the reader missed a swipe.

**Rollout: shadow, then live.** **Settings → Attendance alerts** has three modes. A fresh install starts in **Shadow**: every check runs and is recorded, nobody is emailed. Leave it for about two weeks and watch the *would send* counts per site on the same panel, along with how many people have enough history. Then switch to **Live**. Settings refuses live until `ESSL_PUBLIC_URL` is set and at least one active HR user exists. **Off** stops the checks. Fix-request and decision emails go out in every mode, because the web only queues email and the alerts service sends the queue on every tick. Switching shadow to live mid-day never resends a recorded alert.

**Reporting a problem.** Each email has a **Report a missed swipe** button with a personal link that works without signing in, for that one day, until 7 days after it. Opening the link changes nothing (mail scanners open links too); only submitting the form does. A signed-in person can also use **Report a problem** on their own Day view for any of the last 7 days. The form lists the day's swipes, each with "This was the other direction" and "Ignore this swipe", plus rows to add a missed swipe (time and In/Out) and an optional note. **Everything's correct** and **I left for the day** close the day straight away with no HR step and stop further reminders for it.

**Review and approval.** A report that changes anything goes to HR (and an FYI to the manager, see [Roles](#roles)). On the review page HR sees the day's swipes and each requested line, can adjust a time or direction, and approves or rejects each line; rejecting needs a note. A confirm page shows the hours before and after. The employee is emailed the decision. The page warns when the day's swipes changed after the report, or when a swipe a fix points at no longer exists.

**What an approved fix does.** It is stored beside the punches as a correction and changes the computed hours everywhere: Today, Week, Team, exports and `/api/v1/hours`. The device punch itself is never edited or deleted, and the day shows chips such as *added*, *direction fixed* or *ignored*. Under each punch the day page says why it counts as In or Out: what the device said, a live-log guess the device history has not confirmed yet, the hub's alternation, a quick re-swipe, or who made a fix and why. HR and admins also see the raw device facts (device, door, reader, in/out byte, source). Frappe HR and `/api/v1/checkins` keep receiving only the raw device punches, so Frappe's hours can differ from the dashboard for a fixed day.

**Email delivery.** All these emails go through the same provider as sign-in codes. Resend sends the HTML version with the NITCO logo. The Cloudflare provider sends **text only**, so there is no logo; set `ESSL_MAIL_PROVIDER=resend` if the branded version matters. `ESSL_HR_REPLY_TO` makes replies go to an HR mailbox.

**Privacy notice.** Employees can read `<ESSL_PUBLIC_URL>/privacy` (linked from every alert email and the report page) without signing in. In short: only swipe times and the device are used; the typical-day pattern is recomputed weekly from recent swipes; reminders assume the device missed a swipe and are never used for discipline; the employee and HR see the details, the manager sees hours as before plus an FYI when a fix is filed, never the pattern; alert records are deleted after 90 days, sent emails after 30, report links when they expire, and only the last 4 weekly baselines are kept. Report links and queued emails are never in backups.

## Attendance export

**Team / Employees → Export hours** downloads up to 93 days for the people you can see (all of them, or only those ticked). Archived people are included for ranges where they punched. Every sheet starts with the range and the work-day window (e.g. 06:00 to 06:00 next day, IST), and the whole export comes from one punch query.

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

The C3 sends big tables in chunks, which the bundled library cannot read, so the collector downloads them itself (`read_c3_table` in `essl_hub/collectors.py`, checked against the panel's CRC). With `ESSL_C3_MODE=auto` it pulls the full history (about 30,000 events, 80 seconds on the tested `AC Ver 18.1.2`) at start and every `ESSL_C3_HISTORY_HOURS`, and reads the live queue on the polls between. Only access-granted swipes count, plus event 20 ("Too Short Punch Interval"): the door refused to reopen but the person was there, so it is stored as attendance noted *interval too short*. Other denied swipes, the exit button and door alarms are skipped. Set the door's **Punch Interval** to 1 second or less so real swipes are not denied (now possible from the Devices page, below). The panel allows one session at a time, so pause ZKAccess or other tools while the collector runs.

**Directions.** The history table carries the real in/out byte (0 In, 1 Out). The live queue's library decodes byte 1 as "unknown" (15); on the tested panel only bytes 0 and 1 occur, so a live 15 on a granted or event-20 swipe is stored as Out. The next history read overwrites any live-read direction it disagrees with.

**Door settings (Devices page).** Each C3 card shows the panel's door parameters and when they were read. **Read from device** asks the collector to read them on its next poll. An admin can change a closed whitelist: per door *Punch interval* (`DoorNIntertime`, 0–10 s), *Unlock time* (`DoorNDrivertime`), *Door-open alarm after* (`DoorNDetectortime`), *Lock when door closes* (`DoorNCloseAndLock`), *Door sensor* (`DoorNSensorType`), plus panel-wide *Anti-passback* (0–6) and *Interlock* (0–5). Changes go through a confirm page (old → new), are applied by the collector one key at a time and count as applied only when read back. A request the collector has not reached within 15 minutes expires unsent. The values from before the last write are kept and **Restore previous values** puts them back through the same confirm page. Every change is recorded with who made it. The dashboard never writes `ComPwd`, `IPAddress`, `NetMask`, `GATEIPAddress`, `RS232BaudRate`, `DeviceID` or `DoorNVerifyType`.

**Wait for door close.** Interlock (event 25) and anti-passback (event 24) are the only panel settings that deny a swipe based on door state or swipe history. With **Wait for door close** checked (default) those denials are not attendance. Unchecked, denials that carry a PIN or card are stored from that moment on, flagged *door denied*, and not sent to Frappe; past denials are not added. Setting InterLock and AntiPassback to 0 stops the denials at the source. As read on 2026-10-26 the Gachibowli panel has no door sensor configured and both set to 0, so the panel never waits for the door.

**Bench check (pending).** Before relying on door writes, on the panel: write `Door1Intertime` 0 and read it back; swipe one card twice within 1 s and confirm both are granted; reboot and read again; write and read back one `AntiPassback` value. Record the results here, including whether a written value needs a restart.

**IDs.** The C3 stores bare PINs (`9`); the X990 and the Directory use `NP0009`. `ESSL_C3_ID_FORMAT` (default `NP{pin:04d}`) maps one to the other. Shared cards listed in `ESSL_C3_UNLINKED_PINS` (default `1,2`, the Temporary cards) are stored as `C3-1`, `C3-2` and never count for anyone. Punches stored before this mapping are fixed with `python scripts/remap_c3_ids.py --apply`. If your X990 IDs are not zero-padded (`NP9`, or bare `9`), set `ESSL_C3_ID_FORMAT` to match (`NP{pin}`, `{pin}`) so both devices produce the same ID. Where the two spellings already differ for someone, add the other one as an extra device ID on their person page (or link it from ID issues) instead.

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

Two kinds of punch are held back:
- C3 *door denied* rows are never sent; payroll cannot see the flag.
- A C3 live-queue exit is read as in/out 15, so its direction is a guess, and Frappe keeps the log type it is first sent. These rows wait until the next history read confirms the real byte, at most 24 h.

After a run with nothing left pending, each `FRAPPE_SHIFT_TYPES` entry gets `last_sync_of_checkin` = oldest successful device poll (only moves forward), stopping just before any punch still held. Auto Attendance therefore does not mark absences for punches not yet collected or confirmed. Last run status is in `settings.frappe_last_sync` (JSON).

### Pull alternative
`GET /api/v1/checkins?since=<local ISO datetime>&limit=<1..1000, default 500>` with `Authorization: Bearer <key>` returns records shaped as the method's arguments (`employee_field_value`, `employee_fieldname`, `timestamp`/`time`, `device_id`, `log_type`). `since` is exclusive; pass back `next_since`. Pages never split one second. A Frappe-side scheduled job can feed each record to `add_log_based_on_employee_field`.

## Configuration reference

All settings live in `.env`; `.env.example` documents each one. The daily target, work-day start, attendance alert mode and hours, and API keys are set on **Settings**, not in env.

| Variable | Default | Used by |
| --- | --- | --- |
| `ESSL_DATA_DIR` | empty = Docker volume `essl-data` | all app services: absolute host path mounted at `/data`; native Linux filesystem, owner 10001, mode 700 |
| `ESSL_DB_PATH` | `/data/essl.db` (set in compose) | web, collector, backup |
| `ESSL_SPOOL_PATH` | `/data/collector-spool.db` | collector |
| `ESSL_COLLECTOR_STATUS_PATH`, `ESSL_BACKUP_STATUS_PATH` | next to `ESSL_DB_PATH` | collector, backup, web (reads) |
| `R2_ACCOUNT_ID`, `R2_BUCKET` | installer: required; compose: empty = backups Off | backup |
| `R2_EXPORT_ACCESS_KEY_ID`, `R2_EXPORT_SECRET_ACCESS_KEY` | as above, token scoped to the one bucket | backup |
| `ESSL_BACKUP_MINUTES` | `15` | backup: minutes between snapshots; also the health thresholds |
| `ESSL_BACKUP_KEEP_HOURS` | `48` | backup: hours of `recent/` snapshots kept |
| `ESSL_BACKUP_KEEP_DAYS` | `30` | backup: days of `nightly/` copies kept |
| `ESSL_EXPORT_HOUR` | `2` (IST) | backup: the day's `nightly/` copy is the first snapshot after this hour |
| `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` | empty | backup, only for the one-time `import-turso` ([Upgrading from Turso](#upgrading-from-turso)) |
| `ESSL_WEB_PORT` / `ESSL_WEB_BIND` | `8080` / `127.0.0.1` | web host port; bind `0.0.0.0` for LAN access |
| `TUNNEL_TOKEN`, `COMPOSE_PROFILES` | optional; token + `tunnel` turns on the bundled cloudflared | cloudflared |
| `ESSL_COLLECTOR_INTERVAL_SECONDS` | `60` | collector, Devices page |
| `ESSL_HEARTBEAT_WRITE_SECONDS` | `900` | collector, Devices staleness warning |
| `ESSL_X990_IP` / `_PORT` / `_PASSWORD` | `192.168.1.201` / `4370` / `0` | collector (seeds the row once) |
| `ESSL_C3_IP` / `_PORT` / `_PASSWORD` | `192.168.1.202` / `4370` / blank | collector (seeds the row once) |
| `ESSL_C3_MODE`, `ESSL_C3_HISTORY_HOURS`, `ESSL_C3_RTLOG_BATCH` | `auto`, `6`, `250` | collector |
| `ESSL_C3_ID_FORMAT`, `ESSL_C3_UNLINKED_PINS` | `NP{pin:04d}`, `1,2` | collector: C3 PIN to device ID |
| `ESSL_MAIL_PROVIDER` | auto | web, alerts: `cloudflare`, `resend`, or `console` (local testing only: prints the email to the server log) |
| `CLOUDFLARE_API_TOKEN` / `_ACCOUNT_ID` / `_FROM_ADDRESS` | | web (sign-in codes), alerts (every other email); text only, no logo |
| `RESEND_API_KEY` / `RESEND_FROM_ADDRESS` | | web (sign-in codes), alerts (every other email); HTML with the NITCO logo |
| `ESSL_PUBLIC_URL` | unset | web, alerts: the dashboard's public `https://` URL for the links in alert and fix-request emails. Required to switch alerts to live |
| `ESSL_HR_REPLY_TO` | unset | alerts: Reply-To on alert and fix-request emails, e.g. an HR mailbox |
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | recommended; both set = bot check on sign-in and setup | web |
| `ESSL_FORCE_HTTPS_COOKIES` | `true` | web (`false` only for local http) |
| `ESSL_MAX_UPLOAD_MB` | `5` | web |
| `ESSL_SETUP_TOKEN`, `ESSL_BOOTSTRAP_ADMIN_EMAIL` | unset | web, first admin only |
| `ESSL_BEHIND_TUNNEL` | `1` | web (`0` behind a non-Cloudflare proxy) |

## Development and tests

Dev and tests use a local file at `ESSL_DB_PATH`; no cloud account needed. Create it once with `python -m essl_hub.backup init`.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ESSL_DB_PATH=./data/essl.db ESSL_FORCE_HTTPS_COOKIES=false
mkdir -p data && .venv/bin/python -m essl_hub.backup init   # once
.venv/bin/python -m essl_hub.collector_main             # collector
.venv/bin/flask --app 'essl_hub.web:create_app()' run   # dashboard on :5000
```

```bash
.venv/bin/python -m pytest -q
```

The suite needs no network or database server. `tests/test_libsql_contract.py` pins the libSQL behaviour the app depends on. `psycopg` is only for the one-time migration script.

---

## About

eSSL Attendance Hub is built and maintained by the team at **NITCO Inc.** It started as an internal tool for our Hyderabad offices (Uppal and Gachibowli) and is shared in the hope that it helps other teams running eSSL / ZKTeco door devices.

- **Issues and ideas:** open a GitHub issue with steps to reproduce, the device model and firmware, and the relevant `docker compose logs` lines (remove tokens and personal data first).
- **Contributions:** pull requests are welcome. Run `.venv/bin/python -m pytest -q` before opening one; the suite needs no network or device.
- **Security:** please report vulnerabilities privately to the maintainers rather than in a public issue.

Released under the [MIT License](LICENSE).

eSSL and ZKTeco are trademarks of their respective owners; this project is not affiliated with or endorsed by them. Third-party components are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

<div align="center"><sub>© 2026 NITCO Inc. · Made in Hyderabad</sub></div>
