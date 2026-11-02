#!/bin/bash

# Un lot complet par service : capture, archive, age, publication, rétention.
# La clé privée ne doit jamais être montée dans ce job.
set +x
set -euo pipefail
umask 077

: "${SERVICE:?Service requis}"
: "${RETENTION_DAYS:=15}"
[[ "$SERVICE" =~ ^[a-z]+$ && "$RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]]

k() {
    command kubectl --namespace="$POD_NAMESPACE" --request-timeout=30s "$@"
}

wait_for_application_stop() {
    local selector=$1 attempt

    for ((attempt = 0; attempt < 150; attempt++)); do
        if [[ "$(k get pods -l "$selector" -o json | jq '.items | length')" == 0 ]]; then
            return 0
        fi
        sleep 2
    done

    echo 'Les pods applicatifs ne sont pas arrêtés après cinq minutes.' >&2
    return 1
}

# Attendre un enfant en arrière-plan permet à Bash de traiter SIGTERM aussitôt.
# Le nettoyage interrompt ce processus avant de demander la reprise applicative.
run_capture_command() {
    "$@" &
    worker_pid=$!
    wait "$worker_pid"
    worker_pid=
}

write_pgpass() {
    local password
    password=$(cat -- "$PGPASSWORD_FILE")
    [[ "$password" != *$'\n'* && "$password" != *$'\r'* ]]
    password=${password//\\/\\\\}
    password=${password//:/\\:}

    export PGPASSFILE="$workdir/pgpass"
    printf '%s:%s:*:%s:%s\n' "$PGHOST" "$PGPORT" "$PGUSER" "$password" > "$PGPASSFILE"
    chmod 0600 "$PGPASSFILE"
}

# Les services sans arrêt applicatif gardent cette fonction vide.
resume_application() {
    :
}

archive_backup() {
    tar --acls --xattrs --numeric-owner -czf - -C "$stage" .
}

# Chaque service fournit uniquement sa capture et, si nécessaire, sa reprise.
# shellcheck source=/dev/null
source /scripts/capture.sh

# Préparer seulement le volume de sauvegarde. Les nouveaux fichiers héritent du
# groupe du job (911 en production), également utilisé par l'export Nginx.
chgrp -- "$(id -g)" /published
chmod 2770 /published

# mkdir est atomique, y compris entre deux jobs manuels sur le volume partagé.
# Un verrou laissé par une interruption brutale nécessite une reprise opérateur.
lock=/published/.backup-lock
if ! mkdir -- "$lock"; then
    echo 'Sauvegarde déjà en cours ou verrou à récupérer : voir le guide de récupération du projet.' >&2
    exit 1
fi

workdir=
temporary=
checksum_temporary=
worker_pid=

cleanup() {
    local status=$?
    trap - EXIT INT TERM

    if [[ -n "$worker_pid" ]]; then
        kill "$worker_pid" 2>/dev/null || true
        wait "$worker_pid" 2>/dev/null || true
    fi

    if resume_application; then
        rmdir -- "$lock" || status=1
    else
        echo 'Reprise applicative échouée ; conserver le verrou et suivre le guide.' >&2
        status=1
    fi

    [[ -z "$temporary" ]] || rm -f -- "$temporary" "$checksum_temporary"
    [[ -z "$workdir" ]] || rm -rf -- "$workdir"
    exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

workdir=$(mktemp -d /work/backup.XXXXXX)
stage="$workdir/archive"
mkdir -- "$stage" "$stage/secrets"

# Épingler la génération de chaque Secret projeté, puis copier ses fichiers.
# Les fichiers en clair restent dans le volume de travail privé du job.
for name in ${BACKUP_SECRETS:-}; do
    directory=$(readlink -f -- "/credentials/$name/..data")
    mkdir -- "$stage/secrets/$name"
    cp -- "$directory"/* "$stage/secrets/$name/"
done

archive="$SERVICE-$(date -u +%Y%m%dT%H%M%SZ)-$(cat /proc/sys/kernel/random/uuid).tar.gz.age"
temporary="/published/$archive.tmp"
checksum_temporary="/published/$archive.sha256.tmp"

prepare_backup
archive_backup | age -R /age/recipient -o "$temporary" &
worker_pid=$!
wait "$worker_pid"
worker_pid=

# Nginx ne voit que le volume publié. Le marqueur est écrit en dernier.
chmod 0640 "$temporary"
mv -- "$temporary" "/published/$archive"
(
    cd /published
    sha256sum "$archive" > "$checksum_temporary"
)
chmod 0640 "$checksum_temporary"
mv -- "$checksum_temporary" "/published/$archive.sha256"

# Les PVC RWO peuvent rester attachés au job jusqu'à sa fin : demander la reprise
# sans attendre le rollout ici. L'opérateur contrôle ensuite la disponibilité.
resume_application

# Purger les archives complètes expirées de ce service.
while IFS= read -r -d '' expired; do
    rm -f -- "$expired.sha256" "$expired"
done < <(find /published -maxdepth 1 -type f -name "$SERVICE-*.tar.gz.age" \
    -mmin "+$((RETENTION_DAYS * 1440))" -print0)

echo "Sauvegarde $SERVICE publiée."
