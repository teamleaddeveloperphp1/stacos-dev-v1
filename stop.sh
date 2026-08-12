#!/usr/bin/env bash
# Stops the STACOS development stack started by start.sh (Linux/macOS).
#
# Reads .run/<name>.state, verifies each recorded PID still belongs to the
# process we started (PID *and* start time must match), then terminates it
# together with its children — every service is `uv run <something>`, so the
# process doing the work is a child of the one we launched.
#
# Usage:
#   ./stop.sh
#   ./stop.sh --only worker,beat
#   ./stop.sh --force --ports
#   ./stop.sh --docker            also stop postgres/redis/mailpit containers

set -euo pipefail
# shellcheck source=scripts/_stack.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/_stack.sh"

ONLY=''; FORCE=0; GRACE=8; CHECK_PORTS=0; STOP_DOCKER=0

usage() {
    cat <<'EOF'
Stop the STACOS dev stack

  ./stop.sh [options]

  --only a,b     Stop only these services
  --force        Skip the graceful TERM and kill immediately
  --grace N      Seconds to wait before forcing (default 8)
  --ports        Report anything still holding 8000/8001/5555/3002
  --docker       Also stop the docker-compose infrastructure services
  -h, --help     This text
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --only)     ONLY="${2:?--only needs a value}"; shift 2 ;;
        --only=*)   ONLY="${1#*=}"; shift ;;
        --force)    FORCE=1; shift ;;
        --grace)    GRACE="${2:?--grace needs a value}"; shift 2 ;;
        --grace=*)  GRACE="${1#*=}"; shift ;;
        --ports)    CHECK_PORTS=1; shift ;;
        --docker)   STOP_DOCKER=1; shift ;;
        -h|--help)  usage; exit 0 ;;
        *)          die "Unknown option: $1  (try --help)" ;;
    esac
done

cd "$STACK_ROOT"
step 'Stopping STACOS stack'

tracked="$(tracked_services | tr '\n' ' ')"

names=''
if [ -n "$ONLY" ]; then
    for name in $(printf '%s' "$ONLY" | tr ',' ' '); do
        case " $tracked " in
            *" $name "*) names="$names $name" ;;
            *)           info "$name is not tracked as running" ;;
        esac
    done
else
    names="$tracked"
fi

if [ -z "$(printf '%s' "$names" | tr -d ' ')" ]; then
    info 'Nothing to stop.'
else
    # Reverse order: producers before the things they depend on, so beat is not
    # queueing work into a broker whose workers have already gone.
    ordered=''
    for preferred in mobile assets flower beat; do
        case " $names " in *" $preferred "*) ordered="$ordered $preferred" ;; esac
    done
    for name in $names; do
        case " $ordered " in *" $name "*) ;; *) ordered="$ordered $name" ;; esac
    done

    for name in $ordered; do
        stop_service "$name" "$GRACE" "$FORCE" || true
    done
fi

# ---------------------------------------------------------------------------
# Leftovers
# ---------------------------------------------------------------------------
if [ "$CHECK_PORTS" = "1" ]; then
    step 'Port check'
    for port in 8000 8001 5555 3002; do
        if port_listening "$port"; then
            owner="$(port_owner "$port" || true)"
            if [ -n "$owner" ]; then
                warn "$port still held by $owner"
            else
                warn "$port still in use by an unidentified process"
            fi
        else
            ok "$port free"
        fi
    done
fi

if [ "$STOP_DOCKER" = "1" ]; then
    step 'Infrastructure (docker-compose)'
    # stop, not down: `down` would remove the volumes' containers and, with -v,
    # the development database itself.
    # shellcheck disable=SC2086
    compose stop $INFRA_SERVICES
    ok "stopped: $INFRA_SERVICES"
fi

printf '\n  %sLogs from the stopped run are still in .run/logs/%s\n' "$C_GRAY" "$C_RESET"
