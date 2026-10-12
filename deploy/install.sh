#!/usr/bin/env bash
# One command to install AND update the single-host stack (docker-compose.yml).
#
#   ./deploy/install.sh            # pull latest code, set up .env, back up, build, start
#   ./deploy/install.sh --check    # same checks and build, but do not start anything
#   ./deploy/install.sh --yes      # never prompt; fail on missing values (CI, scripted hosts)
#   ./deploy/install.sh --skip-devices   # skip the device connection test
#   ./deploy/install.sh --no-pull        # use the code as it is (no git pull)
#   ./deploy/install.sh --skip-backup    # no pre-update snapshot to R2
#
# Safe to re-run: it only fills empty values and restarts changed containers.
set -euo pipefail

cd "$(dirname "$0")/.."

CHECK_ONLY=0 ASSUME_YES=0 SKIP_DEVICES=0 NO_PULL=0 SKIP_BACKUP=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --skip-devices) SKIP_DEVICES=1 ;;
    --no-pull) NO_PULL=1 ;;
    --skip-backup) SKIP_BACKUP=1 ;;
    PREV_COMMIT=*) ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "install: unknown option $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then B=$'\033[1m' G=$'\033[32m' Y=$'\033[33m' R=$'\033[31m' N=$'\033[0m'; else B='' G='' Y='' R='' N=''; fi
step() { echo; echo "${B}==> $*${N}"; }
ok()   { echo "  ${G}ok${N}    $*"; }
warn() { echo "  ${Y}warn${N}  $*"; }
die()  { echo "  ${R}error${N} $*" >&2; exit 1; }
interactive() { [ "$ASSUME_YES" = 0 ] && [ -t 0 ]; }
confirm() { # confirm "question": yes in --yes mode, else ask (default no)
  interactive || return 0
  local a; read -rp "  $1 [y/N]: " a; [[ "$a" =~ ^[Yy] ]]
}
trap 'echo "  ${R}error${N} install stopped at line $LINENO: $BASH_COMMAND" >&2' ERR

# One run at a time (a cron update and a manual run must not overlap).
exec 9>.install.lock
command -v flock >/dev/null 2>&1 && { flock -n 9 || die "another install.sh is running"; }

# --- 1. Prerequisites -------------------------------------------------------
step "Checking prerequisites"
command -v docker >/dev/null 2>&1 || die "Docker is not installed. Install it with: curl -fsSL https://get.docker.com | sh"
docker compose version >/dev/null 2>&1 || die "The Docker compose plugin is missing: sudo apt install docker-compose-plugin"
docker info >/dev/null 2>&1 || die "Cannot talk to the Docker daemon. Run: sudo usermod -aG docker \"\$USER\" && newgrp docker"
ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null), compose $(docker compose version --short)"
# KTD18: the libsql client ships Linux wheels for x86_64 only.
arch="$(uname -m)"
[ "$arch" = "x86_64" ] || die "This host is $arch; the libsql client only ships x86_64 Linux wheels. Use an x86_64 host."
ok "x86_64 host"

# --- 1b. Code update ---------------------------------------------------------
PREV_COMMIT=""
if [ "$NO_PULL" = 0 ] && [ -d .git ] && command -v git >/dev/null 2>&1; then
  step "Updating code"
  PREV_COMMIT="$(git rev-parse --short HEAD)"
  if ! git diff --quiet || ! git diff --cached --quiet; then
    warn "local edits to tracked files; not pulling (commit or stash them, or use --no-pull)"
    git status --short --untracked-files=no | sed 's/^/          /'
  elif ! git fetch --quiet origin 2>/dev/null; then
    warn "cannot reach the git remote; installing the code already here"
  else
    upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    if [ -z "$upstream" ]; then
      warn "branch has no upstream; not pulling"
    elif [ "$(git rev-parse HEAD)" = "$(git rev-parse "$upstream")" ]; then
      ok "already up to date ($PREV_COMMIT)"
    else
      echo "  incoming:"; git log --oneline "HEAD..$upstream" | sed 's/^/          /'
      git merge --ff-only --quiet "$upstream" || die "cannot fast-forward to $upstream (local commits?); resolve it with git, then re-run"
      ok "updated $PREV_COMMIT -> $(git rev-parse --short HEAD)"
      # The script itself may have changed: run the new one.
      exec 9>&-
      exec "$0" --no-pull "$@" PREV_COMMIT="$PREV_COMMIT"
    fi
  fi
fi
for arg in "$@"; do case "$arg" in PREV_COMMIT=*) PREV_COMMIT="${arg#PREV_COMMIT=}" ;; esac; done
[ -n "$PREV_COMMIT" ] || PREV_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || true)"

