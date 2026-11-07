# Git-tracked Kubernetes backups

Use a checkout of a chosen container-backup release to generate manifests. The
image contains `/usr/local/lib/backup/run.sh`; the generator copies the selected
capture scripts and Kubernetes templates into the consumer repository. Commit
all generated files, including `.generated.json`. No remote Kustomize bases are
needed to build or deploy the resulting tree.

## Start a project

Install Python 3 and `tools/requirements.txt` (preferably in a virtualenv).
Copy `examples/project/backup/` into the project and adapt its public configuration.
Never put secret values or private age identities in this directory.

```sh
python3 /path/to/container-backup/tools/generate.py backup/config.yaml
kustomize build backup/generated
```

The generator never contacts Kubernetes and always writes beside the config, in
`generated/`. CronJobs start suspended. Generating or building is not deployment.
A project has this layout:

```text
backup/
  config.yaml            # profile, namespace, settings and image reference
  cronjob.yaml           # project patch: mounts, schedule, affinity, resources
  export.yaml            # project patch: storage class and capacity
  generated/             # committed; do not edit directly
    .generated.json
    common/
    portal/
    kustomization.yaml
```

For an upgrade, check out the desired generator release, update the image to the
matching release digest, regenerate and review the Git diff. Commit the result
before a separate, operator-controlled deployment. The inventory refuses to
overwrite manual edits, deletions or additional files. Move adaptations to the
inputs first. Removing a service removes only its inventoried generated files.
The source release is recorded by the project’s chosen checkout/tool lock; the
inventory records content hashes, not a release inferred from a working tree.

## Configuration

`schema: 1`, `image` and `services` are required. Use an explicit image tag for
local development, and a digest of the matching published engine for production.
Service identifiers contain lowercase letters, matching the archive naming
contract. Each service specifies:

- `profile`: `sqlite`, `sqlite-offline`, `ldap`, `postgresql`, `mail` or `nextcloud`.
- `namespace`: an existing application namespace; namespaces are not created.
- `settings`: non-secret environment variables, merged into `backups-settings`.
- `files`: destination filename to project source file, relative to the config.
- `capture`: optional project script path overriding the selected capture profile.
  It is copied to `capture.sh`; a Mail/Nextcloud profile still generates its RBAC.
- `recovery`: optional boolean. Built-in pause/resume profiles generate an
  independent recovery CronJob by default; set `false` to omit it. Custom
  `capture` scripts opt out by default. Enable recovery for a custom capture only
  if it preserves the built-in pause-state, pod labels and backup-lock protocol.
  Profiles that do not pause an application cannot enable recovery.
- `overlay`: Kustomize `resources`, `patches`, `configMapGenerator`, `labels` and
  `replacements`. Referenced files must be supplied through `files` and use local
  filenames for a self-contained tree. Do not use remote resources.

Resource names before the automatic service prefix are `backups` (CronJob,
Deployment and Service), `backups-published` (PVC), `backups-settings`,
`backups-scripts` and `backups-nginx` (ConfigMaps). The archive prefix is the
service identifier. The base uses the cluster's default storage class. Override
capacity, class, schedule, security context and mounts with project patches.
All services currently include an HTTP export, accessible through a ClusterIP
Service on port 8080. Add routing separately if needed; the generator creates no
ingress. Size storage for complete archives over the entire retention period.

Create a public `backup-recipient` ConfigMap containing an age `recipient` file
in each namespace, outside generation. Mount required Secrets read-only under
`/credentials/<secret-name>` and list their names in `BACKUP_SECRETS` to include
them in the encrypted archive. Never pass secret values through `settings`.
The engine reads projected Secrets only inside explicitly started backup jobs.

## Capture profile contract

Captures define `prepare_backup`, optionally `archive_backup` and
`resume_application`. The engine provides `stage`, `workdir`, `k`,
`run_capture_command`, `write_pgpass` and `wait_for_application_stop`. They run
in Bash with strict error handling and private working directories. The engine
archives with GNU tar, encrypts the stream with age, atomically publishes the
archive then its SHA-256 marker, and expires complete archives. It does not
provide incremental backups or restore automation.

