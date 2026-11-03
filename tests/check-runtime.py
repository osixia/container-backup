#!/usr/bin/env python3
"""Run production scripts using synthetic data and a disposable HTTPS API."""
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parent
image = sys.argv[1] if len(sys.argv) > 1 else 'osixia/backup:develop'
with tempfile.TemporaryDirectory(prefix='backup-synthetic-tls-') as directory:
    tls = Path(directory)
    # This identity is disposable test data, never a production key or a value
    # read from a real secret store. Do not print the generated material.
    subprocess.run([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
        '-keyout', str(tls / 'key.pem'), '-out', str(tls / 'cert.pem'),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tls.chmod(0o755)
    for path in tls.iterdir():
        path.chmod(0o644)  # Allow the cap-drop container to read these test fixtures.
    command = [
        'docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
        '--user', '0:911', '--cap-drop', 'ALL',
        '--tmpfs', '/tmp:rw,exec,nosuid,nodev', '--tmpfs', '/work',
        '--tmpfs', '/published', '--tmpfs', '/scripts', '--tmpfs', '/source',
        '--tmpfs', '/age', '--tmpfs', '/credentials',
        '--mount', f'type=bind,src={tls},dst=/fixture-tls,readonly',
        image, 'bash', '-s',
    ]
    with (root / 'runtime.sh').open('rb') as script:
        subprocess.run(command, stdin=script, check=True)
