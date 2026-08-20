#!/bin/sh
# Install the bounded Codex subagent-catalog maintenance LaunchAgent.

set -eu
umask 077

LABEL="com.ggen5.coding-intelligence.codex-catalog-maintenance"
SCRIPT_DIR="$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd)"
SOURCE_PROGRAM="${SCRIPT_DIR}/../ops/codex_catalog_maintenance.py"
SOURCE_PLIST="${SCRIPT_DIR}/${LABEL}.plist"
DESTINATION_DIR="${HOME}/.local/libexec/coding-intelligence"
DESTINATION_PROGRAM="${DESTINATION_DIR}/codex-catalog-maintenance.py"
DESTINATION_PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(/usr/bin/id -u)"
SERVICE="${DOMAIN}/${LABEL}"

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

if [ "$(/usr/bin/id -u)" -eq 0 ]; then
    printf '%s\n' 'Refusing root installation; this is a per-user LaunchAgent.' >&2
    exit 2
fi
if [ ! -f "${SOURCE_PROGRAM}" ] || [ ! -f "${SOURCE_PLIST}" ]; then
    printf '%s\n' 'Missing catalog-maintenance source artifacts.' >&2
    exit 2
fi
if /usr/bin/pgrep -U "$(/usr/bin/id -u)" -f '^(codex|.*/codex) archive [0-9a-f-]+$' >/dev/null 2>&1 \
    || /usr/bin/pgrep -U "$(/usr/bin/id -u)" -f '^xargs .*codex archive' >/dev/null 2>&1; then
    printf '%s\n' 'Refusing installation while another supported codex archive command is active.' >&2
    exit 3
fi

/usr/bin/plutil -lint "${SOURCE_PLIST}" >/dev/null
/usr/bin/python3 -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "${SOURCE_PROGRAM}"

temporary="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/coding-intelligence-catalog-maintenance.XXXXXX")"
had_program=false
had_plist=false
was_loaded=false
cleanup() {
    /bin/rm -f "${temporary}/program" "${temporary}/launchagent.plist"
    /bin/rmdir "${temporary}" 2>/dev/null || true
}
trap 'cleanup' EXIT
trap 'cleanup; exit 130' HUP INT TERM

if [ -f "${DESTINATION_PROGRAM}" ]; then
    /bin/cp -p "${DESTINATION_PROGRAM}" "${temporary}/program"
    had_program=true
fi
if [ -f "${DESTINATION_PLIST}" ]; then
    /bin/cp -p "${DESTINATION_PLIST}" "${temporary}/launchagent.plist"
    had_plist=true
fi
if /bin/launchctl print "${SERVICE}" >/dev/null 2>&1; then
    was_loaded=true
    /bin/launchctl bootout "${SERVICE}"
    wait_until_unloaded
fi

/bin/mkdir -p "${DESTINATION_DIR}" "${HOME}/Library/LaunchAgents"
/usr/bin/install -m 0755 "${SOURCE_PROGRAM}" "${DESTINATION_PROGRAM}"
/usr/bin/install -m 0644 "${SOURCE_PLIST}" "${DESTINATION_PLIST}"
/usr/bin/plutil -lint "${DESTINATION_PLIST}" >/dev/null

install_failed=false
if ! "${DESTINATION_PROGRAM}" --self-check >/dev/null 2>&1; then
    install_failed=true
elif ! /bin/launchctl enable "${SERVICE}"; then
    install_failed=true
elif ! /bin/launchctl bootstrap "${DOMAIN}" "${DESTINATION_PLIST}"; then
    install_failed=true
fi

if [ "${install_failed}" = true ]; then
    /bin/launchctl bootout "${SERVICE}" >/dev/null 2>&1 || true
    wait_until_unloaded || true
    if [ "${had_program}" = true ]; then
        /usr/bin/install -m 0755 "${temporary}/program" "${DESTINATION_PROGRAM}"
    else
        /bin/rm -f "${DESTINATION_PROGRAM}"
    fi
    if [ "${had_plist}" = true ]; then
        /usr/bin/install -m 0644 "${temporary}/launchagent.plist" "${DESTINATION_PLIST}"
        if [ "${was_loaded}" = true ]; then
            /bin/launchctl bootstrap "${DOMAIN}" "${DESTINATION_PLIST}" >/dev/null 2>&1 || true
        fi
    else
        /bin/rm -f "${DESTINATION_PLIST}"
    fi
    printf '%s\n' 'Catalog-maintenance installation failed and prior files were restored.' >&2
    exit 1
fi

printf '{"status":"installed","label":"%s","program":"%s","launchagent":"%s","interval_seconds":3600,"archive_limit":25}\n' \
    "${LABEL}" "${DESTINATION_PROGRAM}" "${DESTINATION_PLIST}"
