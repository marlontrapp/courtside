#!/usr/bin/env bash
#
# Docker-free local development & test environment for CourtControl.
#
# Creates a Python virtualenv with the project dependencies and a private,
# project-local PostgreSQL cluster (in ./.pgdata) that listens only on
# 127.0.0.1:$PG_PORT — it never touches a system-wide PostgreSQL install.
# `run` also starts a private Redis (./.redisdata, 127.0.0.1:$REDIS_PORT)
# for Celery, plus the Django dev server, Celery worker and Celery beat.
#
# Running as root: PostgreSQL refuses to run as root, so the script creates
# (once, via useradd) a dedicated unprivileged system user ($PG_OS_USER) and
# runs only the server binaries (initdb, pg_ctl) as that user. Because a
# project under /root is not reachable by other users, the cluster then lives
# in $PG_ROOT_BASE/<project>-<id>/ (owned by that user, mode 700) instead of
# ./.pgdata. Everything else (venv, pip, psql, tests) runs as root.
#
# Usage:  scripts/test_env.sh <command> [args...]
#         (or via the Makefile: `make help`)
#
# Commands:
#   setup              Install Python deps, init + start PostgreSQL
#   run                Run the whole stack in the foreground (Ctrl-C stops it):
#                      PostgreSQL + Redis + migrate + runserver + Celery worker/beat
#                      (without redis-server: web + PostgreSQL only)
#   stop               Stop PostgreSQL and Redis if left running
#   seed [--reset]     Load development seed data (clubs, courts, players, bookings);
#                      `run` does this automatically (SEED=0 to skip)
#   deps               Create .venv and install requirements.txt (cached)
#   test [labels...]   Run the Django test suite; extra args go to `manage.py test`
#   smoke [args...]    Smoke-test the `run` stack over HTTP (club registration),
#                      starting it first if it isn't running (and stopping it
#                      after); cleans up its own data; extra args go to the command
#   db-start           Init (first run) and start the local PostgreSQL cluster
#   db-stop            Stop the local PostgreSQL cluster
#   db-status          Show PostgreSQL/Redis/venv status and connection info
#   db-shell           Open psql against the local database
#   db-reset           Stop PostgreSQL/Redis and delete their data directories
#   clean              db-reset + delete the virtualenv
#   install-postgres   Install PostgreSQL via brew/apt/dnf/pacman/apk (uses sudo)
#   install-redis      Install Redis (for Celery) the same way
#
# Environment overrides:
#   PG_PORT=54329            TCP port for the local cluster
#   PG_BIN=/path/to/bin      Directory containing initdb/pg_ctl (auto-detected)
#   PYTHON=python3.13        Interpreter used to create the virtualenv
#   VENV_DIR=.venv           Virtualenv location
#   KEEP_DB_RUNNING=1        Don't stop a cluster that `test` had to start
#   PG_OS_USER=courtcontrol-pg               OS user for PostgreSQL (root only)
#   PG_ROOT_BASE=/var/lib/courtcontrol-testdb Cluster parent dir (root only)
#   TEST_SETTINGS=config.settings_test
#   RUN_ADDR=127.0.0.1:8000  Django dev server address (0.0.0.0:8000 = all interfaces)
#   DEV_SUPERUSER_EMAIL=admin@example.com    Superuser `run` creates if missing
#   DEV_SUPERUSER_PASSWORD=admin123          (local testing only — weak on purpose)
#   SEED=0                   Don't load seed data in `run`
#   SMOKE_BASE_URL=http://127.0.0.1:8000     Server `smoke` targets (default: from RUN_ADDR;
#                                            when set, `smoke` never starts the stack)
#   SMOKE_START_TIMEOUT=300  Seconds `smoke` waits for the stack it starts
#   REDIS_PORT=63790         TCP port for the local Redis
#   REDIS_BIN=/path/to/redis-server   (auto-detected; valkey-server also works)
#
# Compatible with bash 3.2 (macOS default) and newer.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# --- Configuration ------------------------------------------------------------

VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
PYTHON="${PYTHON:-}"

IS_ROOT=0
[[ "$(id -u)" -eq 0 ]] && IS_ROOT=1
PG_OS_USER="${PG_OS_USER:-courtcontrol-pg}"
PG_ROOT_BASE="${PG_ROOT_BASE:-/var/lib/courtcontrol-testdb}"

if [[ -z "${PG_HOME:-}" ]]; then
    if [[ "$IS_ROOT" -eq 1 ]]; then
        # One cluster per checkout: <dirname>-<checksum of absolute path>.
        PG_HOME="$PG_ROOT_BASE/$(basename "$ROOT_DIR")-$(printf '%s' "$ROOT_DIR" | cksum | cut -d' ' -f1)"
    else
        PG_HOME="$ROOT_DIR/.pgdata"
    fi
fi
PG_DATA="$PG_HOME/data"
PG_LOG="$PG_HOME/postgres.log"
PG_HOST="127.0.0.1"
PG_PORT="${PG_PORT:-54329}"
PG_USER="${PG_USER:-courtcontrol}"
PG_DB="${PG_DB:-courtcontrol}"
PG_BIN="${PG_BIN:-}"
TEST_SETTINGS="${TEST_SETTINGS:-config.settings_test}"
KEEP_DB_RUNNING="${KEEP_DB_RUNNING:-0}"

