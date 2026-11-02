#!/bin/bash

: "${APPLICATION:?DaemonSet requis}"
: "${STATE_CONFIGMAP:?ConfigMap de reprise requise}"

paused=false

prepare_backup() {
    local selector pause_id

    [[ "$(k get configmap "$STATE_CONFIGMAP" -o jsonpath='{.data.paused}')" == false ]]
    [[ "$(k get daemonset "$APPLICATION" -o json | jq '.spec.template.spec.nodeSelector["backup.osixia.net/paused"] == null')" == true ]]
    selector=$(k get daemonset "$APPLICATION" -o json | jq -r '.spec.selector.matchLabels | to_entries | map(.key + "=" + .value) | join(",")')
    pause_id=$(cat /proc/sys/kernel/random/uuid)

    # Écrire l'état avant l'arrêt pour permettre une reprise après un SIGKILL.
    k patch configmap "$STATE_CONFIGMAP" --type=merge -p '{"data":{"paused":"true"}}'
    paused=true

    # Garder le sélecteur de la VIP et ajouter seulement un sélecteur temporaire.
    k patch daemonset "$APPLICATION" --type=merge \
        -p "{\"spec\":{\"template\":{\"spec\":{\"nodeSelector\":{\"backup.osixia.net/paused\":\"$pause_id\"}}}}}"
    wait_for_application_stop "$selector"
}

archive_backup() {
    tar --acls --xattrs --numeric-owner -czf - \
        -C /source data state config -C "${stage:?}" .
}

resume_application() {
    [[ "$paused" == true ]] || return 0

    # Idempotent si la demande de reprise a abouti mais pas sa confirmation.
    local selector
    selector=$(k get daemonset "$APPLICATION" -o json | jq '.spec.template.spec.nodeSelector["backup.osixia.net/paused"] != null') || return 1
    if [[ "$selector" == true ]]; then
        k patch daemonset "$APPLICATION" --type=json \
            -p '[{"op":"remove","path":"/spec/template/spec/nodeSelector/backup.osixia.net~1paused"}]' || return 1
    fi
    k patch configmap "$STATE_CONFIGMAP" --type=merge -p '{"data":{"paused":"false"}}' || return 1
    paused=false
}
