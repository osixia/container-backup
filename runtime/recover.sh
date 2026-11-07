#!/bin/bash
# Runs in a separate Kubernetes Job, without application or backup volumes.
set -euo pipefail

: "${SERVICE:?Service required}"
: "${APPLICATION:?Workload required}"
: "${STATE_CONFIGMAP:?Recovery state required}"
: "${RECOVERY_KIND:?Workload kind required}"
: "${POD_NAMESPACE:?Namespace required}"
: "${KUBECONFIG:?Explicit kubeconfig required}"
[[ "$SERVICE" =~ ^[a-z]+$ ]]
case "$RECOVERY_KIND" in deployment|statefulset|daemonset) ;; *) exit 1 ;; esac
k() {
    command kubectl --kubeconfig="$KUBECONFIG" --namespace="$POD_NAMESPACE" --request-timeout=20s "$@"
}

state=$(k get configmap "$STATE_CONFIGMAP" -o json)
paused=$(jq -er '.data.paused' <<< "$state")
case "$paused" in
    false) exit 0 ;;
    true|recovering) ;;
    *) echo 'Invalid recovery state; operator intervention required.' >&2; exit 1 ;;
esac

# Failed/Succeeded are terminal. Pending, Running, Unknown, and missing phases
# block recovery, including pods still terminating. Never delete or force a pod.
pods=$(k get pods -l "app.kubernetes.io/name=backup-job,backup.osixia.net/service=$SERVICE" -o json)
if ! jq -e '(.items | type == "array") and all(.items[];
    (.status.phase == "Succeeded" or .status.phase == "Failed"))' <<< "$pods" >/dev/null; then
    echo 'Capture pods are not all terminal; leaving the application paused.'
    exit 0
fi

if [[ "$RECOVERY_KIND" != daemonset ]]; then
    replicas=$(jq -er '.data.replicas' <<< "$state")
    [[ "$replicas" =~ ^[1-9][0-9]*$ ]] || {
        echo 'Invalid saved replica count; operator intervention required.' >&2
        exit 1
    }
fi

# Compare-and-swap the snapshot, so completion of a capture or another operator
# action between reading state and listing pods aborts recovery. New captures
# already require paused=false. A failed recovery can retry from recovering.
version=$(jq -er '.metadata.resourceVersion | select(type == "string" and length > 0)' <<< "$state")
patch=$(jq -nc --arg version "$version" '[
    {op:"test",path:"/metadata/resourceVersion",value:$version},
    {op:"replace",path:"/data/paused",value:"recovering"}]')
k patch configmap "$STATE_CONFIGMAP" --type=json -p "$patch"

if [[ "$RECOVERY_KIND" == daemonset ]]; then
    workload=$(k get daemonset "$APPLICATION" -o json)
    if jq -e '.spec.template.spec.nodeSelector["backup.osixia.net/paused"] != null' <<< "$workload" >/dev/null; then
        # Change only the key reserved for backup pauses, preserving placement.
        k patch daemonset "$APPLICATION" --type=json \
            -p '[{"op":"remove","path":"/spec/template/spec/nodeSelector/backup.osixia.net~1paused"}]'
    fi
else
    k scale "$RECOVERY_KIND" "$APPLICATION" --replicas="$replicas"
fi

# The abandoned filesystem lock intentionally remains: recovery must not mount
# a busy RWO PVC or silently authorize another capture after a crash.
k patch configmap "$STATE_CONFIGMAP" --type=merge -p '{"data":{"paused":"false"}}'
echo "Resumption requested for $RECOVERY_KIND $APPLICATION after interrupted backup; verify rollout and inspect the backup lock."