REDIS_HOME="${REDIS_HOME:-$ROOT_DIR/.redisdata}"
REDIS_HOST="127.0.0.1"
REDIS_PORT="${REDIS_PORT:-63790}"
REDIS_BIN="${REDIS_BIN:-}"
RUN_ADDR="${RUN_ADDR:-127.0.0.1:8000}"
DEV_SUPERUSER_EMAIL="${DEV_SUPERUSER_EMAIL:-admin@example.com}"
DEV_SUPERUSER_PASSWORD="${DEV_SUPERUSER_PASSWORD:-admin123}"

MIN_PG_MAJOR=14        # Django 5.2 minimum
MIN_PY="3.10"          # Django 5.2 minimum
TARGET_PY="3.13"       # Matches Dockerfile (python:3.13-slim)

PG_MAJOR=""
PSQL=""
DROP_PRIVS=""   # runuser | setpriv — chosen when running as root
REDIS_SERVER="" # resolved redis-server binary

# --- Output helpers -----------------------------------------------------------

if [[ -t 1 ]]; then
    C_BLUE=$'\033[34m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_RED=$'\033[31m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
    C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BOLD=""; C_RESET=""
fi

info() { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
ok()   { printf '%s ✔ %s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s !  %s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%s ✖  %s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

hash_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# --- Python / virtualenv ------------------------------------------------------

python_ok() {
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= tuple(map(int, '$MIN_PY'.split('.'))) else 1)" \
        >/dev/null 2>&1
}

find_python() {
    if [[ -n "$PYTHON" ]]; then
        command -v "$PYTHON" >/dev/null 2>&1 || die "PYTHON=$PYTHON not found."
        python_ok "$PYTHON" || die "PYTHON=$PYTHON is older than $MIN_PY."
        return 0
    fi
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
            PYTHON="$candidate"
            return 0
        fi
    done
    die "Python >= $MIN_PY not found. Install Python $TARGET_PY or set PYTHON=/path/to/python."
}

cmd_deps() {
    local venv_py="$VENV_DIR/bin/python"

    if [[ -x "$venv_py" ]] && ! "$venv_py" -c "import sys" >/dev/null 2>&1; then
        warn "Virtualenv at $VENV_DIR is broken — recreating."
        rm -rf "$VENV_DIR"
    fi

    if [[ ! -x "$venv_py" ]]; then
        find_python
        local version
        version="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
        [[ "$version" == "$TARGET_PY" ]] \
            || warn "Using Python $version (production image uses $TARGET_PY)."
        info "Creating virtualenv in ${VENV_DIR#$ROOT_DIR/} with $PYTHON ($version)"
        "$PYTHON" -m venv "$VENV_DIR" \
            || die "venv creation failed. On Debian/Ubuntu: sudo apt-get install python3-venv"
    fi

    # Reinstall only when requirements.txt changed since the last install.
    local stamp="$VENV_DIR/.requirements.sha256"
    local want
    want="$(hash_file requirements.txt)"
    if [[ -f "$stamp" && "$(cat "$stamp")" == "$want" ]]; then
        ok "Python dependencies up to date"
        return 0
    fi

    info "Installing Python dependencies (requirements.txt)"
    "$venv_py" -m pip install --quiet --disable-pip-version-check --upgrade pip
    "$venv_py" -m pip install --quiet --disable-pip-version-check -r requirements.txt
    echo "$want" >"$stamp"
    ok "Python dependencies installed"
}

# --- PostgreSQL discovery -----------------------------------------------------

pg_major_of() {
    # "initdb (PostgreSQL) 16.15 (Ubuntu ...)" -> 16
    "$1/initdb" --version 2>/dev/null | awk '{print $3}' | sed 's/[^0-9].*//'
}

find_pg_bin() {
    local candidates="" dir v major

    if [[ -n "$PG_BIN" ]]; then
        candidates="$PG_BIN"
    else
        if command -v pg_config >/dev/null 2>&1; then
            candidates="$candidates $(pg_config --bindir 2>/dev/null || true)"
        fi
        if command -v initdb >/dev/null 2>&1; then
            candidates="$candidates $(dirname "$(command -v initdb)")"
        fi
        for v in 18 17 16 15 14; do
            candidates="$candidates /usr/lib/postgresql/$v/bin"                 # Debian/Ubuntu
            candidates="$candidates /usr/pgsql-$v/bin"                          # RHEL/Fedora (PGDG)
            candidates="$candidates /opt/homebrew/opt/postgresql@$v/bin"        # Homebrew (Apple Silicon)
            candidates="$candidates /usr/local/opt/postgresql@$v/bin"           # Homebrew (Intel)
            candidates="$candidates /Applications/Postgres.app/Contents/Versions/$v/bin"
        done
        candidates="$candidates /opt/homebrew/opt/postgresql/bin /usr/local/opt/postgresql/bin"
        candidates="$candidates /Applications/Postgres.app/Contents/Versions/latest/bin"
        candidates="$candidates /usr/bin /usr/local/bin"
    fi

    for dir in $candidates; do
        [[ -x "$dir/initdb" && -x "$dir/pg_ctl" ]] || continue
        major="$(pg_major_of "$dir")"
        [[ -n "$major" && "$major" -ge "$MIN_PG_MAJOR" ]] || continue
        PG_BIN="$dir"
        PG_MAJOR="$major"
        return 0
    done
    return 1
}

ensure_pg() {
    [[ -n "$PG_MAJOR" ]] && return 0
    if ! find_pg_bin; then
        die "PostgreSQL >= $MIN_PG_MAJOR server binaries (initdb, pg_ctl) not found.
    Install them with:   make install-postgres   (or: scripts/test_env.sh install-postgres)
    or point to them:    PG_BIN=/path/to/postgresql/bin make test"
    fi
    if [[ -x "$PG_BIN/psql" ]]; then
        PSQL="$PG_BIN/psql"
    elif command -v psql >/dev/null 2>&1; then
        PSQL="$(command -v psql)"
    else
        die "psql not found (expected next to $PG_BIN/initdb)."
    fi
    ensure_pg_os_user
}

# --- Root support: dedicated unprivileged OS user ----------------------------

nologin_shell() {
    local shell
    for shell in /usr/sbin/nologin /sbin/nologin /bin/false; do
        [[ -x "$shell" ]] && { echo "$shell"; return 0; }
    done
    echo /bin/false
}

select_drop_privs() {
    [[ "$IS_ROOT" -eq 1 ]] || return 0
    [[ "$(uname -s)" == "Linux" ]] \
        || die "Running as root is only supported on Linux. Run as a regular user instead."
    if command -v runuser >/dev/null 2>&1; then
        DROP_PRIVS="runuser"
    elif command -v setpriv >/dev/null 2>&1; then
        DROP_PRIVS="setpriv"
    else
        die "Running as root needs 'runuser' or 'setpriv' (util-linux package)."
    fi
}

ensure_pg_os_user() {
    [[ "$IS_ROOT" -eq 1 ]] || return 0
    select_drop_privs

    if ! id -u "$PG_OS_USER" >/dev/null 2>&1; then
        command -v useradd >/dev/null 2>&1 \
            || die "useradd not found (Debian/Ubuntu: apt-get install passwd; RHEL: dnf install shadow-utils)."
        info "Running as root: creating system user '$PG_OS_USER' to own PostgreSQL"
        # System account: no login shell, no password, no sudo, no home dir
        # created (its only writable location is the cluster dir below).
        useradd --system --user-group --no-create-home \
            --home-dir "$PG_ROOT_BASE" --shell "$(nologin_shell)" \
            --comment "CourtControl local test PostgreSQL" "$PG_OS_USER" \
            || die "useradd failed to create '$PG_OS_USER'."
        ok "User '$PG_OS_USER' created"
    fi

    # Parent: root-owned, traversable. Cluster dir: private to the PG user.
    mkdir -p "$PG_ROOT_BASE" "$PG_HOME"
    chmod 755 "$PG_ROOT_BASE"
    if [[ "$(stat -c %U "$PG_HOME")" != "$PG_OS_USER" ]]; then
        chown -R "$PG_OS_USER:" "$PG_HOME"
    fi
    chmod 700 "$PG_HOME"
}

# Run a PostgreSQL server binary (initdb/pg_ctl). As root, drop to PG_OS_USER
# and start from "/" — the caller's cwd (e.g. /root/...) may not be reachable.
pg_run() {
    if [[ "$IS_ROOT" -ne 1 ]]; then
        "$@"
    elif [[ "$DROP_PRIVS" == "runuser" ]]; then
        (cd / && runuser -u "$PG_OS_USER" -- "$@")
    else
        (cd / && setpriv --reuid="$PG_OS_USER" --regid="$PG_OS_USER" --init-groups -- "$@")
    fi
}

# --- PostgreSQL cluster lifecycle ---------------------------------------------

db_is_running() {
    [[ -f "$PG_DATA/PG_VERSION" ]] && pg_run "$PG_BIN/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1
}

running_port() {
    # Line 4 of postmaster.pid is the port the running server is bound to.
    sed -n '4p' "$PG_DATA/postmaster.pid" 2>/dev/null || true
}

port_in_use() {
    local host="${2:-$PG_HOST}" port="${1:-$PG_PORT}"
    (exec 3<>"/dev/tcp/$host/$port") >/dev/null 2>&1
}

psql_admin() {
    "$PSQL" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d postgres -XAtq -v ON_ERROR_STOP=1 -c "$1"
}

db_init() {
    if [[ -f "$PG_DATA/PG_VERSION" ]]; then
        local existing
        existing="$(cat "$PG_DATA/PG_VERSION")"
        if [[ "$existing" != "$PG_MAJOR" ]]; then
            die "Cluster in ${PG_DATA#$ROOT_DIR/} was created by PostgreSQL $existing, but binaries are $PG_MAJOR.
    Run 'make db-reset' to recreate it (test data only — nothing of value is lost)."
        fi
        return 0
    fi

    info "Initializing PostgreSQL $PG_MAJOR cluster in ${PG_DATA#$ROOT_DIR/}"
    mkdir -p "$PG_HOME"
    if ! pg_run "$PG_BIN/initdb" -D "$PG_DATA" -U "$PG_USER" --auth=trust \
            --encoding=UTF8 --locale=C >"$PG_HOME/initdb.log" 2>&1; then
        cat "$PG_HOME/initdb.log" >&2
        rm -rf "$PG_DATA"
        die "initdb failed (full log above)."
    fi

    # Throwaway test cluster: TCP on localhost only, no unix socket (avoids
    # socket-path permission/length issues), durability traded for speed.
    cat >>"$PG_DATA/postgresql.conf" <<EOF

# --- CourtControl local test cluster (scripts/test_env.sh) ---
listen_addresses = '$PG_HOST'
unix_socket_directories = ''
fsync = off
synchronous_commit = off
full_page_writes = off
EOF
    ok "Cluster initialized (user '$PG_USER', trust auth, localhost only)"
}

cmd_db_start() {
    ensure_pg
    db_init

    if db_is_running; then
        PG_PORT="$(running_port)"
        ok "PostgreSQL $PG_MAJOR already running on $PG_HOST:$PG_PORT"
    else
        if port_in_use; then
            die "Port $PG_PORT is already in use by another process. Pick another: PG_PORT=54330 make test"
        fi
        info "Starting PostgreSQL $PG_MAJOR on $PG_HOST:$PG_PORT"
        if ! pg_run "$PG_BIN/pg_ctl" -D "$PG_DATA" -l "$PG_LOG" -w -t 30 -o "-p $PG_PORT" start >/dev/null; then
            tail -n 20 "$PG_LOG" >&2 || true
            die "PostgreSQL failed to start (log: ${PG_LOG#$ROOT_DIR/})."
        fi
        ok "PostgreSQL started"
    fi

    if [[ "$(psql_admin "SELECT 1 FROM pg_database WHERE datname = '$PG_DB'")" != "1" ]]; then
        psql_admin "CREATE DATABASE \"$PG_DB\"" >/dev/null
        ok "Database '$PG_DB' created"
    fi

    # Reservation's ExclusionConstraint needs btree_gist (PostgreSQL contrib).
    if [[ "$(psql_admin "SELECT 1 FROM pg_available_extensions WHERE name = 'btree_gist'")" != "1" ]]; then
        die "PostgreSQL extension 'btree_gist' is not available.
    Install the contrib package (Debian/Ubuntu: postgresql-contrib, RHEL: postgresql<ver>-contrib)."
    fi
}

cmd_db_stop() {
    ensure_pg
    if db_is_running; then
        pg_run "$PG_BIN/pg_ctl" -D "$PG_DATA" -m fast -w stop >/dev/null
        ok "PostgreSQL stopped"
    else
        ok "PostgreSQL is not running"
    fi
}

cmd_db_status() {
    ensure_pg
    printf '%sPostgreSQL%s\n' "$C_BOLD" "$C_RESET"
    printf '  binaries : %s (v%s)\n' "$PG_BIN" "$PG_MAJOR"
    printf '  data dir : %s\n' "$PG_DATA"
    if [[ "$IS_ROOT" -eq 1 ]]; then
        printf '  os user  : %s (running as root; server runs via %s)\n' "$PG_OS_USER" "$DROP_PRIVS"
    fi
    if db_is_running; then
        PG_PORT="$(running_port)"
        printf '  status   : %srunning%s on %s:%s\n' "$C_GREEN" "$C_RESET" "$PG_HOST" "$PG_PORT"
    elif [[ -f "$PG_DATA/PG_VERSION" ]]; then
        printf '  status   : %sstopped%s\n' "$C_YELLOW" "$C_RESET"
    else
        printf '  status   : %snot initialized%s\n' "$C_YELLOW" "$C_RESET"
    fi
    printf '  url      : %s\n' "$(database_url)"
    printf '%sRedis%s (Celery broker)\n' "$C_BOLD" "$C_RESET"
    if find_redis; then
        printf '  binary   : %s\n' "$REDIS_SERVER"
        if redis_is_running; then
            printf '  status   : %srunning%s on %s:%s\n' "$C_GREEN" "$C_RESET" "$REDIS_HOST" "$(redis_running_port)"
        else
            printf '  status   : %sstopped%s\n' "$C_YELLOW" "$C_RESET"
        fi
    else
        printf '  status   : %snot installed%s (make install-redis) — `run` skips Celery\n' "$C_YELLOW" "$C_RESET"
    fi
    printf '%sPython%s\n' "$C_BOLD" "$C_RESET"
    if [[ -x "$VENV_DIR/bin/python" ]]; then
        printf '  venv     : %s (%s)\n' "$VENV_DIR" "$("$VENV_DIR/bin/python" --version 2>&1)"
    else
        printf '  venv     : %snot created%s\n' "$C_YELLOW" "$C_RESET"
    fi
}

cmd_db_shell() {
    cmd_db_start
    exec "$PSQL" -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB"
}

cmd_db_reset() {
    if [[ -d "$PG_HOME" ]]; then
        if find_pg_bin && select_drop_privs && db_is_running; then
            pg_run "$PG_BIN/pg_ctl" -D "$PG_DATA" -m immediate -w stop >/dev/null || true
        fi
        rm -rf "$PG_HOME"
        ok "Removed ${PG_HOME#$ROOT_DIR/}"
    else
        ok "No PostgreSQL data to reset"
    fi
    if [[ -d "$REDIS_HOME" ]]; then
        redis_stop_quiet
        rm -rf "$REDIS_HOME"
        ok "Removed ${REDIS_HOME#$ROOT_DIR/}"
    fi
}

cmd_clean() {
    cmd_db_reset
    rm -rf "$VENV_DIR"
    ok "Removed ${VENV_DIR#$ROOT_DIR/}"
}

database_url() {
    echo "postgres://$PG_USER@$PG_HOST:$PG_PORT/$PG_DB"
}

# --- Redis (Celery broker / result backend) ----------------------------------

find_redis() {
    [[ -n "$REDIS_SERVER" ]] && return 0
    local candidate
    for candidate in \
        "$REDIS_BIN" \
        "$(command -v redis-server 2>/dev/null || true)" \
        "$(command -v valkey-server 2>/dev/null || true)" \
        /usr/bin/redis-server /usr/sbin/redis-server /usr/local/bin/redis-server \
        /opt/homebrew/bin/redis-server /usr/bin/valkey-server; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            REDIS_SERVER="$candidate"
            return 0
        fi
    done
    return 1
}

redis_pid() {
    cat "$REDIS_HOME/redis.pid" 2>/dev/null || true
}

redis_running_port() {
    cat "$REDIS_HOME/redis.port" 2>/dev/null || echo "$REDIS_PORT"
}

redis_is_running() {
    local pid
    pid="$(redis_pid)"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

redis_ping() {
    # Speak RESP over bash's /dev/tcp — no redis-cli needed.
    (
        exec 3<>"/dev/tcp/$REDIS_HOST/$1" || exit 1
        printf 'PING\r\n' >&3
        IFS= read -r -t 2 reply <&3 || exit 1
        [[ "$reply" == "+PONG"* ]]
    ) 2>/dev/null
}

cmd_redis_start() {
    find_redis || die "redis-server not found. Install it with: make install-redis (or set REDIS_BIN=/path/to/redis-server)"
    mkdir -p "$REDIS_HOME"

    if redis_is_running; then
        REDIS_PORT="$(redis_running_port)"
        ok "Redis already running on $REDIS_HOST:$REDIS_PORT"
        return 0
    fi
    if port_in_use "$REDIS_PORT" "$REDIS_HOST"; then
        die "Port $REDIS_PORT is already in use by another process. Pick another: REDIS_PORT=63791 make run"
    fi

    info "Starting Redis on $REDIS_HOST:$REDIS_PORT"
    # Dev broker: localhost only, no persistence (queued tasks are disposable;
    # the beat schedule itself lives in PostgreSQL).
    "$REDIS_SERVER" \
        --bind "$REDIS_HOST" --port "$REDIS_PORT" \
        --dir "$REDIS_HOME" \
        --daemonize yes \
        --pidfile "$REDIS_HOME/redis.pid" \
        --logfile "$REDIS_HOME/redis.log" \
        --save "" --appendonly no >/dev/null
    echo "$REDIS_PORT" >"$REDIS_HOME/redis.port"

    local i
    for i in $(seq 1 50); do
        redis_ping "$REDIS_PORT" && { ok "Redis started"; return 0; }
        sleep 0.2
    done
    tail -n 20 "$REDIS_HOME/redis.log" >&2 || true
    die "Redis failed to start (log: ${REDIS_HOME#$ROOT_DIR/}/redis.log)."
}

redis_stop_quiet() {
    local pid i
    pid="$(redis_pid)"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null || return 0
    kill -TERM "$pid" 2>/dev/null || true
    for i in $(seq 1 50); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.2
    done
    rm -f "$REDIS_HOME/redis.pid"
}

cmd_redis_stop() {
    if redis_is_running; then
        redis_stop_quiet
        ok "Redis stopped"
    else
        ok "Redis is not running"
    fi
}

cmd_stop() {
    cmd_redis_stop
    cmd_db_stop
}

# --- run: supervise web + Celery in the foreground ----------------------------

ensure_dev_superuser() {
    # Create-if-missing only: never duplicates the user or resets a password
    # you changed. create_superuser() skips AUTH_PASSWORD_VALIDATORS, so the
    # deliberately weak local password is accepted without warnings.
    # Values go through the environment, not the code string (no quoting issues).
    local result
    result="$(DEV_SUPERUSER_EMAIL="$DEV_SUPERUSER_EMAIL" \
        DEV_SUPERUSER_PASSWORD="$DEV_SUPERUSER_PASSWORD" \
        "$VENV_DIR/bin/python" manage.py shell -v0 -c '
import os
from django.contrib.auth import get_user_model

User = get_user_model()
email = os.environ["DEV_SUPERUSER_EMAIL"]
if User.objects.filter(email__iexact=email).exists():
    print("exists")
else:
    User.objects.create_superuser(email=email, password=os.environ["DEV_SUPERUSER_PASSWORD"])
    print("created")
')" || die "Could not create the development superuser."

    if [[ "$result" == "created" ]]; then
        ok "Superuser created: $DEV_SUPERUSER_EMAIL / $DEV_SUPERUSER_PASSWORD (local testing only)"
    else
        ok "Superuser $DEV_SUPERUSER_EMAIL already exists (password left unchanged)"
    fi

    case "${RUN_ADDR%:*}" in
        127.0.0.1|localhost|::1|"[::1]") ;;
        *) warn "RUN_ADDR=$RUN_ADDR exposes the server beyond this machine — the dev superuser has a weak password." ;;
    esac
}

SERVICE_NAMES=()
SERVICE_PIDS=()
RUN_PG_STARTED=0
RUN_REDIS_STARTED=0
RUN_SHUTTING_DOWN=0

prefix_output() {
    # Label each line with the service name. A bash loop (not awk: mawk
    # buffers piped input) keeps output live. Ignore Ctrl-C so services'
    # shutdown logs still get printed; exits on EOF when the service ends.
    trap '' INT
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
        printf '%s%s\n' "$1" "$line"
    done
}

start_service() {
    local name="$1" color="$2"
    shift 2
    local label
    label="$(printf '%s%-6s|%s ' "$color" "$name" "$C_RESET")"
    # Each service gets its own process group (setpgid + exec via the venv's
    # Python — portable, unlike setsid), so shutdown can signal the whole tree
    # (e.g. runserver's autoreload child, Celery's pool) with `kill -- -PGID`.
    # No bash job control: that would hand the terminal to foreground
    # commands and Ctrl-C would no longer reach this script.
    "$VENV_DIR/bin/python" -c \
        'import os, sys; os.setpgid(0, 0); os.execvp(sys.argv[1], sys.argv[1:])' \
        "$@" </dev/null > >(prefix_output "$label") 2>&1 &
    SERVICE_NAMES+=("$name")
    SERVICE_PIDS+=("$!")
}

run_shutdown() {
    [[ "$RUN_SHUTTING_DOWN" -eq 1 ]] && return 0
    RUN_SHUTTING_DOWN=1
    trap - INT TERM
    echo
    info "Shutting down..."

    local pid i alive
    for pid in ${SERVICE_PIDS[@]+"${SERVICE_PIDS[@]}"}; do
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done
    # Celery's warm shutdown lets running tasks finish; give it a moment.
    for i in $(seq 1 75); do
        alive=0
        for pid in ${SERVICE_PIDS[@]+"${SERVICE_PIDS[@]}"}; do
            kill -0 "$pid" 2>/dev/null && alive=1
        done
        [[ "$alive" -eq 0 ]] && break
        sleep 0.2
    done
    for pid in ${SERVICE_PIDS[@]+"${SERVICE_PIDS[@]}"}; do
        kill -KILL -- "-$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    ok "Services stopped"

    if [[ "$RUN_REDIS_STARTED" -eq 1 ]]; then cmd_redis_stop; fi
    if [[ "$RUN_PG_STARTED" -eq 1 && "$KEEP_DB_RUNNING" != "1" ]]; then cmd_db_stop; fi
}

cmd_run() {
    # Fail fast, before starting anything, if the web port is taken.
    local web_port="${RUN_ADDR##*:}"
    if port_in_use "$web_port" "127.0.0.1"; then
        die "Port $web_port is already in use. Pick another: RUN_ADDR=127.0.0.1:8001 make run"
    fi

    cmd_deps

    ensure_pg
    db_is_running || RUN_PG_STARTED=1
    cmd_db_start

    # Exported vars override the docker-oriented .env (hosts "db"/"redis").
    export DATABASE_URL PYTHONUNBUFFERED=1
    DATABASE_URL="$(database_url)"

    local with_celery=0
    if find_redis; then
        redis_is_running || RUN_REDIS_STARTED=1
        cmd_redis_start
        REDIS_PORT="$(redis_running_port)"
        export REDIS_URL="redis://$REDIS_HOST:$REDIS_PORT/0"
        export CELERY_BROKER_URL="redis://$REDIS_HOST:$REDIS_PORT/0"
        export CELERY_RESULT_BACKEND="redis://$REDIS_HOST:$REDIS_PORT/1"
        with_celery=1
    else
        warn "redis-server not found — starting WITHOUT Celery worker/beat."
        warn "Install it with 'make install-redis' (or set REDIS_BIN) to run the full stack."
    fi

    if [[ ! -f .env ]]; then
        warn "No .env file — using development defaults (DJANGO_DEBUG=True). See .env.example."
        export DJANGO_DEBUG="${DJANGO_DEBUG:-True}"
    fi

    info "Applying migrations"
    "$VENV_DIR/bin/python" manage.py migrate --noinput
    ensure_dev_superuser
    if [[ "${SEED:-1}" != "0" ]]; then
        info "Loading seed data (SEED=0 to skip)"
        "$VENV_DIR/bin/python" manage.py seed_dev_data \
            || warn "Seed data not loaded (see above) — continuing without it."
    fi

    trap 'run_shutdown; exit 0' INT TERM
    trap 'run_shutdown' EXIT

    start_service web "$C_GREEN" "$VENV_DIR/bin/python" manage.py runserver "$RUN_ADDR"
    if [[ "$with_celery" -eq 1 ]]; then
        start_service worker "$C_BLUE" "$VENV_DIR/bin/celery" -A config worker -l info
        start_service beat "$C_YELLOW" "$VENV_DIR/bin/celery" -A config beat -l info \
            --scheduler django_celery_beat.schedulers:DatabaseScheduler
    fi

    ok "Running: web on http://$RUN_ADDR$([[ "$with_celery" -eq 1 ]] && echo ', Celery worker + beat') — press Ctrl-C to stop"

    # Supervise: if any service exits on its own, stop the rest too.
    local i
    while :; do
        for i in "${!SERVICE_PIDS[@]}"; do
            if ! kill -0 "${SERVICE_PIDS[$i]}" 2>/dev/null; then
                warn "Service '${SERVICE_NAMES[$i]}' exited — stopping the stack."
                run_shutdown
                exit 1
            fi
        done
        sleep 1
    done
}

# --- Commands -----------------------------------------------------------------

cmd_setup() {
    cmd_deps
    cmd_db_start
    ok "Environment ready — run: make test"
}

cmd_test() {
    cmd_deps

    ensure_pg
    local started_here=0
    if ! db_is_running; then
        started_here=1
    fi
    cmd_db_start

    if [[ "$started_here" -eq 1 && "$KEEP_DB_RUNNING" != "1" ]]; then
        # Leave the machine as we found it; data persists in .pgdata for --keepdb.
        trap 'cmd_db_stop >/dev/null 2>&1 || true' EXIT
    fi

    # Exported vars take precedence over .env (django-environ uses setdefault),
    # so the docker-oriented .env (host "db") does not need editing.
    export DATABASE_URL
    DATABASE_URL="$(database_url)"

    info "Running tests (settings=$TEST_SETTINGS${*:+, args: $*})"
    local rc=0
    # --noinput: never prompt (e.g. to drop a test DB left by a --keepdb run),
    # so the suite behaves the same in a terminal, a pipe, or CI.
    "$VENV_DIR/bin/python" manage.py test --settings="$TEST_SETTINGS" --noinput "$@" || rc=$?

    if [[ "$rc" -eq 0 ]]; then ok "Tests passed"; else warn "Tests failed (exit $rc)"; fi
    return "$rc"
}

cmd_install_postgres() {
    local sudo=""
    [[ "$(id -u)" -ne 0 ]] && sudo="sudo"

    case "$(uname -s)" in
        Darwin)
            command -v brew >/dev/null 2>&1 || die "Homebrew not found — install it from https://brew.sh or use Postgres.app."
            info "Installing PostgreSQL 17 via Homebrew"
            brew install postgresql@17
            ;;
        Linux)
            if command -v apt-get >/dev/null 2>&1; then
                info "Installing PostgreSQL via apt (python3-venv included)"
                $sudo apt-get update
                $sudo apt-get install -y postgresql postgresql-contrib python3-venv
            elif command -v dnf >/dev/null 2>&1; then
                info "Installing PostgreSQL via dnf"
                $sudo dnf install -y postgresql-server postgresql-contrib
            elif command -v pacman >/dev/null 2>&1; then
                info "Installing PostgreSQL via pacman"
                $sudo pacman -S --needed --noconfirm postgresql
            elif command -v apk >/dev/null 2>&1; then
                info "Installing PostgreSQL via apk"
                $sudo apk add postgresql postgresql-contrib
            else
                die "Unsupported package manager — install PostgreSQL >= $MIN_PG_MAJOR manually."
            fi
            ;;
        *)
            die "Unsupported OS $(uname -s) — install PostgreSQL >= $MIN_PG_MAJOR manually."
            ;;
    esac
    ok "PostgreSQL installed. The local test cluster is private; the system service is not needed."
}

