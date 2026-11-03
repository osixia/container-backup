#!/bin/bash
# Shared pause/recovery protocol for a single Deployment and its local data.
: "${APPLICATION:?Deployment requis}"
: "${STATE_CONFIGMAP:?ConfigMap de reprise requise}"
paused=false
replicas=

pause_deployment() {
    local selector
    [[ "$(k get configmap "$STATE_CONFIGMAP" -o jsonpath='{.data.paused}')" == false ]]
    [[ "$(k get hpa -o json | jq --arg app "$APPLICATION" '[.items[] | select(.spec.scaleTargetRef.name == $app and .spec.scaleTargetRef.kind == "Deployment")] | length')" == 0 ]]
    replicas=$(k get deployment "$APPLICATION" -o jsonpath='{.spec.replicas}')
    [[ "$replicas" =~ ^[1-9][0-9]*$ ]]
    selector=$(k get deployment "$APPLICATION" -o json | jq -r '.spec.selector.matchLabels | to_entries | map(.key + "=" + .value) | join(",")')
    [[ -n "$selector" ]]
    k patch configmap "$STATE_CONFIGMAP" --type=merge \
        -p "{\"data\":{\"paused\":\"true\",\"replicas\":\"$replicas\"}}"
    paused=true
    k scale deployment "$APPLICATION" --replicas=0
    wait_for_application_stop "$selector"
}

resume_deployment() {
    [[ "$paused" == true ]] || return 0
    k scale deployment "$APPLICATION" --replicas="$replicas" || return 1
    k patch configmap "$STATE_CONFIGMAP" --type=merge -p '{"data":{"paused":"false"}}' || return 1
    paused=false
}
