#!/bin/bash

: "${SQLITE_FILE:?Base SQLite requise}"
: "${SQLITE_BACKUP_NAME:?Nom de sauvegarde requis}"
[[ "$SQLITE_BACKUP_NAME" =~ ^[A-Za-z0-9_.-]+$ && "$SQLITE_BACKUP_NAME" != . && "$SQLITE_BACKUP_NAME" != .. ]]
# shellcheck source=/dev/null
source /usr/local/lib/backup/deployment.sh

prepare_backup() {
    local private suffix
    [[ -f "$SQLITE_FILE" ]]
    private="${workdir:?}/sqlite-source"
    mkdir -m 0700 -- "$private"
    pause_deployment
    stat --printf='uid=%u\ngid=%g\nmode=%a\n' "$SQLITE_FILE" > "${stage:?}/database-metadata.txt"
    run_capture_command cp -- "$SQLITE_FILE" "$private/database.sqlite3"
    chmod 0600 "$private/database.sqlite3"
    # All application writers are stopped. Copy the WAL or rollback journal if
    # present; SQLite rebuilds its own shared memory in this writable private copy.
    for suffix in -wal -journal; do
        if [[ -f "$SQLITE_FILE$suffix" ]]; then
            run_capture_command cp -- "$SQLITE_FILE$suffix" "$private/database.sqlite3$suffix"
            chmod 0600 "$private/database.sqlite3$suffix"
        fi
    done
    resume_deployment
    run_capture_command sqlite3 "$private/database.sqlite3" ".backup '${stage:?}/$SQLITE_BACKUP_NAME'"
    if [[ "$(sqlite3 -readonly "${stage:?}/$SQLITE_BACKUP_NAME" 'PRAGMA integrity_check;')" != ok ]]; then
        echo 'La copie SQLite est invalide.' >&2
        return 1
    fi
}

resume_application() {
    resume_deployment
}