cmd_seed() {
    cmd_deps
    ensure_pg
    local started_here=0
    db_is_running || started_here=1
    cmd_db_start
    if [[ "$started_here" -eq 1 && "$KEEP_DB_RUNNING" != "1" ]]; then
        trap 'cmd_db_stop >/dev/null 2>&1 || true' EXIT
    fi

    export DATABASE_URL
    DATABASE_URL="$(database_url)"
    [[ -f .env ]] || export DJANGO_DEBUG="${DJANGO_DEBUG:-True}"

    info "Applying migrations"
    "$VENV_DIR/bin/python" manage.py migrate --noinput -v0
    "$VENV_DIR/bin/python" manage.py seed_dev_data "$@"
}

# --- Smoke test -----------------------------------------------------------------

SMOKE_RUN_PID=""   # `run` started by `smoke` (own process group; stopped when smoke ends)
SMOKE_RUN_LOG=""
SMOKE_RC=1
SMOKE_PG_WAS_UP=0
SMOKE_REDIS_WAS_UP=0

cmd_smoke() {
    # The smoke test targets the `run` stack and must share its database: it
    # creates and cleans up its data there. If no server is listening on
    # RUN_ADDR, start the stack (like `make run`) and stop it afterwards.
    local base_url="${SMOKE_BASE_URL:-http://${RUN_ADDR/#0.0.0.0:/127.0.0.1:}}"
    local web_port="${RUN_ADDR##*:}"

    if [[ -z "${SMOKE_BASE_URL:-}" ]] && ! port_in_use "$web_port" "127.0.0.1"; then
        smoke_start_stack "$base_url" "$web_port"
    fi

    ensure_pg
    [[ -x "$VENV_DIR/bin/python" ]] || die "No virtualenv — start the stack first: make run"
    db_is_running || die "PostgreSQL isn't running — start the stack first: make run"

    export DATABASE_URL
    DATABASE_URL="$(database_url)"
    [[ -f .env ]] || export DJANGO_DEBUG="${DJANGO_DEBUG:-True}"

    SMOKE_RC=0
    "$VENV_DIR/bin/python" manage.py smoke_club_registration --base-url "$base_url" "$@" || SMOKE_RC=$?
    return "$SMOKE_RC"
}

