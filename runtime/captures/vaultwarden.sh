#!/bin/bash
# Même contrôle de Deployment que Roundcube ; /data complet, figé pendant l'archive.
: "${SQLITE_FILE:?Base SQLite requise}"
# shellcheck source=/dev/null
source /usr/local/lib/backup/deployment.sh

prepare_backup() {
    local private suffix
    pause_deployment
    [[ -s "$SQLITE_FILE" && -s /source/data/rsa_key.pem ]]
    private="${workdir:?}/sqlite-source"
    mkdir -m 0700 -- "$private"
    # Contrôler WAL/journal depuis une copie modifiable, jamais sur le PVC en lecture seule.
    run_capture_command cp -- "$SQLITE_FILE" "$private/database.sqlite3"
    chmod 0600 "$private/database.sqlite3"
    for suffix in -wal -journal; do
        if [[ -f "$SQLITE_FILE$suffix" ]]; then
            run_capture_command cp -- "$SQLITE_FILE$suffix" "$private/database.sqlite3$suffix"
            chmod 0600 "$private/database.sqlite3$suffix"
        fi
    done
    if [[ "$(sqlite3 "$private/database.sqlite3" 'PRAGMA integrity_check;')" != ok ]]; then
        echo 'La copie SQLite Vaultwarden est invalide.' >&2
        return 1
    fi
}

archive_backup() {
    # L'application reste arrêtée : base, WAL, fichiers et clés forment le même lot.
    # Le PVC est lu directement, sans copie complète dans l'emptyDir privé.
    tar --acls --xattrs --numeric-owner -czf - \
        -C /source data -C "${stage:?}" .
}

resume_application() {
    resume_deployment
}
