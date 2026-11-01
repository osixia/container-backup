# osixia/backup

A container with the tools needed to capture application data, create archives
and encrypt them with [age](https://github.com/FiloSottile/age).

It is built from [osixia/container-age](https://github.com/osixia/container-age),
using the same Alpine base and Osixia build pipeline. The source repository is
`osixia/container-backup`; the container image is `osixia/backup`.

The Dockerfile installs standard packages. There is no entrypoint or automatic
backup routine: a Docker command or Kubernetes Job runs the script you provide.
Application-specific steps, schedules and retention stay with the application.

## Included tools

| Tools | Purpose |
| --- | --- |
| `bash` | Scripts with `set -euo pipefail` and cleanup traps |
| GNU `tar`, `gzip` | Compressed archives, including links and file metadata |
| `getfacl`, `setfacl`, `getfattr`, `setfattr` | Inspect and test ACLs and extended attributes |
| GNU coreutils and findutils | Checksums, file operations and retention commands |
| `age`, `age-keygen` | Public-key encryption; decryption on the restore host |
| `sqlite3` | Consistent SQLite copies using `.backup` |
| `pg_dump`, `pg_dumpall`, `pg_restore`, `psql` | PostgreSQL dumps and restores |
| `kubectl`, `jq` | Explicit application orchestration from a Kubernetes Job |
| `python3` | Existing Python scripts, with the standard `sqlite3` module |
| CA certificates | Certificate verification by clients such as PostgreSQL and kubectl |

Default tool families are age **1.3.2**, PostgreSQL **18** and kubectl **1.36**.
The Dockerfile declares these versions; Alpine supplies package revisions.
Inspect exact installed versions with `apk info -v`.

`pg_dump` must support the PostgreSQL server version. kubectl must also be within
the [supported version skew](https://kubernetes.io/releases/version-skew-policy/#kubectl)
of the API server. Select suitable build arguments before using another cluster.

## Build locally

```bash
docker build --tag osixia/backup:develop .
```

The examples below use this local tag. After publishing a release, use its version
or, preferably, its image digest.

The image runs as UID/GID `65534:65534` by default. Select your own UID/GID for
host-mounted directories, as in the following examples. Prepare their permissions
before starting the container.

## Create an encrypted archive

Prepare these paths:

- `source/`: a consistent capture of the application files and any database dump.
- `backups/`: an existing output directory, writable by your user.
- `recipient`: the age public recipient file supplied by the operator.

For a live application, first use its documented backup procedure. Nextcloud, for
example, needs its files and database captured in the same maintenance window.
A database file copied while it is being written is not a substitute for a dump
or a consistent SQLite backup.

```bash
docker run --rm -i \
  --user "$(id -u):$(id -g)" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --mount type=bind,src="$PWD/source",dst=/source,readonly \
  --mount type=bind,src="$PWD/backups",dst=/backup \
  --mount type=bind,src="$PWD/recipient",dst=/run/recipient,readonly \
  osixia/backup:develop bash -seuo pipefail <<'BACKUP'
umask 077
cd /backup

archive="backup-$(date -u +%Y%m%dT%H%M%SZ)-$(cat /proc/sys/kernel/random/uuid).tar.gz.age"
trap 'rm -f -- "$archive.tmp" "$archive.sha256.tmp"' EXIT

tar --acls --xattrs --numeric-owner -czf - -C /source . |
  age -R /run/recipient -o "$archive.tmp"

mv -- "$archive.tmp" "$archive"
sha256sum "$archive" > "$archive.sha256.tmp"
mv -- "$archive.sha256.tmp" "$archive.sha256"
BACKUP
```

Only ciphertext is written to `backups/`. The checksum file is published last and
uses a relative filename, so it can be checked after copying the backup elsewhere.
Consumers must wait for that checksum file before collecting an archive.

The backup Job only needs the public recipient. Keep the private age identity on
the operator's restore host, outside the cluster. `age-keygen` is included with the
age package; the container does not generate or install any keys automatically.

## Capture a SQLite database

With the application volume available at `/source` and a writable directory at
`/backup`, run:

```bash
sqlite3 -readonly /source/application.sqlite3 '.backup /backup/application.sqlite3'
sqlite3 -readonly /backup/application.sqlite3 'PRAGMA integrity_check;'
```

These are commands to run **inside** the image, with the appropriate mounts.
SQLite's [online backup](https://www.sqlite.org/backup.html) keeps the copy
consistent. The database and any existing WAL/SHM files must be accessible to the
selected user. Check the result, then include this copy in the encrypted archive.
It contains plaintext and belongs in a private working directory, not an HTTP export.

## Capture PostgreSQL

Use PostgreSQL's normal connection settings: `PGHOST`, `PGPORT`, `PGUSER`,
`PGDATABASE`, `PGPASSFILE`, `PGSSLMODE` and `PGSSLROOTCERT` as appropriate.
Mount the password file and CA certificate read-only; use `verify-full` for TLS.
The password file must have the permissions required by PostgreSQL, normally `0600`.

Inside the image, for a single database:

```bash
pg_dump --format=custom --file=/backup/database.dump
```

For a whole PostgreSQL instance, including roles:

```bash
pg_dumpall --file=/backup/cluster.sql
```

Store dumps in a private working directory and include them in the encrypted
archive. The application decides when writes must stop to keep the dump consistent
with its other files. See the [PostgreSQL backup documentation](https://www.postgresql.org/docs/current/backup-dump.html).

## Restore on a trusted host

1. Copy the archive and its checksum to the restore host.
2. Run `sha256sum --check ARCHIVE.sha256` in that directory.
3. Decrypt the archive with the operator's private age identity.
4. Extract it into an empty directory and follow the application's restore steps.

Commands inside the image, with the archive at `/backup/backup.tar.gz.age`, the
private identity at `/run/identity`, and an empty writable directory at `/restore`:

```bash
set -euo pipefail

age -d -i /run/identity -o /restore/backup.tar.gz /backup/backup.tar.gz.age

# Only extract after age has successfully authenticated the complete file.
mkdir /restore/files
tar --acls --xattrs --numeric-owner -xzf /restore/backup.tar.gz -C /restore/files
```

Allow disk space for the decrypted archive and extracted files. Restoring original
owners may require root on the restore host. Extended attributes outside the
`user` namespace need explicit tar options and appropriate permissions; see the
[GNU tar manual](https://www.gnu.org/software/tar/manual/tar.html).

## Use in Kubernetes

A Job selects `command: ["bash", "/scripts/backup.sh"]` and mounts the script,
application data, working space, output volume and public recipient it needs.
The image works with a read-only root filesystem and dropped capabilities when
the chosen user can read and write those mounts. Mount writable `/tmp` if a tool
needs temporary files or a client cache.

Mount only the public age recipient in backup Jobs. Mount application credentials
as files rather than putting values into scripts. Disable service account token
mounting for Jobs that do not call the Kubernetes API. Jobs that use kubectl need
RBAC limited to their application and the operations their script performs.

Application pause/resume, volume placement and retention are configured by the
Job, rather than happening automatically when the image starts.

## Tests and releases

The [build guide](build/README.md) describes local tests and the inherited
`osixia/dagger-build` pipeline. Tests use disposable data and keys only.

The toolbox has its own version, independent of the age version. Release tags use
`x.y.z`, or `x.y.z-n` for rebuilds. The release pipeline publishes the version
aliases and `latest`, signs the image with cosign and attaches an SPDX SBOM.
Registry credentials must be configured before creating a release tag.

## License

Project files use the [MIT license](LICENSE.md). The image's packages keep their
respective upstream licenses. Build and CI integration are adapted from
[osixia/container-age](https://github.com/osixia/container-age).
