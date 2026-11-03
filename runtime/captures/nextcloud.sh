#!/bin/bash

: "${PGDATABASE:?Base requise}"
# shellcheck source=/dev/null
source /usr/local/lib/backup/deployment.sh

prepare_backup() {
    pause_deployment

    # L'arrêt du Deployment arrête aussi PHP et le cron sidecar.
    # La base et les fichiers sont alors capturés dans la même fenêtre sans écritures.
    : "${PGPASSWORD_FILE:?Chemin du fichier de mot de passe requis}"
    export PGPASSWORD_FILE
    write_pgpass
    run_capture_command pg_dump --no-password --format=custom --lock-wait-timeout=60s \
        --file="${stage:?}/database.dump" "$PGDATABASE"
}

archive_backup() {
    tar --acls --xattrs --numeric-owner -czf - \
        -C /source data -C "${stage:?}" .
}

resume_application() {
    resume_deployment
}
