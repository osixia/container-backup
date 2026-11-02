#!/bin/bash

: "${APPLICATION:?Deployment requis}"
: "${STATE_CONFIGMAP:?ConfigMap de reprise requise}"
: "${PGDATABASE:?Base requise}"

paused=false
replicas=

prepare_backup() {
    local selector

    [[ "$(k get configmap "$STATE_CONFIGMAP" -o jsonpath='{.data.paused}')" == false ]]
    [[ "$(k get hpa -o json | jq --arg app "$APPLICATION" '[.items[] | select(.spec.scaleTargetRef.name == $app and .spec.scaleTargetRef.kind == "Deployment")] | length')" == 0 ]]
    replicas=$(k get deployment "$APPLICATION" -o jsonpath='{.spec.replicas}')
    [[ "$replicas" =~ ^[1-9][0-9]*$ ]]
    selector=$(k get deployment "$APPLICATION" -o json | jq -r '.spec.selector.matchLabels | to_entries | map(.key + "=" + .value) | join(",")')

    k patch configmap "$STATE_CONFIGMAP" --type=merge \
        -p "{\"data\":{\"paused\":\"true\",\"replicas\":\"$replicas\"}}"
    paused=true
    k scale deployment "$APPLICATION" --replicas=0
    wait_for_application_stop "$selector"

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
    [[ "$paused" == true ]] || return 0

    k scale deployment "$APPLICATION" --replicas="$replicas" || return 1
    k patch configmap "$STATE_CONFIGMAP" --type=merge -p '{"data":{"paused":"false"}}' || return 1
    paused=false
}