| Profile | Required project inputs |
| --- | --- |
| sqlite | `SQLITE_FILE`, `SQLITE_BACKUP_NAME`; source mount; SQLite backup API and integrity check |
| sqlite-offline | SQLite settings above plus `APPLICATION` Deployment name; briefly stop all writers, copy database/WAL/journal privately, request resumption, then back up and integrity-check the private copy |
| ldap | `LDAP_MAX_DUMP_AGE_MINUTES`; `/source` holding completed native `openldap-cron-*.sha256` dumps and their `-config.gz`/`-data.gz` files |
| postgresql | `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD_FILE`; client TLS settings and referenced files as needed; `pg_dumpall` |
| mail | `APPLICATION` DaemonSet name; `/source/data`, `/source/state`, `/source/config` mounts |
| nextcloud | `APPLICATION` Deployment name, `PGDATABASE`, `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD_FILE`; TLS settings/files; `/source/data` mount |

Mail and Nextcloud interrupt the application for the complete capture and
archive. Offline SQLite stops its Deployment only while copying the database and
journals; the original volume stays read-only. Its shared-memory file is rebuilt
in the private copy. Use this profile for WAL databases that cannot open from a
read-only mount when the WAL/shared-memory files are absent. All three controlled profiles generate scoped RBAC, a ServiceAccount, token mounting,
and a persistent ConfigMap with `paused: 'false'` and `replicas: ''` data. Mail
requires get/list pods, get/patch its DaemonSet and get/patch its state ConfigMap.
Nextcloud and offline SQLite require get/list pods and HPAs, get its Deployment, patch its
Deployment scale subresource and get/patch its state ConfigMap. Limit resource
names where possible. The generated `STATE_CONFIGMAP` setting uses the final prefixed name; never
reset recovery state by blindly reapplying its initial manifest.

Mail temporarily adds the node selector `backup.osixia.net/paused=<uuid>`;
reserve that key and ensure no node has it. Nextcloud refuses a Deployment
managed by an HPA. Place RWO consumers on the application's node with affinity.
Application volumes must be mounted read-only; do not add a recursive fsGroup.

## Recovery and restore

The engine requests resumption on normal exit and SIGTERM. SIGKILL, including
an OOM kill, cannot run its cleanup trap. The generator also emits a separate
`<service>-backups-recovery` CronJob for `nextcloud`, `sqlite-offline`,
`vaultwarden`, `statefulset-offline` and `mail`. Its script is copied into a
ConfigMap and runs using the selected toolbox image; compatible existing images
can run it without an engine rebuild. Online SQLite, LDAP, PostgreSQL and native
GitLab captures do not pause an application and generate no recovery job.

Both capture and recovery CronJobs start suspended. Activate recovery together
with the capture schedule, through a project overlay patch targeting the CronJob
named `backups-recovery` before the service prefix. Recovery runs every minute
by default, with `concurrencyPolicy: Forbid`, one retry and a two-minute Job
deadline. Capture retains its six-hour default `activeDeadlineSeconds`: choose a
capture deadline appropriate to the allowed maintenance window. A hung capture
must actually terminate before application writes can safely resume.

Recovery reads the public pause state and lists only this service's capture
pods in its namespace. Pending, Running, Unknown or missing phases block it,
including terminating captures that have not reached a terminal phase. API
errors fail closed. When all capture pods are Failed or Succeeded (or already
garbage-collected), a JSON patch claims the exact ConfigMap resourceVersion and
sets `paused: recovering`. A changed snapshot aborts the attempt. Recovery then
restores saved Deployment/StatefulSet replicas, or removes only Mail's reserved
pause selector, and clears the pause flag. Partial failures retry from
`recovering`; normal captures require `paused: false` before stopping a service.

The recovery job reuses the existing scoped orchestration ServiceAccount and
Role. It mounts no application credentials, Secret, application volume or
published volume, so it can run without waiting for a busy RWO PVC. It cannot
delete pods. Keep the capture labels `app.kubernetes.io/name=backup-job` and
`backup.osixia.net/service=<service>`: these identify all captures, including
manual Jobs. Recovery uses a different application label.