smoke_start_stack() {
    local base_url="$1" web_port="$2" timeout="${SMOKE_START_TIMEOUT:-300}" waited=0

    # Remember what was already up, so that exactly what we caused to start is stopped.
    ensure_pg
    db_is_running && SMOKE_PG_WAS_UP=1
    if find_redis && redis_is_running; then SMOKE_REDIS_WAS_UP=1; fi

    SMOKE_RUN_LOG="$(mktemp "${TMPDIR:-/tmp}/courtcontrol-smoke-run.XXXXXX")"
    # Seed data isn't needed by the smoke test, and loading it rewrites the dev
    # schedule — skipped unless SEED is set explicitly.
    info "No server on $base_url — starting the stack ('make run', SEED=${SEED:-0}); log: $SMOKE_RUN_LOG"
    trap 'smoke_stop_stack' EXIT
    trap 'smoke_stop_stack; exit 130' INT TERM
    # Own process group (set -m), so stopping can also reach a step `run` is
    # in the middle of (pip, migrate, seed) — not only the services it starts.
    set -m
    SEED="${SEED:-0}" bash "$ROOT_DIR/scripts/test_env.sh" run </dev/null >"$SMOKE_RUN_LOG" 2>&1 &
    SMOKE_RUN_PID=$!
    set +m

    # Ready when runserver accepts connections (it binds after its checks).
    until port_in_use "$web_port" "127.0.0.1"; do
        if ! kill -0 "$SMOKE_RUN_PID" 2>/dev/null; then
            smoke_show_log
            die "'make run' exited before the server came up (see the log above)."
        fi
        if (( waited >= timeout )); then
            smoke_show_log
            die "The server didn't come up within ${timeout}s (SMOKE_START_TIMEOUT)."
        fi
        sleep 1
        waited=$((waited + 1))
    done
    ok "Server up on $base_url (${waited}s)"
}

