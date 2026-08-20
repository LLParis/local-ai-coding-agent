#!/bin/sh
# Idempotently install the Codex SSH-host primary LaunchAgent for this Mac edge.

set -eu
umask 077

LABEL="com.ggen5.coding-intelligence.codex-ssh-host-primary"
SCRIPT_DIR="$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd)"
SOURCE_WRAPPER="${SCRIPT_DIR}/codex-ssh-host-primary.sh"
SOURCE_PLIST="${SCRIPT_DIR}/${LABEL}.plist"
SOURCE_ZSHENV="${SCRIPT_DIR}/zshenv.coding-intelligence"
DESTINATION_DIR="${HOME}/.local/libexec/coding-intelligence"
DESTINATION_WRAPPER="${DESTINATION_DIR}/codex-ssh-host-primary.sh"
DESTINATION_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DESTINATION_ZSHENV="${HOME}/.zshenv"
DOMAIN="gui/$(/usr/bin/id -u)"
SERVICE="${DOMAIN}/${LABEL}"
STATE_FILE="${HOME}/.codex/coding-intelligence/ssh-host-primary/state.json"
CODEX_BIN="${HOME}/.local/bin/codex"
MANAGED_CODEX="${HOME}/.codex/packages/standalone/current/codex"
CONTROL_SOCKET="${HOME}/.codex/app-server-control/app-server-control.sock"
APP_PID_FILE="${HOME}/.codex/app-server-daemon/app-server.pid"
UPDATER_PID_FILE="${HOME}/.codex/app-server-daemon/app-server-updater.pid"

wait_until_unloaded() {
    attempts=0
    while [ "${attempts}" -lt 15 ]; do
        if ! /bin/launchctl print "${SERVICE}" >/dev/null 2>&1; then
            return 0
        fi
        /bin/sleep 1
        attempts=$((attempts + 1))
    done
    return 1
}

terminate_exact_process() {
    pid="$1"
    shift
    actual_uid="$(/bin/ps -p "${pid}" -o uid= 2>/dev/null | /usr/bin/xargs)"
    actual_start="$(/bin/ps -p "${pid}" -o lstart= 2>/dev/null | /usr/bin/xargs)"
    actual_command="$(/bin/ps -p "${pid}" -o command= 2>/dev/null || true)"
    [ "${actual_uid}" = "$(/usr/bin/id -u)" ] || return 1
    allowed=false
    for expected_command in "$@"; do
        if [ "${actual_command}" = "${expected_command}" ]; then
            allowed=true
            break
        fi
    done
    [ "${allowed}" = true ] || return 1

    /bin/kill -TERM "${pid}"
    attempts=0
    while /bin/kill -0 "${pid}" 2>/dev/null && [ "${attempts}" -lt 10 ]; do
        /bin/sleep 1
        attempts=$((attempts + 1))
    done
    if /bin/kill -0 "${pid}" 2>/dev/null; then
        current_uid="$(/bin/ps -p "${pid}" -o uid= 2>/dev/null | /usr/bin/xargs)"
        current_start="$(/bin/ps -p "${pid}" -o lstart= 2>/dev/null | /usr/bin/xargs)"
        current_command="$(/bin/ps -p "${pid}" -o command= 2>/dev/null || true)"
        [ "${current_uid}" = "$(/usr/bin/id -u)" ] || return 1
        [ "${current_start}" = "${actual_start}" ] || return 1
        [ "${current_command}" = "${actual_command}" ] || return 1
        /bin/kill -KILL "${pid}"
    fi
    return 0
}

reconcile_existing_app_servers() {
    ssh_command="codex -c features.code_mode_host=true app-server --listen unix://"
    installed_command="${CODEX_BIN} -c features.code_mode_host=true app-server --listen unix://"
    managed_command="${MANAGED_CODEX} app-server --remote-control --listen unix://"
    socket_owners="$(/usr/sbin/lsof -t "${CONTROL_SOCKET}" 2>/dev/null | /usr/bin/sort -nu || true)"
    for owner_pid in ${socket_owners}; do
        terminate_exact_process "${owner_pid}" "${ssh_command}" "${installed_command}" "${managed_command}" || return 1
    done

    if [ -f "${APP_PID_FILE}" ]; then
        managed_pid="$(/usr/bin/plutil -extract pid raw "${APP_PID_FILE}" 2>/dev/null || true)"
        case "${managed_pid}" in
            ''|*[!0-9]*) managed_pid="" ;;
        esac
        if [ -n "${managed_pid}" ] && /bin/kill -0 "${managed_pid}" 2>/dev/null; then
            terminate_exact_process "${managed_pid}" "${managed_command}" || return 1
        fi
        if [ -z "${managed_pid}" ] || ! /bin/kill -0 "${managed_pid}" 2>/dev/null; then
            /bin/rm -f "${APP_PID_FILE}"
        fi
    fi

    if [ -f "${UPDATER_PID_FILE}" ]; then
        updater_pid="$(/usr/bin/plutil -extract pid raw "${UPDATER_PID_FILE}" 2>/dev/null || true)"
        updater_command="${MANAGED_CODEX} app-server daemon pid-update-loop"
        case "${updater_pid}" in
            ''|*[!0-9]*) updater_pid="" ;;
        esac
        if [ -n "${updater_pid}" ] && /bin/kill -0 "${updater_pid}" 2>/dev/null; then
            terminate_exact_process "${updater_pid}" "${updater_command}" || return 1
        fi
        if [ -z "${updater_pid}" ] || ! /bin/kill -0 "${updater_pid}" 2>/dev/null; then
            /bin/rm -f "${UPDATER_PID_FILE}"
        fi
    fi
    return 0
}

