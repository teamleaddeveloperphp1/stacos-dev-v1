#!/usr/bin/env bash
# Starts the whole STACOS development stack in the background (Linux/macOS).
#
# The counterpart of start.ps1. Django, the SSE server, Celery worker, Celery
# beat, Flower and the asset watchers, each detached with its output in
# .run/logs/<name>.log and its PID in .run/<name>.state so stop.sh can shut down
# exactly these processes.
#
# Usage:
#   ./start.sh                       default set
#   ./start.sh --docker --migrate    bring up postgres/redis/mailpit, migrate, run
#   ./start.sh --only web,worker
#   ./start.sh --skip flower,sse
#   ./start.sh --split-workers       one worker per queue, as in production
#   ./start.sh --status
#
# See ./start.sh --help.

set -euo pipefail
# shellcheck source=scripts/_stack.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/_stack.sh"

ONLY=''; SKIP=''; WITH_MOBILE=0; SPLIT=0
DO_MIGRATE=0; DO_BUILD=0; DO_RESTART=0; DO_STATUS=0; USE_DOCKER=0

usage() {
    cat <<'EOF'
STACOS dev stack

  ./start.sh [options]

  --only a,b          Start only these services
  --skip a,b          Start the default set minus these
  --with-mobile       Also start the mobile dev server on :3002
  --split-workers     One Celery worker per queue (production topology)
  --migrate           Run migrations + sync_system_roles first
  --build             Build CSS/JS once before starting
  --docker            Start postgres/redis/mailpit via docker-compose first
  --restart           Replace anything already running
  --status            Report what is running, then exit
  -h, --help          This text

  Services: web sse worker beat flower assets mobile
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --only)          ONLY="${2:?--only needs a value}"; shift 2 ;;
        --only=*)        ONLY="${1#*=}"; shift ;;
        --skip)          SKIP="${2:?--skip needs a value}"; shift 2 ;;
        --skip=*)        SKIP="${1#*=}"; shift ;;
        --with-mobile)   WITH_MOBILE=1; shift ;;
        --split-workers) SPLIT=1; shift ;;
        --migrate)       DO_MIGRATE=1; shift ;;
        --build)         DO_BUILD=1; shift ;;
        --docker)        USE_DOCKER=1; shift ;;
        --restart)       DO_RESTART=1; shift ;;
        --status)        DO_STATUS=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               die "Unknown option: $1  (try --help)" ;;
    esac
done

cd "$STACK_ROOT"
SERVICES="$(stack_services "$SPLIT")"

# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------
if [ "$DO_STATUS" = "1" ]; then
    step 'STACOS stack status'
    found=0
    for name in $(tracked_services); do
        found=1
        if pid="$(live_pid "$name")"; then
            ok "$name  pid $pid  log $(log_file "$name")"
        else
            warn "$name  not running (stale state; run ./stop.sh to tidy up)"
        fi
    done
    if [ "$found" = "0" ]; then info 'Nothing is running (no state in .run/).'; fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
selected=''
if [ -n "$ONLY" ]; then
    for name in $(printf '%s' "$ONLY" | tr ',' ' '); do
        line="$(printf '%s\n' "$SERVICES" | grep "^$name|" || true)"
        [ -n "$line" ] || die "Unknown service '$name'. Known: $(printf '%s\n' "$SERVICES" | cut -d'|' -f1 | tr '\n' ' ')"
        selected="${selected}${line}"$'\n'
    done
else
    wanted="$DEFAULT_SERVICES"
    if [ "$WITH_MOBILE" = "1" ]; then wanted="$wanted mobile"; fi
    for name in $wanted; do
        case " $(printf '%s' "$SKIP" | tr ',' ' ') " in *" $name "*) continue ;; esac
        if [ "$name" = "worker" ] && [ "$SPLIT" = "1" ]; then
            selected="${selected}$(printf '%s\n' "$SERVICES" | grep '^worker-')"$'\n'
            continue
        fi
        line="$(printf '%s\n' "$SERVICES" | grep "^$name|" || true)"
        [ -n "$line" ] && selected="${selected}${line}"$'\n'
    done
fi
selected="$(printf '%s' "$selected" | grep -v '^$' || true)"
[ -n "$selected" ] || die 'No services selected.'

init_dirs
load_dotenv

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
step 'Prerequisites'

command -v uv >/dev/null 2>&1 || die 'uv is not on PATH. See https://docs.astral.sh/uv/'

if printf '%s\n' "$selected" | grep -q '|npm run dev$'; then
    if ! command -v npm >/dev/null 2>&1; then
        warn 'npm is not on PATH; skipping asset/mobile watchers'
        selected="$(printf '%s\n' "$selected" | grep -v '|npm run dev$' || true)"
    fi
fi
if printf '%s\n' "$selected" | grep -q '^assets|' && [ ! -d node_modules ]; then
    warn 'node_modules is missing; run `npm install`. Skipping assets.'
    selected="$(printf '%s\n' "$selected" | grep -v '^assets|' || true)"
fi
if printf '%s\n' "$selected" | grep -q '^mobile|' && [ ! -d mobile/node_modules ]; then
    warn 'mobile/node_modules is missing; run `npm install` in mobile/. Skipping mobile.'
    selected="$(printf '%s\n' "$selected" | grep -v '^mobile|' || true)"
