#!/usr/bin/env bash
# Shared process-supervision helpers for start.sh and stop.sh.
#
# The Linux/macOS counterpart of scripts/_stack.ps1, and deliberately the same
# shape: one state file per service under .run/, holding the PID and the process
# start time. The start time is the PID-reuse guard — without it, `stop` would
# eventually kill whatever unrelated process inherited a recycled PID.
#
# Differences from Windows that are real, not cosmetic:
#   * Celery uses the prefork pool here. --pool=solo exists on Windows only
#     because prefork does not work there.
#   * PostgreSQL and Redis are not Windows services, so --docker brings them up
#     through docker-compose.yml instead.

set -euo pipefail

STACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$STACK_ROOT/.run"
LOG_DIR="$RUN_DIR/logs"

if [ -t 1 ]; then
    C_RESET=$'\033[0m'; C_CYAN=$'\033[36m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_GRAY=$'\033[90m'
else
    C_RESET=''; C_CYAN=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_GRAY=''
fi

step() { printf '\n%s=== %s ===%s\n' "$C_CYAN" "$1" "$C_RESET"; }
ok()   { printf '  %s[ok]  %s %s\n' "$C_GREEN" "$1" "$C_RESET"; }
warn() { printf '  %s[warn]%s %s\n' "$C_YELLOW" "$C_RESET" "$1"; }
info() { printf '  %s[..]   %s%s\n' "$C_GRAY" "$1" "$C_RESET"; }
fail() { printf '  %s[fail]%s %s\n' "$C_RED" "$C_RESET" "$1"; }
die()  { fail "$1"; exit 1; }

init_dirs() { mkdir -p "$LOG_DIR"; }

# ---------------------------------------------------------------------------
# .env -> environment. Child processes inherit it; that is how the workers get
# DATABASE_URL. `set -a` exports everything sourced between the two calls.
# ---------------------------------------------------------------------------
load_dotenv() {
    cd "$STACK_ROOT"
    if [ ! -f .env ]; then
        warn 'No .env found; copying .env.example'
        cp .env.example .env
    fi
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
}

# ---------------------------------------------------------------------------
# Service catalog: name|port|working dir|description|command
#
# Concurrencies mirror docker-compose.yml, which is the production topology.
# ---------------------------------------------------------------------------
QUEUE_SPEC='default:4 reminders:4 materialise:2 portal:1 ocr:2 returns:2 billing:1 exports:2'

stack_services() {
    local split="${1:-0}" all_queues total=4 q name conc
    all_queues=''
    for q in $QUEUE_SPEC; do
        name="${q%%:*}"
        all_queues="${all_queues:+$all_queues,}$name"
    done

    echo "web|8000|.|Django dev server|uv run python manage.py runserver 0.0.0.0:8000"
    echo "sse|8001|.|ASGI server for Server-Sent Events|uv run uvicorn config.asgi:application --host 0.0.0.0 --port 8001"

    if [ "$split" = "1" ]; then
        for q in $QUEUE_SPEC; do
            name="${q%%:*}"; conc="${q##*:}"
            echo "worker-$name|0|.|Celery worker: $name|uv run celery -A config worker -l info -Q $name --concurrency $conc"
        done
    else
        echo "worker|0|.|Celery worker (all queues)|uv run celery -A config worker -l info -Q $all_queues --concurrency $total"
    fi

    echo "beat|0|.|Celery beat scheduler|uv run celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler"
    echo "flower|5555|.|Celery inspector|uv run celery -A config flower --port=5555"
    echo "assets|0|.|Sass + esbuild watchers|npm run dev"
    # mobile is opt-in: mobile/node_modules is a separate install.
    echo "mobile|3002|mobile|Framework7 mobile dev server|npm run dev"
}

DEFAULT_SERVICES='web sse worker beat flower assets'

svc_field() {  # svc_field <line> <1..5>
    printf '%s' "$1" | cut -d'|' -f"$2"
}

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
state_file() { printf '%s/%s.state' "$RUN_DIR" "$1"; }
log_file()   { printf '%s/%s.log' "$LOG_DIR" "$1"; }
err_file()   { printf '%s/%s.err.log' "$LOG_DIR" "$1"; }

proc_start_time() {  # a stable-per-process string, used to detect PID reuse
    ps -o lstart= -p "$1" 2>/dev/null | tr -s ' ' | sed 's/^ *//;s/ *$//' || true
}

save_state() {  # save_state <name> <pid> <command>
    init_dirs
    {
        printf 'pid=%s\n' "$2"
        printf 'start=%s\n' "$(proc_start_time "$2")"
        printf 'command=%s\n' "$3"
    } > "$(state_file "$1")"
}

state_value() {  # state_value <name> <key>
    local f; f="$(state_file "$1")"
    [ -f "$f" ] || return 1
    sed -n "s/^$2=//p" "$f" | head -n1
}

remove_state() { rm -f "$(state_file "$1")"; }

tracked_services() {
    [ -d "$RUN_DIR" ] || return 0
    for f in "$RUN_DIR"/*.state; do
        [ -e "$f" ] || continue
        basename "$f" .state
    done
}

# Echoes the live PID for a service, or returns 1.
live_pid() {
    local name="$1" pid recorded current
    pid="$(state_value "$name" pid 2>/dev/null || true)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    recorded="$(state_value "$name" start 2>/dev/null || true)"
    if [ -n "$recorded" ]; then
        current="$(proc_start_time "$pid")"
        if [ -n "$current" ] && [ "$current" != "$recorded" ]; then return 1; fi
    fi
    printf '%s' "$pid"
}

is_running() { live_pid "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------
port_listening() {
    local port="$1"
    if command -v nc >/dev/null 2>&1; then
        nc -z 127.0.0.1 "$port" >/dev/null 2>&1 && return 0
        return 1
    fi
    # Bash's /dev/tcp, when nc is unavailable.
    (exec 3<>"/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1 && { exec 3<&- 3>&-; return 0; }
    return 1
}

wait_port() {  # wait_port <port> <timeout-seconds>
    local port="$1" timeout="${2:-30}" waited=0
    while [ "$waited" -lt "$((timeout * 2))" ]; do
        port_listening "$port" && return 0
        sleep 0.5
        waited=$((waited + 1))
    done
    return 1
}

port_owner() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN -F pc 2>/dev/null |
            awk '/^p/{p=substr($0,2)} /^c/{print substr($0,2)" (pid "p")"; exit}'
    fi
}

# ---------------------------------------------------------------------------
# Stopping
#
# Children first, then the parent: every service is `uv run <something>`, so the
# process actually serving requests is a child of the one we launched. Killing
# only the parent orphans the worker and leaves the port held.
# ---------------------------------------------------------------------------
kill_tree() {  # kill_tree <pid> <signal>
    local pid="$1" sig="$2" child
    for child in $(pgrep -P "$pid" 2>/dev/null || true); do
        kill_tree "$child" "$sig"
    done
    kill "-$sig" "$pid" 2>/dev/null || true
}

stop_service() {  # stop_service <name> <grace-seconds> <force 0|1>
    local name="$1" grace="${2:-8}" force="${3:-0}" pid waited

    if ! pid="$(live_pid "$name")"; then
        if [ -f "$(state_file "$name")" ]; then
            remove_state "$name"
            info "$name was not running (stale state cleaned up)"
        else
            info "$name is not running"
        fi
        return 0
    fi

    if [ "$force" != "1" ]; then
        kill_tree "$pid" TERM
        waited=0
        while [ "$waited" -lt "$((grace * 2))" ]; do
            if ! kill -0 "$pid" 2>/dev/null; then
                remove_state "$name"
                ok "$name stopped (pid $pid)"
                return 0
            fi
            sleep 0.5
            waited=$((waited + 1))
        done
        warn "$name did not exit within ${grace}s; sending KILL"
    fi

    kill_tree "$pid" KILL
    sleep 0.3
    if kill -0 "$pid" 2>/dev/null; then
        fail "$name (pid $pid) would not die; kill it by hand"
        return 1
    fi
    remove_state "$name"
    ok "$name stopped (pid $pid, forced)"
}

# ---------------------------------------------------------------------------
# Infrastructure via docker-compose (--docker). Only the backing services: the
# application containers would duplicate what these scripts run locally.
# ---------------------------------------------------------------------------
INFRA_SERVICES='postgres redis mailpit'

compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
        docker-compose "$@"
    else
        die 'Neither `docker compose` nor `docker-compose` is available.'
    fi
}