This requests resumption, not a healthy rollout: verify availability separately.
Recovery cannot force a partitioned node to stop writing. Unknown/running
captures are left alone until Kubernetes establishes their termination; never
force-delete one to bypass this guard without first fencing its node. API,
scheduling or workload failures can still require operator intervention. The
one-minute schedule is not a guarantee of recovery within one minute.

The abandoned `/published/.backup-lock` deliberately remains after automatic
resumption. Recovery neither publishes nor retries the failed archive. Inspect
the failure, suspend capture scheduling, ensure no capture is active, verify
availability, and remove the empty lock directory through an authorized
maintenance operation before retrying. Inspect unfinished ciphertext separately;
`.tmp` files are not complete backups. Never remove a lock while a job works.

For manual recovery, suspend both CronJobs and inspect the public state ConfigMap
(including `paused: recovering`). Restore remembered replicas or remove only
Mail's temporary selector, verify availability, then mark `paused: false`.
Never reset recovery state by blindly reapplying its initial manifest. Normal
captures still request resumption before releasing RWO mounts; verify rollout
after their pod terminates.

On an isolated restore host, verify the `.sha256` marker and decrypt the complete
archive with the operator's age identity before extracting. Preserve numerical
owners, ACLs and xattrs. Archives contain application files/dumps and optionally
`secrets/<secret-name>/<key>`. SQLite also records file ownership and mode in
`database-metadata.txt`. Validate SQLite integrity, import database dumps into
isolated compatible servers, and test each application's restore before enabling
its schedule. Keep private age identities outside the cluster and Git.

The controlled profiles mount a generated kubeconfig under `/kube/config` and
set `KUBECONFIG` explicitly. It points to `https://kubernetes.default.svc`, the
projected service-account CA and `tokenFile`, never a token value. The shared
engine `k` helper requires this setting rather than silently using localhost.
Custom captures invoking `k` must also provide an explicit kubeconfig. Backups
that do not use the Kubernetes API need no kubeconfig or service-account token.

## Contributor checks

```sh
python3 -m unittest discover -s tests -v
docker build -t osixia/backup:extraction-test .
python3 tests/check-runtime.py osixia/backup:extraction-test
```

Generator tests require Kustomize. Runtime checks require Docker, Python 3 and
OpenSSL on the host. They cover bare WAL databases, recovery of committed WAL
rows, resumption after a copy failure, and the real kubectl client against a
local HTTPS API using a synthetic token and a disposable certificate. All other
application orchestration is simulated. Only the temporary synthetic TLS fixture
is mounted; no real data, kubeconfig or credentials are available in the container.
The container has no external network and drops all capabilities.
Recovery tests also require Bash and jq on the host. They execute the production
recovery script against a disposable fake kubectl API: terminal OOM pods,
active/unknown captures, stale state snapshots, partial failures, all three
workload kinds and idempotent retries. No real cluster is contacted.

## GitLab chart native archive profile

The `gitlab` profile consumes the archive made by the official GitLab Toolbox
CronJob. It does not replace backup-utility, stop GitLab, or run pods/exec. Set
GITLAB_BACKUP_CRONJOB to the native CronJob name, mount its persistent RWX
scratch volume read-only at /source, and retain its archive with the vendor
--skip-cleanup option. The generated Role only reads that CronJob and Jobs.

Capture requires a recent completed Job owned by the current CronJob UID, no
active native Job, and exactly one regular tar matching the Job time window.
It checks the native metadata for omitted required components and refuses a
replaced archive. Coordinate schedules and do not start manual native backups
during encryption. Native snapshots have GitLab's normal online-backup semantics.

BACKUP_SECRETS lists the operator and generated Rails/SSH/internal secrets to
project and encrypt, without API read rights on Secrets. Both native plaintext
S3 intermediates and local scratch must stay private. Only the age envelope and
its final checksum are published. No private age identity is needed in Kubernetes.
The generated capture uses the existing 1.0.1 engine/tools: no image rebuild is
needed when distributing the capture through a project ConfigMap.