fi

if [ "$USE_DOCKER" = "1" ]; then
    step 'Infrastructure (docker-compose)'
    # Backing services only. The application containers would duplicate what
    # this script runs on the host.
    # shellcheck disable=SC2086
    compose up -d $INFRA_SERVICES
    ok "started: $INFRA_SERVICES"
else
    port_listening 5432 || warn 'Nothing is listening on 5432 (PostgreSQL). Try --docker.'
    port_listening 6379 || warn 'Nothing is listening on 6379 (Redis). Try --docker.'
fi

# One warm-up, so N concurrent `uv run` invocations do not race to build the
# environment — and so a broken venv fails here, loudly, not in a log file.
info 'Preparing the Python environment (uv run)...'
uv run python -c "pass" || die 'uv run failed. Try `uv sync`.'
ok 'Python environment ready'

# ---------------------------------------------------------------------------
# Optional one-off steps
# ---------------------------------------------------------------------------
if [ "$DO_MIGRATE" = "1" ]; then
    step 'Migrations'
    [ -n "${DATABASE_MIGRATE_URL:-}" ] || die 'DATABASE_MIGRATE_URL is not set in .env'
    # Migrations run as the schema owner, never as the runtime role. Subshell, so
    # the services started below still connect as stacos_app.
    (
        export DATABASE_URL="$DATABASE_MIGRATE_URL"
        uv run python manage.py migrate
        uv run python manage.py sync_system_roles
    )
    ok 'Database is up to date'
fi

if [ "$DO_BUILD" = "1" ]; then
    step 'Assets'
    npm run build
    ok 'CSS and JS built'
fi

# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
step 'Starting services'

started=''
while IFS= read -r line; do
    [ -n "$line" ] || continue
    name="$(svc_field "$line" 1)"
    port="$(svc_field "$line" 2)"
    cwd="$(svc_field "$line" 3)"
    desc="$(svc_field "$line" 4)"
    cmd="$(svc_field "$line" 5)"

    if is_running "$name"; then
        if [ "$DO_RESTART" = "1" ]; then
            stop_service "$name" 8 0 || true
        else
            info "$name already running (pid $(live_pid "$name")); use --restart to replace it"
            continue
        fi
    else
        remove_state "$name"
    fi

    if [ "$port" != "0" ] && port_listening "$port"; then
        warn "$name: port $port is already in use; not starting"
        continue
    fi

    log="$(log_file "$name")"; err="$(err_file "$name")"
    rm -f "$log" "$err"
    launch_pid_file="$RUN_DIR/.launch.$name.pid"
    rm -f "$launch_pid_file"

    # The launcher reports its own $$ and then exec's, so the PID we record is
    # the service itself. Reading `$!` instead would be wrong whenever setsid
    # forks, which depends on whether job control happens to be enabled.
    (
        cd "$STACK_ROOT/$cwd"
        # shellcheck disable=SC2086
        if command -v setsid >/dev/null 2>&1; then
            # A new session: closing this terminal does not take the stack down.
            setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' _ "$launch_pid_file" $cmd \
                >"$log" 2>"$err" </dev/null &
        else
            nohup bash -c 'echo $$ > "$1"; shift; exec "$@"' _ "$launch_pid_file" $cmd \
                >"$log" 2>"$err" </dev/null &
        fi
    )

    pid=''; tries=0
    while [ "$tries" -lt 40 ]; do
        if [ -s "$launch_pid_file" ]; then pid="$(cat "$launch_pid_file")"; break; fi
        sleep 0.1
        tries=$((tries + 1))
    done
    rm -f "$launch_pid_file"
    if [ -z "$pid" ]; then
        fail "$name did not report a pid. See $err"
        continue
    fi

    sleep 0.5
    if ! kill -0 "$pid" 2>/dev/null; then
        fail "$name exited immediately. See $err"
        continue
    fi

    save_state "$name" "$pid" "$cmd"
    printf '  %s[ok]%s   %-16s pid %-7s %s\n' "$C_GREEN" "$C_RESET" "$name" "$pid" "$desc"
    started="${started}${name}|${port}"$'\n'
done <<< "$selected"

# ---------------------------------------------------------------------------
# Wait for the ports that matter, so "started" means "actually accepting".
# ---------------------------------------------------------------------------
while IFS= read -r line; do
    [ -n "$line" ] || continue
    name="${line%%|*}"; port="${line##*|}"
    if [ "$port" = "0" ]; then continue; fi
    if wait_port "$port" 40; then
        ok "$name is listening on :$port"
    else
        warn "$name is not listening on :$port yet. See $(err_file "$name")"
    fi
done <<< "$started"

step 'Up'
cat <<EOF
  Marketing        http://localhost:8000/
  Web app          http://localhost:8000/app/
  API schema       http://localhost:8000/api/v1/schema/swagger-ui/
  SSE (ASGI)       http://localhost:8001/
  Flower           http://localhost:5555/
  Mailpit          http://localhost:8025/   (with --docker)
  Mobile           http://localhost:3002/   (with --with-mobile)

  Logs             .run/logs/
  Follow one       tail -f .run/logs/web.log
  Status           ./start.sh --status
  Stop everything  ./stop.sh
EOF
