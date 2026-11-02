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

- `profile`: `sqlite`, `ldap`, `postgresql`, `mail` or `nextcloud`.
- `namespace`: an existing application namespace; namespaces are not created.
- `settings`: non-secret environment variables, merged into `backups-settings`.
- `files`: destination filename to project source file, relative to the config.
- `capture`: optional project script path overriding the selected capture profile.
  It is copied to `capture.sh`; a Mail/Nextcloud profile still generates its RBAC.
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
| ldap | `LDAP_MAX_DUMP_AGE_MINUTES`; `/source` holding completed native `openldap-cron-*.sha256` dumps and their `-config.gz`/`-data.gz` files |
| postgresql | `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD_FILE`; client TLS settings and referenced files as needed; `pg_dumpall` |
| mail | `APPLICATION` DaemonSet name; `/source/data`, `/source/state`, `/source/config` mounts |
| nextcloud | `APPLICATION` Deployment name, `PGDATABASE`, `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD_FILE`; TLS settings/files; `/source/data` mount |

Mail and Nextcloud interrupt the application for the complete capture and
archive. Their profiles generate scoped RBAC, a ServiceAccount, token mounting,
and a persistent ConfigMap with `paused: 'false'` and `replicas: ''` data. Mail
requires get/list pods, get/patch its DaemonSet and get/patch its state ConfigMap.
Nextcloud requires get/list pods and HPAs, get its Deployment, patch its
Deployment scale subresource and get/patch its state ConfigMap. Limit resource
names where possible. The generated `STATE_CONFIGMAP` setting uses the final prefixed name; never
reset recovery state by blindly reapplying its initial manifest.

Mail temporarily adds the node selector `backup.osixia.net/paused=<uuid>`;
reserve that key and ensure no node has it. Nextcloud refuses a Deployment
managed by an HPA. Place RWO consumers on the application's node with affinity.
Application volumes must be mounted read-only; do not add a recursive fsGroup.

## Recovery and restore

The engine requests resumption on normal exit and SIGTERM. A failed resumption
preserves `/published/.backup-lock`. SIGKILL or node loss can leave applications
stopped. Suspend scheduling, ensure no backup job is active, inspect the public
state ConfigMap, then restore the application: remove only Mail's temporary node
selector, or scale Nextcloud to the remembered replicas. Verify availability,
mark `paused: 'false'`, and remove the empty lock directory from the published
volume through an explicitly authorized maintenance operation. Never remove a
lock while another job is working. Jobs request resumption before releasing RWO
mounts; verify application rollout after job termination.

On an isolated restore host, verify the `.sha256` marker and decrypt the complete
archive with the operator's age identity before extracting. Preserve numerical
owners, ACLs and xattrs. Archives contain application files/dumps and optionally
`secrets/<secret-name>/<key>`. SQLite also records file ownership and mode in
`database-metadata.txt`. Validate SQLite integrity, import database dumps into
isolated compatible servers, and test each application's restore before enabling
its schedule. Keep private age identities outside the cluster and Git.

## Contributor checks

```sh
python3 -m unittest discover -s tests -v
docker build -t osixia/backup:extraction-test .
docker run --rm -i --network none --read-only --user 0:911 \
  --tmpfs /tmp --tmpfs /work --tmpfs /published --tmpfs /scripts \
  --tmpfs /source --tmpfs /age --tmpfs /credentials \
  osixia/backup:extraction-test bash -s < tests/runtime.sh
```

Generator tests require Kustomize. Runtime checks use only synthetic SQLite data,
a disposable age identity, and shell fixtures for application orchestration.
No host directory, kubeconfig or real credentials are mounted.
