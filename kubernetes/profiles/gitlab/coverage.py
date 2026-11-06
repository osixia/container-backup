#!/usr/bin/env python3
"""Validate native GitLab coverage; never print S3 output or credential values."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import time

REQUIRED = {'registry', 'artifacts', 'lfs', 'uploads', 'packages'}
MAX_PROOF_AGE = 300


def identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise ValueError('Native archive must be a nonempty regular file')
    return {'name': path.name, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'inode': info.st_ino}


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def buckets():
    result = json.loads(os.environ['GITLAB_OBJECT_BUCKETS'])
    if (not isinstance(result, dict) or not REQUIRED <= result.keys()
            or any(not re.fullmatch(r'[a-z][a-z0-9_]*', name) or name in {'db', 'repositories'} for name in result)
            or any(not isinstance(b, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', b) for b in result.values())):
        raise ValueError('Explicit bucket mapping required for all five object components')
    return result


def coverage(path):
    """Read only native metadata and tar headers; never extract payload files."""
    with tarfile.open(path, 'r:') as archive:
        members = archive.getmembers()
        names = {}
        for member in members:
            name = member.name
            while name.startswith('./'):
                name = name[2:]
            if name in names:
                raise ValueError('Duplicate native archive member')
            names[name] = member
        metadata = names.get('backup_information.yml')
        if metadata is None or not metadata.isfile() or not 0 < metadata.size <= 65536:
            raise ValueError('Native backup metadata missing or invalid')
        text = archive.extractfile(metadata).read().decode('utf-8')
        fields = dict(line.split(': ', 1) for line in text.splitlines() if ': ' in line)
        if (fields.get(':installation_type') != 'gitlab-helm-chart'
                or not fields.get(':gitlab_version')
                or (fields.get(':backup_id') or '') + '_gitlab_backup.tar' != path.name):
            raise ValueError('Native backup identity mismatch')
        skipped = {v.strip() for v in fields.get(':skipped', '').strip('"\'').split(',') if v.strip()}
        if skipped & {'db', 'repositories'}:
            raise ValueError('Database or repositories skipped')
        if not any(n.startswith('db/') and m.isfile() and m.size > 0 for n, m in names.items()):
            raise ValueError('Database payload missing')
        if not any(n == 'repositories' or n.startswith('repositories/') for n in names):
            raise ValueError('Repositories payload missing')
        result = {}
        for component in sorted(buckets()):
            member = names.get(component + '.tar.gz')
            if component in skipped:
                if member is not None:
                    raise ValueError('Skipped component contradicts archive contents')
                result[component] = 'requires-empty-proof'
            else:
                if member is None or not member.isfile() or member.size <= 0:
                    raise ValueError('Required component payload missing: ' + component)
                result[component] = 'archived'
        return result


def bucket_empty(bucket):
    # Success with empty stdout proves the bucket is accessible and has no objects
    # or prefixes. Permission/network/missing bucket errors are never accepted.
    result = subprocess.run(
        ['s3cmd', '--config=' + os.environ.get('GITLAB_S3_CONFIG_FILE', '/gitlab-s3-config/config'), '--limit=1',
         'ls', 's3://' + bucket + '/'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    if result.returncode != 0:
        raise ValueError('S3 coverage check failed for bucket ' + bucket)
    return not result.stdout.strip()


def preflight(folder, output):
    candidates = sorted(folder.glob('*_gitlab_backup.tar'), key=lambda p: p.lstat().st_mtime_ns)
    if not candidates:
        raise ValueError('Native archive missing')
    path = candidates[-1]
    before = identity(path)
    declared = buckets()
    components = coverage(path)
    for component, status in components.items():
        if status == 'requires-empty-proof':
            if not bucket_empty(declared[component]):
                raise ValueError('Skipped component has a nonempty bucket: ' + component)
            components[component] = 'empty'
    checksum = digest(path)
    if before != identity(path):
        raise ValueError('Native archive changed during coverage check')
    proof = {'schema': 1, 'archive': before, 'sha256': checksum,
             'buckets': declared, 'components': components, 'verified_at': time.time()}
    temporary = output.with_suffix('.tmp')
    temporary.write_text(json.dumps(proof, sort_keys=True) + '\n')
    temporary.replace(output)


def validate(path, proof_path, destination):
    proof = json.loads(proof_path.read_text())
    age = time.time() - proof['verified_at']
    if (proof.get('schema') != 1 or not 0 <= age <= MAX_PROOF_AGE
            or proof['archive'] != identity(path)
            or proof['sha256'] != digest(path) or proof['buckets'] != buckets()):
        raise ValueError('Coverage proof missing, stale or bound to another archive')
    actual = coverage(path)
    expected = {name: ('empty' if status == 'requires-empty-proof' else 'archived')
                for name, status in actual.items()}
    if proof['components'] != expected:
        raise ValueError('Coverage proof contradicts native metadata')
    destination.write_text(json.dumps(proof, indent=2, sort_keys=True) + '\n')


def main():
    os.umask(0o077)
    try:
        if len(sys.argv) == 4 and sys.argv[1] == 'preflight':
            preflight(Path(sys.argv[2]), Path(sys.argv[3]))
        elif len(sys.argv) == 5 and sys.argv[1] == 'validate':
            validate(*(Path(p) for p in sys.argv[2:]))
        else:
            raise ValueError('Invalid coverage command')
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError):
        # Raw diagnostics can contain endpoint details: keep this output generic.
        print('GitLab coverage check failed: missing payload, inaccessible/nonempty bucket, or invalid proof.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
