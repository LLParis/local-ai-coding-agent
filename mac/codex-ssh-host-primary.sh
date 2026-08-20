#!/bin/sh
# LaunchAgent-owned supervisor for Codex's official SSH-host app-server command.

set -u
umask 077

PATH="/usr/bin:/bin:/usr/sbin:/sbin:${HOME}/.local/bin"
export PATH

LABEL="com.ggen5.coding-intelligence.codex-ssh-host-primary"
CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
CODEX_BIN="${CODEX_BIN:-${HOME}/.local/bin/codex}"
STATE_ROOT="${CODEX_HOME}/coding-intelligence/ssh-host-primary"
STATE_FILE="${STATE_ROOT}/state.json"
LOG_FILE="${STATE_ROOT}/supervisor.log"
CONTROL_SOCKET="${CODEX_HOME}/app-server-control/app-server-control.sock"
LOG_LIMIT_BYTES="${LOG_LIMIT_BYTES:-262144}"
START_TIMEOUT_SECONDS="${START_TIMEOUT_SECONDS:-20}"
USER_UID="$(/usr/bin/id -u)"
DOMAIN="gui/${USER_UID}"
SERVICE="${DOMAIN}/${LABEL}"
EXPECTED_COMMAND="${CODEX_BIN} -c features.code_mode_host=true app-server --listen unix://"
CHILD_PID=""

/bin/mkdir -p "${STATE_ROOT}"
/bin/chmod 700 "${STATE_ROOT}"

rotate_log() {
    [ -f "${LOG_FILE}" ] || return 0
    size="$(/usr/bin/stat -f '%z' "${LOG_FILE}" 2>/dev/null || printf '0')"
    case "${size}" in
        ''|*[!0-9]*) size=0 ;;
    esac
    if [ "${size}" -ge "${LOG_LIMIT_BYTES}" ]; then
        /bin/mv -f "${LOG_FILE}" "${LOG_FILE}.1"
    fi
}

log_event() {
    rotate_log
    printf '%s %s\n' "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" >> "${LOG_FILE}"
}

write_state() {
    status="$1"
    reason="$2"
    child_pid_json="null"
    [ -n "${CHILD_PID}" ] && child_pid_json="${CHILD_PID}"
    temporary="${STATE_FILE}.$$"
    printf '{"schema":"coding-intelligence.codex-ssh-host-primary/v1","status":"%s","checked_at":"%s","supervisor_pid":%s,"app_server_pid":%s,"reason":"%s"}\n' \
        "${status}" \
        "$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        "$$" \
        "${child_pid_json}" \
        "${reason}" > "${temporary}"
    /bin/chmod 600 "${temporary}"
    /bin/mv -f "${temporary}" "${STATE_FILE}"
}

matching_pids() {
    /bin/ps -axo pid=,uid=,command= | /usr/bin/awk -v wanted_uid="${USER_UID}" -v wanted_command="${EXPECTED_COMMAND}" '
        {
            pid = $1
            owner = $2
            sub(/^[[:space:]]*[0-9]+[[:space:]]+[0-9]+[[:space:]]+/, "", $0)
            if (owner == wanted_uid && $0 == wanted_command) print pid
        }
    '
}

socket_owner() {
    /usr/sbin/lsof -t "${CONTROL_SOCKET}" 2>/dev/null | /usr/bin/sort -nu
}

