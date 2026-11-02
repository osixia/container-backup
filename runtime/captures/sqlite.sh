#!/bin/bash

# Authelia et Roundcube utilisent la même copie cohérente SQLite.
prepare_backup() {
    [[ -f "$SQLITE_FILE" ]]
    run_capture_command sqlite3 -readonly "$SQLITE_FILE" ".backup '${stage:?}/$SQLITE_BACKUP_NAME'"
    stat --printf='uid=%u\ngid=%g\nmode=%a\n' "$SQLITE_FILE" > "${stage:?}/database-metadata.txt"

    if [[ "$(sqlite3 -readonly "${stage:?}/$SQLITE_BACKUP_NAME" 'PRAGMA integrity_check;')" != ok ]]; then
        echo 'La copie SQLite est invalide.' >&2
        return 1
    fi
}
