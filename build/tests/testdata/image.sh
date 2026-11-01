#!/bin/bash
# Functional image checks. All data and keys below are disposable test fixtures.
set -euo pipefail

# Versions and tools must match the documented image.
[[ "$(id -u)" == 65534 ]]
[[ "$HOME" == /tmp ]]
[[ "$(age --version)" == "v$AGE_VERSION" ]]
pg_dump --version | grep -q " $POSTGRESQL_MAJOR\."
pg_dumpall --version | grep -q " $POSTGRESQL_MAJOR\."
pg_restore --version | grep -q " $POSTGRESQL_MAJOR\."
psql --version | grep -q " $POSTGRESQL_MAJOR\."
tar --version | grep -q 'GNU tar'
find . --version | grep -q 'GNU findutils'
sha256sum --version | grep -q 'GNU coreutils'
kubectl version --client --output=json |
    jq -e '.clientVersion.gitVersion | startswith("v" + env.KUBECTL_VERSION + ".")' >/dev/null
[[ -s /etc/ssl/certs/ca-certificates.crt ]]

workdir=$(mktemp -d)
trap 'rm -rf -- "$workdir"' EXIT
cd "$workdir"

# A small file tree exercises the metadata needed for application restores.
mkdir source restored
printf 'synthetic backup data\n' > source/file
chmod 0640 source/file
ln source/file source/hardlink
ln -s file source/symlink
mkfifo source/pipe
setfattr -n user.backup-test -v preserved source/file
setfacl -m u:65533:r source/file

# Test-only keys never leave this temporary directory and are never printed.
age-keygen -o identity 2>/dev/null
age-keygen -o other-identity 2>/dev/null
age-keygen -y identity > recipient

# Use only the public recipient to create the encrypted archive.
tar --acls --xattrs --numeric-owner -czf - -C source . |
    age -R recipient -o backup.tar.gz.age

sha256sum backup.tar.gz.age > backup.sha256
sha256sum --check --status backup.sha256

if age -d -i other-identity backup.tar.gz.age >/dev/null 2>&1; then
    echo 'A different key must not decrypt the archive' >&2
    exit 1
fi

# Decrypt first, then extract a fully authenticated archive.
age -d -i identity -o backup.tar.gz backup.tar.gz.age
tar --acls --xattrs --numeric-owner -xzf backup.tar.gz -C restored

cmp source/file restored/file
[[ "$(stat -c %a restored/file)" == "$(stat -c %a source/file)" ]]
[[ "$(stat -c %u:%g restored/file)" == "$(stat -c %u:%g source/file)" ]]
[[ "$(readlink restored/symlink)" == file ]]
[[ restored/file -ef restored/hardlink ]]
[[ -p restored/pipe ]]
[[ "$(getfattr --only-values -n user.backup-test restored/file)" == preserved ]]
getfacl --omit-header --numeric restored/file | grep -q '^user:65533:r--$'

# Detect a truncated ciphertext before handing any archive to the extractor.
cp backup.tar.gz.age truncated.age
truncate -s -1 truncated.age
if age -d -i identity -o truncated.tar.gz truncated.age 2>/dev/null; then
    echo 'A truncated ciphertext must fail authentication' >&2
    exit 1
fi

# Exercise SQLite's online backup while the source connection remains open.
# This also verifies that Python includes the sqlite3 module used by older jobs.
python3 - <<'PY'
import sqlite3
import subprocess

with sqlite3.connect('source.sqlite3') as source:
    source.execute('PRAGMA journal_mode=WAL')
    source.execute('CREATE TABLE example (id INTEGER PRIMARY KEY)')
    source.execute('INSERT INTO example VALUES (1)')
    source.commit()

    subprocess.run(['sqlite3', 'source.sqlite3', '.backup copied.sqlite3'], check=True)

    with sqlite3.connect('copied.sqlite3') as copied:
        assert copied.execute('SELECT count(*) FROM example').fetchone() == (1,)
        assert copied.execute('PRAGMA integrity_check').fetchone() == ('ok',)
PY

echo 'Backup image checks passed'