print_check() {
    if [ ! -f "${STATE_FILE}" ]; then
        printf '%s\n' '{"status":"unhealthy","reason":"state_missing"}'
        return 1
    fi
    supervisor_pid="$(/usr/bin/plutil -extract supervisor_pid raw "${STATE_FILE}" 2>/dev/null || true)"
    child_pid="$(/usr/bin/plutil -extract app_server_pid raw "${STATE_FILE}" 2>/dev/null || true)"
    state_status="$(/usr/bin/plutil -extract status raw "${STATE_FILE}" 2>/dev/null || true)"
    launchd_pid="$(/bin/launchctl print "${SERVICE}" 2>/dev/null | /usr/bin/awk '/^[[:space:]]*pid = / { print $3; exit }')"
    case "${supervisor_pid}:${child_pid}:${launchd_pid}" in
        *[!0-9:]*|:*|*:) reason="pid_state_invalid" ;;
        *) reason="" ;;
    esac
    if [ -n "${reason}" ] || [ "${state_status}" != "healthy" ] || [ "${supervisor_pid}" != "${launchd_pid}" ]; then
        printf '{"status":"unhealthy","reason":"%s"}\n' "${reason:-launchagent_state_mismatch}"
        return 1
    fi
    actual_ppid="$(/bin/ps -p "${child_pid}" -o ppid= 2>/dev/null | /usr/bin/xargs)"
    actual_uid="$(/bin/ps -p "${child_pid}" -o uid= 2>/dev/null | /usr/bin/xargs)"
    actual_command="$(/bin/ps -p "${child_pid}" -o command= 2>/dev/null || true)"
    matches="$(matching_pids)"
    match_count="$(printf '%s\n' "${matches}" | /usr/bin/awk 'NF { count += 1 } END { print count + 0 }')"
    owner="$(socket_owner)"
    if [ "${actual_ppid}" != "${supervisor_pid}" ] || [ "${actual_uid}" != "${USER_UID}" ] || [ "${actual_command}" != "${EXPECTED_COMMAND}" ]; then
        printf '%s\n' '{"status":"unhealthy","reason":"process_owner_mismatch"}'
        return 1
    fi
    if [ "${match_count}" -ne 1 ] || [ "${matches}" != "${child_pid}" ]; then
        printf '%s\n' '{"status":"unhealthy","reason":"duplicate_or_missing_app_server"}'
        return 1
    fi
    if [ "${owner}" != "${child_pid}" ]; then
        printf '%s\n' '{"status":"unhealthy","reason":"control_socket_owner_mismatch"}'
        return 1
    fi
    if /usr/sbin/lsof -nP -a -p "${child_pid}" -iTCP -sTCP:LISTEN 2>/dev/null | /usr/bin/grep -q .; then
        printf '%s\n' '{"status":"unhealthy","reason":"unexpected_tcp_listener"}'
        return 1
    fi
    printf '{"status":"healthy","supervisor_pid":%s,"app_server_pid":%s,"socket":"unix://","tcp_listener_count":0,"exact_owner_count":1}\n' \
        "${supervisor_pid}" "${child_pid}"
    return 0
}

if [ "${1:-}" = "--check" ]; then
    print_check
    exit $?
fi

shutdown() {
    if [ -n "${CHILD_PID}" ] && /bin/kill -0 "${CHILD_PID}" 2>/dev/null; then
        /bin/kill -TERM "${CHILD_PID}" 2>/dev/null || true
        wait "${CHILD_PID}" 2>/dev/null || true
    fi
    write_state "stopped" "launchagent_stopped"
    log_event "supervisor_stopped pid=$$ app_server_pid=${CHILD_PID:-none}"
    exit 0
}
trap 'shutdown' HUP INT TERM

log_event "supervisor_started pid=$$"
"${CODEX_BIN}" -c features.code_mode_host=true app-server --listen unix:// >/dev/null 2>&1 &
CHILD_PID=$!
write_state "starting" "waiting_for_unix_socket"

attempts=0
while [ "${attempts}" -lt "${START_TIMEOUT_SECONDS}" ]; do
    if ! /bin/kill -0 "${CHILD_PID}" 2>/dev/null; then
        wait "${CHILD_PID}" 2>/dev/null || true
        write_state "degraded" "app_server_exited_during_start"
        log_event "start_failed reason=child_exited app_server_pid=${CHILD_PID}"
        exit 1
    fi
    owner="$(socket_owner)"
    if [ "${owner}" = "${CHILD_PID}" ]; then
        write_state "healthy" "exact_owner_socket_only"
        log_event "healthy supervisor_pid=$$ app_server_pid=${CHILD_PID}"
        break
    fi
    /bin/sleep 1
    attempts=$((attempts + 1))
done

if [ "${attempts}" -ge "${START_TIMEOUT_SECONDS}" ]; then
    /bin/kill -TERM "${CHILD_PID}" 2>/dev/null || true
    wait "${CHILD_PID}" 2>/dev/null || true
    write_state "degraded" "unix_socket_start_timeout"
    log_event "start_failed reason=socket_timeout app_server_pid=${CHILD_PID}"
    exit 1
fi

child_exit=0
wait "${CHILD_PID}" || child_exit=$?
write_state "degraded" "app_server_exited"
log_event "app_server_exited pid=${CHILD_PID} exit=${child_exit}"
exit 1