# --- 2. .env ----------------------------------------------------------------
env_value() { # value of KEY in .env, empty if unset
  sed -n "s/^$1=//p" .env | tail -n 1
}
set_env() { # set KEY=VALUE in .env, replacing an existing line or appending
  local tmp
  tmp="$(mktemp .env.XXXXXX)"
  KEY="$1" VALUE="$2" awk 'BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"]; done = 0 }
    index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' .env > "$tmp"
  chmod 600 "$tmp" && mv "$tmp" .env
}
ask() { # ask KEY "question" [secret]
  local answer
  if [ "${3:-}" = secret ]; then read -rsp "  $2: " answer; echo; else read -rp "  $2: " answer; fi
  answer="${answer#"${answer%%[![:space:]]*}"}"; answer="${answer%"${answer##*[![:space:]]}"}"
  [ -n "$answer" ] && set_env "$1" "$answer"
  return 0
}

step "Configuring .env"
created=0
if [ ! -f .env ]; then
  (umask 077 && cp .env.example .env)
  ok "created .env from .env.example (mode 600)"
  created=1
fi
chmod 600 .env
if [ "$created" = 0 ]; then
  # Keep a copy before touching it, and add settings new releases introduced.
  (umask 077 && cp .env ".env.bak.$(date +%Y%m%d-%H%M%S)")
  ls -1t .env.bak.* 2>/dev/null | tail -n +6 | xargs -r rm -f   # keep the last 5
  added=()
  while IFS= read -r line; do
    key="${line%%=*}"
    grep -q "^$key=" .env || { printf '%s\n' "$line" >> .env; added+=("$key"); }
  done < <(grep -E '^[A-Z][A-Z0-9_]*=' .env.example)
  if [ "${#added[@]}" -gt 0 ]; then ok "added new settings with defaults: ${added[*]}"; fi
  ok ".env backed up"
fi

ask_default() { # ask KEY "question": Enter keeps the current value
  local answer current
  current="$(env_value "$1")"
  read -rp "  $2 [$current]: " answer
  [ -n "$answer" ] && set_env "$1" "$answer"
  return 0
}
if [ "$created" = 1 ] && interactive; then
  echo "  Door devices (Enter keeps the value shown):"
  ask_default ESSL_X990_IP "X990 IP address"
  ask_default ESSL_X990_PASSWORD "X990 comm key (0 if none)"
  ask_default ESSL_C3_IP "C3-400 IP address"
  ask "ESSL_C3_PASSWORD" "C3-400 communication password (Enter if none)" secret
fi

# key | question | secret?
REQUIRED=(
  "TURSO_DATABASE_URL|Turso database URL (turso db show essl --url)|"
  "TURSO_AUTH_TOKEN|Turso read-write token (turso db tokens create essl)|secret"
  "TURSO_EXPORT_AUTH_TOKEN|Turso read-only token (turso db tokens create essl --read-only)|secret"
  "R2_ACCOUNT_ID|Cloudflare R2 account ID|"
  "R2_BUCKET|R2 bucket name, e.g. essl-attendance|"
  "R2_EXPORT_ACCESS_KEY_ID|R2 API token Access Key ID|"
  "R2_EXPORT_SECRET_ACCESS_KEY|R2 API token Secret Access Key|secret"
)
missing=()
for row in "${REQUIRED[@]}"; do
  IFS='|' read -r key question secret <<<"$row"
  if [ -z "$(env_value "$key")" ] && interactive; then ask "$key" "$question" "$secret"; fi
  [ -n "$(env_value "$key")" ] || missing+=("$key")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "  ${R}error${N} .env is missing required values:" >&2
  printf '          %s\n' "${missing[@]}" >&2
  echo "        Fill them in .env (README \"Prerequisites\" says where each comes from) and run again." >&2
  exit 1
fi
ok "Turso and R2 values present"

# Tunnel: bundled cloudflared only when a token is set; otherwise the host's own.
if [ -n "$(env_value TUNNEL_TOKEN)" ]; then
  [ "$(env_value COMPOSE_PROFILES)" = tunnel ] || set_env COMPOSE_PROFILES tunnel
  ok "bundled Cloudflare Tunnel on (TUNNEL_TOKEN set)"
else
  ok "no bundled tunnel; route your host's tunnel to http://localhost:$(env_value ESSL_WEB_PORT | grep . || echo 8080)"
fi

case "$(env_value TURSO_DATABASE_URL)" in
  libsql://*|https://*) ;;
  *) die "TURSO_DATABASE_URL must start with libsql:// or https:// (turso db show essl --url)" ;;
esac
[ "$(env_value TURSO_AUTH_TOKEN)" != "$(env_value TURSO_EXPORT_AUTH_TOKEN)" ] \
  || die "TURSO_EXPORT_AUTH_TOKEN must be a separate read-only token (turso db tokens create essl --read-only)"

if [ -z "$(env_value CLOUDFLARE_API_TOKEN)" ] && [ -z "$(env_value RESEND_API_KEY)" ] && interactive; then
  echo "  Sign-in codes are emailed. No mail provider is set; Resend is the quickest (resend.com, verify your domain)."
  ask RESEND_API_KEY "Resend API key (Enter to skip)" secret
  [ -n "$(env_value RESEND_API_KEY)" ] && [ -z "$(env_value RESEND_FROM_ADDRESS)" ] \
    && ask RESEND_FROM_ADDRESS "From address, e.g. Attendance <attendance@yourcompany.com>"