smoke_stop_stack() {
    trap - EXIT INT TERM
    [[ -n "$SMOKE_RUN_PID" ]] || return 0
    local pgid="$SMOKE_RUN_PID" i
    SMOKE_RUN_PID=""

    info "Stopping the stack started for the smoke test"
    # `run` traps TERM once its services are starting: it stops them (each in
    # its own process group) and then Redis/PostgreSQL if it started them.
    kill -TERM "$pgid" 2>/dev/null || true
    wait "$pgid" 2>/dev/null || true
    # Stopped earlier (e.g. still migrating), `run` exits without that
    # cleanup: end whatever is left of its group...
    kill -TERM -- "-$pgid" 2>/dev/null || true
    for i in $(seq 1 50); do
        kill -0 -- "-$pgid" 2>/dev/null || break
        sleep 0.2
    done
    kill -KILL -- "-$pgid" 2>/dev/null || true
    # ...and stop the databases it started, leaving the machine as we found it.
    if [[ "$SMOKE_REDIS_WAS_UP" -eq 0 ]] && find_redis && redis_is_running; then cmd_redis_stop; fi
    if [[ "$SMOKE_PG_WAS_UP" -eq 0 && "$KEEP_DB_RUNNING" != "1" ]] && db_is_running; then cmd_db_stop; fi
    ok "Stack stopped"

    if [[ -n "$SMOKE_RUN_LOG" && -f "$SMOKE_RUN_LOG" ]]; then
        if [[ "$SMOKE_RC" -eq 0 ]]; then
            rm -f "$SMOKE_RUN_LOG"
        else
            warn "Server log kept: $SMOKE_RUN_LOG"
        fi
    fi
}