if [ "$(/usr/bin/id -u)" -eq 0 ]; then
    printf '%s\n' 'Refusing root installation; this is a per-user LaunchAgent.' >&2
    exit 2
fi
if [ ! -x "${SOURCE_WRAPPER}" ] && [ ! -f "${SOURCE_WRAPPER}" ]; then
    printf 'Missing wrapper: %s\n' "${SOURCE_WRAPPER}" >&2
    exit 2
fi
if [ ! -f "${SOURCE_PLIST}" ]; then
    printf 'Missing LaunchAgent: %s\n' "${SOURCE_PLIST}" >&2
    exit 2
fi
if [ ! -f "${SOURCE_ZSHENV}" ]; then
    printf 'Missing noninteractive SSH environment: %s\n' "${SOURCE_ZSHENV}" >&2
    exit 2
fi
if [ ! -x "${CODEX_BIN}" ]; then
    printf 'Missing official standalone Codex: %s\n' "${CODEX_BIN}" >&2
    exit 2
fi

/usr/bin/plutil -lint "${SOURCE_PLIST}" >/dev/null
"${CODEX_BIN}" --version >/dev/null

temporary="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/coding-intelligence-codex-primary.XXXXXX")"
had_wrapper=false
had_plist=false
had_zshenv=false
was_loaded=false
cleanup() {
    /bin/rm -f "${temporary}/wrapper" "${temporary}/launchagent.plist" "${temporary}/zshenv"
    /bin/rmdir "${temporary}" 2>/dev/null || true
}
trap 'cleanup' EXIT
trap 'cleanup; exit 130' HUP INT TERM

if [ -f "${DESTINATION_WRAPPER}" ]; then
    /bin/cp -p "${DESTINATION_WRAPPER}" "${temporary}/wrapper"
    had_wrapper=true
fi
if [ -f "${DESTINATION_PLIST}" ]; then
    /bin/cp -p "${DESTINATION_PLIST}" "${temporary}/launchagent.plist"
    had_plist=true
fi
if [ -f "${DESTINATION_ZSHENV}" ]; then
    /bin/cp -p "${DESTINATION_ZSHENV}" "${temporary}/zshenv"
    had_zshenv=true
fi
if /bin/launchctl print "${SERVICE}" >/dev/null 2>&1; then
    was_loaded=true
    /bin/launchctl bootout "${SERVICE}"
    wait_until_unloaded
fi

if ! reconcile_existing_app_servers; then
    if [ "${was_loaded}" = true ]; then
        /bin/launchctl bootstrap "${DOMAIN}" "${DESTINATION_PLIST}" >/dev/null 2>&1 || true
    fi
    printf '%s\n' 'Refusing to replace an app-server whose exact owner could not be verified.' >&2
    exit 1
fi

/bin/mkdir -p "${DESTINATION_DIR}" "${HOME}/Library/LaunchAgents"
/usr/bin/install -m 0755 "${SOURCE_WRAPPER}" "${DESTINATION_WRAPPER}"
/usr/bin/install -m 0644 "${SOURCE_PLIST}" "${DESTINATION_PLIST}"
/usr/bin/install -m 0644 "${SOURCE_ZSHENV}" "${DESTINATION_ZSHENV}"
/usr/bin/plutil -lint "${DESTINATION_PLIST}" >/dev/null

install_failed=false
if ! /bin/launchctl enable "${SERVICE}"; then
    install_failed=true
elif ! /bin/launchctl bootstrap "${DOMAIN}" "${DESTINATION_PLIST}"; then
    install_failed=true
fi

if [ "${install_failed}" = false ]; then
    attempts=0
    while [ "${attempts}" -lt 35 ]; do
        if "${DESTINATION_WRAPPER}" --check >/dev/null 2>&1; then
            break
        fi
        /bin/sleep 1
        attempts=$((attempts + 1))
    done
    if ! "${DESTINATION_WRAPPER}" --check >/dev/null 2>&1; then
        install_failed=true
    fi
fi

if [ "${install_failed}" = true ]; then
    /bin/launchctl bootout "${SERVICE}" >/dev/null 2>&1 || true
    wait_until_unloaded || true
    if [ "${had_wrapper}" = true ]; then
        /usr/bin/install -m 0755 "${temporary}/wrapper" "${DESTINATION_WRAPPER}"
    else
        /bin/rm -f "${DESTINATION_WRAPPER}"
    fi
    if [ "${had_plist}" = true ]; then
        /usr/bin/install -m 0644 "${temporary}/launchagent.plist" "${DESTINATION_PLIST}"
        if [ "${was_loaded}" = true ]; then
            /bin/launchctl bootstrap "${DOMAIN}" "${DESTINATION_PLIST}" >/dev/null 2>&1 || true
        fi
    else
        /bin/rm -f "${DESTINATION_PLIST}"
    fi
    if [ "${had_zshenv}" = true ]; then
        /usr/bin/install -m 0644 "${temporary}/zshenv" "${DESTINATION_ZSHENV}"
    else
        /bin/rm -f "${DESTINATION_ZSHENV}"
    fi
    printf '%s\n' 'Codex SSH-host primary installation failed and prior files were restored.' >&2
    exit 1
fi

supervisor_pid="$(/bin/launchctl print "${SERVICE}" | /usr/bin/awk '/^[[:space:]]*pid = / { print $3; exit }')"
printf '{"status":"installed","label":"%s","supervisor_pid":%s,"wrapper":"%s","launchagent":"%s","zshenv":"%s"}\n' \
    "${LABEL}" "${supervisor_pid}" "${DESTINATION_WRAPPER}" "${DESTINATION_PLIST}" "${DESTINATION_ZSHENV}"
