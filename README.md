# osixia/backup

A container with the tools needed to capture application data, create archives
and encrypt them with [age](https://github.com/FiloSottile/age).

It is built from [osixia/container-age](https://github.com/osixia/container-age),
using the same Alpine base and Osixia build pipeline. The source repository is
`osixia/container-backup`; the container image is `osixia/backup`.

The image includes a backup engine and capture profiles. There is no entrypoint:
a Docker command or Kubernetes Job explicitly starts the routine. A generator
creates local Kubernetes manifests that each project commits to Git.
See [the Kubernetes generator guide](kubernetes/README.md).

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
needs temporary files or a client cache. `HOME` defaults to `/tmp` so clients can
write their cache there instead of trying to write to the root filesystem.

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

## GitLab : composants vides et preuve de couverture

Le profil `gitlab` est fourni par ce dépôt : `tools/generate.py` copie
`runtime/captures/gitlab.sh` vers la ConfigMap de capture de chaque projet.
Une correction de ce profil et des assets du générateur ne demande pas de
reconstruire l’image du moteur si ses outils restent compatibles. Les fichiers
`generated/` doivent être régénérés, jamais corrigés à la main.

GitLab Toolbox 19.5.3 ne produit pas de sous-tar pour un bucket vide et l’inscrit
dans `:skipped`, comme pour un composant omis ou inaccessible. Le profil strict
historique refuse tous les composants requis ignorés. Pour distinguer les sources
vides sans accepter les omissions, déclarer ces paramètres dans le service :

```yaml
profile: gitlab
namespace: another-gitlab
settings:
  GITLAB_BACKUP_CRONJOB: another-toolbox-backup
  GITLAB_TOOLBOX_IMAGE: registry.gitlab.com/gitlab-org/build/cng/gitlab-toolbox-ce:v19.5.3
  GITLAB_NATIVE_PVC: another-toolbox-backup-tmp
  GITLAB_S3_CONFIG_SECRET: another-backup-s3
  GITLAB_S3_CONFIG_KEY: config
  GITLAB_OBJECT_BUCKETS: '{"registry":"another-registry","artifacts":"another-artifacts","lfs":"another-lfs","uploads":"another-uploads","packages":"another-packages"}'
```

C’est un fragment à intégrer à la configuration du projet : conserver aussi ses
références de Secrets à archiver, ses volumes de credentials et ses overlays de
stockage/routage. Aucun nom de namespace, de bucket ou de Secret applicatif n’est
codé en dur dans le profil commun. Ajouter les autres composants utilisés, par
exemple `terraform_state`, à `GITLAB_OBJECT_BUCKETS` pour qu’ils soient contrôlés.
Les cinq composants ci-dessus et base/dépôts restent obligatoires.

Le générateur ajoute un init container utilisant l’image Toolbox native déclarée
et son client `s3cmd`. Il monte le PVC natif en lecture seule et la seule
configuration S3 nécessaire, fournie par le Secret existant. Aucun droit API
Kubernetes supplémentaire, aucune lecture API de Secret, aucune clé age privée.
L’endpoint est celui du fichier S3 du projet ; le générateur n’en charge pas la
valeur. L’init ne télécharge pas les objets, ne modifie ni bucket ni archive.

Pour chaque composant déclaré ignoré, une requête limitée à un objet doit réussir
avec une liste vide. Erreur d’accès, timeout, bucket absent, préfixe ou objet
présent font échouer le contrôle. Les composants non ignorés doivent avoir leur
sous-tar ; la base et les dépôts ne peuvent jamais être ignorés. La preuve est
liée au nom, taille, inode, mtime et SHA-256 de l’archive native et à la déclaration
des buckets. Elle expire après cinq minutes et est revérifiée dans la capture
avant d’être incluse sous `gitlab-coverage.json` dans le lot chiffré.

Cette preuve décrit les buckets au moment du contrôle après la capture native ;
elle ne transforme pas le backup GitLab en snapshot transactionnel global. Une
écriture concurrente dans un bucket ignoré peut faire refuser la capture ; relancer
alors une sauvegarde native. Ne pas faire de rotation des credentials pendant
la capture et sa validation. Ne pas retirer un bucket utilisé de la déclaration
pour contourner un échec. Les composants non déclarés ne sont pas réputés couverts.

Les projets sans ces paramètres conservent le comportement strict historique.
Les tests du profil et du générateur utilisent exclusivement des fixtures
synthétiques (`python3 -m unittest discover -s tests -v`).

## Profils de fichiers persistants

`vaultwarden` reprend le protocole de pause/reprise du Deployment et capture
`/source/data` complet (base, journaux SQLite, pièces jointes, Sends et clés).
Il exige `APPLICATION` et `SQLITE_FILE`, et contrôle SQLite depuis une copie privée.
Le PVC applicatif reste en lecture seule. Ce profil est partagé entre les projets,
sans `capture:` spécifique dans les applications ni nouvelle image du moteur.

`statefulset-offline` applique le même protocole à un StatefulSet nommé par
`APPLICATION`, avec sauvegarde des répliques dans `STATE_CONFIGMAP`, arrêt de tous
ses pods et refus d’un HPA avant intervention. Il archive `/source/data` complet,
puis demande la reprise. Le générateur fournit un rôle limité au StatefulSet
nommé, à son sous-resource `scale`, à ses pods et à la ConfigMap d’état. Il ne
faut pas utiliser ce profil pour prétendre capturer des données conservées hors
du PVC monté. Les répertoires éphémères et services externes restent à traiter
selon le contexte applicatif. Une perte brutale du nœud peut demander une reprise
opérateur ; lire l’état sauvegardé avant de remettre les répliques.

## Reprise indépendante après interruption

Les profils qui arrêtent un service génèrent aussi un CronJob de reprise séparé :
Nextcloud, SQLite hors ligne, Vaultwarden, StatefulSet hors ligne et Mail. Il
reprend le service après la fin de tous ses pods de capture, y compris après un
OOM/SIGKILL qui empêche le nettoyage du moteur. Il ne monte aucune donnée ni
aucun Secret et conserve le verrou de la sauvegarde échouée pour inspection.
Les deux calendriers sont initialement suspendus ; activer explicitement la
reprise avec la capture. Le script est distribué par ConfigMap et fonctionne
avec les images compatibles existantes, sans reconstruction obligatoire.
Voir le [protocole, les limites et l’activation](kubernetes/README.md#recovery-and-restore).
