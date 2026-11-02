#!/bin/bash

# Le dump inclut toutes les bases et les rôles de cette instance.
prepare_backup() {
    : "${PGPASSWORD_FILE:?Chemin du fichier de mot de passe requis}"
    export PGPASSWORD_FILE
    write_pgpass
    run_capture_command pg_dumpall --no-password --file="${stage:?}/cluster.sql"
}