smoke_show_log() {
    warn "Last lines of the 'make run' log ($SMOKE_RUN_LOG):"
    tail -n 25 "$SMOKE_RUN_LOG" >&2 || true
}

cmd_install_redis() {
    local sudo=""
    [[ "$(id -u)" -ne 0 ]] && sudo="sudo"

    case "$(uname -s)" in
        Darwin)
            command -v brew >/dev/null 2>&1 || die "Homebrew not found — install it from https://brew.sh."
            brew install redis
            ;;
        Linux)
            if command -v apt-get >/dev/null 2>&1; then
                $sudo apt-get update
                $sudo apt-get install -y redis-server
            elif command -v dnf >/dev/null 2>&1; then
                $sudo dnf install -y redis
            elif command -v pacman >/dev/null 2>&1; then
                $sudo pacman -S --needed --noconfirm valkey   # Redis-compatible; Arch dropped redis
            elif command -v apk >/dev/null 2>&1; then
                $sudo apk add redis
            else
                die "Unsupported package manager — install Redis (or Valkey) manually."
            fi
            ;;
        *)
            die "Unsupported OS $(uname -s) — install Redis manually."
            ;;
    esac
    ok "Redis installed. 'make run' starts its own private instance; the system service is not needed."
}

usage() {
    awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "${BASH_SOURCE[0]}"
}

main() {
    local cmd="${1:-help}"
    [[ $# -gt 0 ]] && shift

    case "$cmd" in
        setup)            cmd_setup ;;
        run)              cmd_run ;;
        stop)             cmd_stop ;;
        seed)             cmd_seed "$@" ;;
        deps)             cmd_deps ;;
        test)             cmd_test "$@" ;;
        smoke)            cmd_smoke "$@" ;;
        db-start)         cmd_db_start ;;
        db-stop)          cmd_db_stop ;;
        db-status)        cmd_db_status ;;
        db-shell)         cmd_db_shell ;;
        db-reset)         cmd_db_reset ;;
        clean)            cmd_clean ;;
        install-postgres) cmd_install_postgres ;;
        install-redis)    cmd_install_redis ;;
        help|-h|--help)   usage ;;
        *)                usage; die "Unknown command: $cmd" ;;
    esac
}

main "$@"