fi
if [ -n "$(env_value CLOUDFLARE_API_TOKEN)" ]; then ok "mail: Cloudflare Email Sending"
elif [ -n "$(env_value RESEND_API_KEY)" ]; then ok "mail: Resend"
else warn "no mail provider; nobody can receive a sign-in code (README \"Sign-in email\")"; fi

if [ -n "$(env_value TURNSTILE_SITE_KEY)" ] && [ -n "$(env_value TURNSTILE_SECRET_KEY)" ]; then ok "Turnstile bot check on"
else warn "Turnstile not set; sign-in has no bot check (README \"Protection\")"; fi

# First admin: a one-time /setup token unless an admin email is configured.
if [ -z "$(env_value ESSL_SETUP_TOKEN)" ] && [ -z "$(env_value ESSL_BOOTSTRAP_ADMIN_EMAIL)" ]; then
  set_env ESSL_SETUP_TOKEN "$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  ok "generated a one-time ESSL_SETUP_TOKEN for the first administrator"
fi

# --- 2b. Pre-update snapshot -------------------------------------------------
# An update can migrate the schema. Before that, push a fresh export to R2 with
# the image that is running now (same as the nightly one, outside the schedule).
if [ "$CHECK_ONLY" = 0 ] && [ "$SKIP_BACKUP" = 0 ] && [ -n "$(docker compose ps -q web 2>/dev/null)" ]; then
  step "Snapshot before update (R2)"
  if docker compose run --rm --no-deps nightly-export python -m essl_hub.nightly_export --once; then
    ok "snapshot uploaded to R2 nightly/"
  else
    warn "snapshot failed (see above). Turso point-in-time recovery still covers you."
    confirm "Continue the update without a fresh snapshot?" || die "stopped; fix R2 or re-run with --skip-backup"
  fi
fi

# --- 3. Build ---------------------------------------------------------------
step "Building images (first run takes a few minutes)"
docker compose build --quiet
ok "images built"

# Every service runs as 10001:10001 and must be able to write /data.
docker compose run --rm --no-deps --entrypoint sh web -c \
  'id -u | grep -qx 10001 && touch /data/.write-test && rm -f /data/.write-test' >/dev/null \
  || die "the essl-data volume is not writable by UID 10001. Fix: docker compose run --rm --no-deps -u 0 --entrypoint chown web -R 10001:10001 /data"
ok "data volume writable"

# --- 4. Devices -------------------------------------------------------------
if [ "$SKIP_DEVICES" = 0 ]; then
  step "Testing the door devices (TCP 4370)"
  # The C3 allows one session: pause the running collector during the test.
  running_collector="$(docker compose ps -q --status running collector 2>/dev/null || true)"
  [ -n "$running_collector" ] && docker compose stop collector >/dev/null
  if docker compose run --rm --no-deps collector python diagnose.py; then
    ok "both devices answer"
  else
    warn "a device check failed (see above and README \"Device setup and troubleshooting\")."
    warn "The collector keeps retrying, so the rest of the stack can still start."
  fi
  [ -n "$running_collector" ] && [ "$CHECK_ONLY" = 1 ] && docker compose start collector >/dev/null
fi

if [ "$CHECK_ONLY" = 1 ]; then
  step "Check finished; nothing was started (--check)"
  exit 0
fi

# --- 5. Start ---------------------------------------------------------------
step "Starting the stack"
docker compose up -d --remove-orphans
printf '  waiting for the web app'
healthy=0
for _ in $(seq 1 60); do
  if docker compose exec -T web python -c \
      "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status != 200)" \
      >/dev/null 2>&1; then healthy=1; break; fi
  printf '.'; sleep 2
done
echo
docker compose ps --format 'table {{.Service}}\t{{.Status}}'
if [ "$healthy" = 1 ]; then
  ok "web app is up"
  docker image prune -f >/dev/null 2>&1 || true
else
  warn "web app did not answer within 2 minutes: docker compose logs web"
  if [ -n "$PREV_COMMIT" ] && [ "$PREV_COMMIT" != "$(git rev-parse --short HEAD 2>/dev/null)" ]; then
    warn "to go back to the previous version: git checkout $PREV_COMMIT && ./deploy/install.sh --no-pull"
  fi
  exit 1
fi

# --- 6. Next steps ----------------------------------------------------------
step "Done"
echo "  Open your public hostname (e.g. https://attendance.yourcompany.com)."
if [ -n "$(env_value ESSL_SETUP_TOKEN)" ]; then
  echo "  First administrator: open /setup and enter this token:"
  echo "      ${B}$(env_value ESSL_SETUP_TOKEN)${N}"
  echo "  The page closes itself once an admin exists. Then clear ESSL_SETUP_TOKEN in .env."
fi
echo "  Then People -> Import roster, and check People -> ID issues."
echo "  Logs: docker compose logs -f collector web"
