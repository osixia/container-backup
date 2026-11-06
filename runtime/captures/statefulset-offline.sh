#!/bin/bash
# Shared pause/recovery protocol for a single StatefulSet and its local data.
: "${APPLICATION:?StatefulSet requis}"
: "${STATE_CONFIGMAP:?ConfigMap de reprise requise}"
paused=false
replicas=

pause_statefulset() {
    local selector
    [[ "$(k get configmap "$STATE_CONFIGMAP" -o jsonpath='{.data.paused}')" == false ]]
    [[ "$(k get hpa -o json | jq --arg app "$APPLICATION" '[.items[] | select(.spec.scaleTargetRef.name == $app and .spec.scaleTargetRef.kind == "StatefulSet")] | length')" == 0 ]]
    replicas=$(k get statefulset "$APPLICATION" -o jsonpath='{.spec.replicas}')
    [[ "$replicas" =~ ^[1-9][0-9]*$ ]]
    selector=$(k get statefulset "$APPLICATION" -o json | jq -r '.spec.selector.matchLabels | to_entries | map(.key + "=" + .value) | join(",")')
    [[ -n "$selector" ]]
    k patch configmap "$STATE_CONFIGMAP" --type=merge \
        -p "{\"data\":{\"paused\":\"true\",\"replicas\":\"$replicas\"}}"
    paused=true
    k scale statefulset "$APPLICATION" --replicas=0
    wait_for_application_stop "$selector"
}

resume_statefulset() {
    [[ "$paused" == true ]] || return 0
    k scale statefulset "$APPLICATION" --replicas="$replicas" || return 1
    k patch configmap "$STATE_CONFIGMAP" --type=merge -p '{"data":{"paused":"false"}}' || return 1
    paused=false
}

prepare_backup() {
    [[ -d /source/data ]]
    pause_statefulset
}

archive_backup() {
    tar --acls --xattrs --numeric-owner -czf - \
        -C /source data -C "${stage:?}" .
}

resume_application() {
    resume_statefulset
}
