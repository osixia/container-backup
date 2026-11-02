#!/bin/bash
# shellcheck disable=SC1091,SC2034,SC2154
# Sourced image profiles share stage and paused with this synthetic fixture.
# Run inside the test image, with only disposable tmpfs mounts and no network.
set -euo pipefail
umask 077
mkdir -p /scripts /source /published /work /age /credentials/example/generation
cp /usr/local/lib/backup/captures/sqlite.sh /scripts/capture.sh
age-keygen -o /tmp/identity 2>/dev/null
age-keygen -y /tmp/identity > /age/recipient
printf 'synthetic-only' > /credentials/example/generation/key
ln -s generation /credentials/example/..data
sqlite3 /source/db.sqlite3 'CREATE TABLE fixture (id INTEGER); INSERT INTO fixture VALUES (42);'
export SERVICE=portal SQLITE_FILE=/source/db.sqlite3 SQLITE_BACKUP_NAME=db.sqlite3 BACKUP_SECRETS=example RETENTION_DAYS=15
bash /usr/local/lib/backup/run.sh >/tmp/run.log
cd /published
sha256sum --check --status ./*.sha256
archive=$(find . -name '*.tar.gz.age' -print -quit)
age -d -i /tmp/identity -o /tmp/archive.tar.gz "$archive"
mkdir /tmp/restored
tar -xzf /tmp/archive.tar.gz -C /tmp/restored
[[ $(sqlite3 /tmp/restored/db.sqlite3 'PRAGMA integrity_check;') == ok ]]
[[ $(sqlite3 /tmp/restored/db.sqlite3 'SELECT COUNT(*) FROM fixture;') == 1 ]]
cmp /credentials/example/generation/key /tmp/restored/secrets/example/key
[[ ! -e /published/.backup-lock ]]
[[ -z $(find /work -mindepth 1 -print -quit) ]]
# A concurrent/stale lock must refuse a new run, preserving the existing lock.
mkdir /published/.backup-lock
if bash /usr/local/lib/backup/run.sh >/tmp/locked.log 2>&1; then exit 1; fi
[[ -d /published/.backup-lock ]]
rmdir /published/.backup-lock
# Normal capture failure must release the lock and clean private working files.
SQLITE_FILE=/source/missing
if bash /usr/local/lib/backup/run.sh >/tmp/failed.log 2>&1; then exit 1; fi
[[ ! -e /published/.backup-lock ]]
[[ -z $(find /work -mindepth 1 -print -quit) ]]

# Orchestration is replaced by a shell fixture; no real kubectl is invoked.
export APPLICATION=fixture STATE_CONFIGMAP=fixture-backups-state PGDATABASE=fixture PGPASSWORD_FILE=/tmp/fake
k() {
    printf '%s\n' "$*" >> /tmp/operations
    case "$*" in
        'get configmap '*) printf false ;;
        'get hpa '*) printf '{"items":[]}' ;;
        'get deployment fixture -o jsonpath='*) printf 2 ;;
        'get deployment fixture -o json'*) printf '{"spec":{"selector":{"matchLabels":{"app":"fixture"}}}}' ;;
        'get daemonset fixture -o json'*) printf '{"spec":{"template":{"spec":{"nodeSelector":{}}},"selector":{"matchLabels":{"app":"fixture"}}}}' ;;
    esac
}
wait_for_application_stop() { [[ $1 == app=fixture ]]; }
write_pgpass() { :; }
run_capture_command() { :; }
stage=/tmp/restored
source /usr/local/lib/backup/captures/nextcloud.sh
prepare_backup
resume_application
[[ $paused == false ]]
grep -q 'scale deployment fixture --replicas=0' /tmp/operations
grep -q 'scale deployment fixture --replicas=2' /tmp/operations
source /usr/local/lib/backup/captures/mail.sh
prepare_backup
resume_application
[[ $paused == false ]]
grep -q 'patch daemonset fixture' /tmp/operations
grep -q 'backup.osixia.net/paused' /tmp/operations
printf 'Synthetic runtime checks passed.\n'
