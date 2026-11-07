#!/bin/bash
# shellcheck disable=SC1091,SC2034,SC2154
# Sourced image profiles share stage and paused with this synthetic fixture.
# Run inside the test image, with only disposable tmpfs mounts and no network.
set -euo pipefail
umask 077
mkdir -p /scripts /source /published /work /age /credentials/example/generation
cp /usr/local/lib/backup/captures/sqlite.sh /scripts/capture.sh
age-keygen -o /tmp/identity 2>/dev/null
age-keygen -y /tmp/identity > /age/recipient
printf 'synthetic-only' > /credentials/example/generation/key
ln -s generation /credentials/example/..data
sqlite3 /source/db.sqlite3 'CREATE TABLE fixture (id INTEGER); INSERT INTO fixture VALUES (42);'
export SERVICE=portal SQLITE_FILE=/source/db.sqlite3 SQLITE_BACKUP_NAME=db.sqlite3 BACKUP_SECRETS=example RETENTION_DAYS=15
bash /usr/local/lib/backup/run.sh >/tmp/run.log
cd /published
sha256sum --check --status ./*.sha256
archive=$(find . -name '*.tar.gz.age' -print -quit)
age -d -i /tmp/identity -o /tmp/archive.tar.gz "$archive"
mkdir /tmp/restored
tar -xzf /tmp/archive.tar.gz -C /tmp/restored
[[ $(sqlite3 /tmp/restored/db.sqlite3 'PRAGMA integrity_check;') == ok ]]
[[ $(sqlite3 /tmp/restored/db.sqlite3 'SELECT COUNT(*) FROM fixture;') == 1 ]]
cmp /credentials/example/generation/key /tmp/restored/secrets/example/key
[[ ! -e /published/.backup-lock ]]
[[ -z $(find /work -mindepth 1 -print -quit) ]]
# A concurrent/stale lock must refuse a new run, preserving the existing lock.
mkdir /published/.backup-lock
if bash /usr/local/lib/backup/run.sh >/tmp/locked.log 2>&1; then exit 1; fi
[[ -d /published/.backup-lock ]]
rmdir /published/.backup-lock
# Normal capture failure must release the lock and clean private working files.
SQLITE_FILE=/source/missing
if bash /usr/local/lib/backup/run.sh >/tmp/failed.log 2>&1; then exit 1; fi
[[ ! -e /published/.backup-lock ]]
[[ -z $(find /work -mindepth 1 -print -quit) ]]

# Orchestration is replaced by a shell fixture; no real kubectl is invoked.
export APPLICATION=fixture STATE_CONFIGMAP=fixture-backups-state PGDATABASE=fixture PGPASSWORD_FILE=/tmp/fake
k() {
    printf '%s\n' "$*" >> /tmp/operations
    case "$*" in
        'get configmap '*) printf false ;;
        'get hpa '*) printf '{"items":[]}' ;;
        'get deployment fixture -o jsonpath='*) printf 2 ;;
        'get deployment fixture -o json'*) printf '{"spec":{"selector":{"matchLabels":{"app":"fixture"}}}}' ;;
        'get daemonset fixture -o json'*) printf '{"spec":{"template":{"spec":{"nodeSelector":{}}},"selector":{"matchLabels":{"app":"fixture"}}}}' ;;
    esac
}
wait_for_application_stop() { [[ $1 == app=fixture ]]; }
write_pgpass() { :; }
run_capture_command() { :; }
stage=/tmp/restored
source /usr/local/lib/backup/captures/nextcloud.sh
prepare_backup
resume_application
[[ $paused == false ]]
grep -q 'scale deployment fixture --replicas=0' /tmp/operations
grep -q 'scale deployment fixture --replicas=2' /tmp/operations
source /usr/local/lib/backup/captures/mail.sh
prepare_backup
resume_application
[[ $paused == false ]]
grep -q 'patch daemonset fixture' /tmp/operations
grep -q 'backup.osixia.net/paused' /tmp/operations


# Regression: WAL databases without sidecars cannot open on a read-only source.
# Drop DAC_OVERRIDE for this test so chmod 0555 enforces the source boundary.
sqlite3 /source/wal.db 'PRAGMA journal_mode=WAL; CREATE TABLE fixture (id INTEGER); INSERT INTO fixture VALUES (1);' >/dev/null
chmod 0555 /source
if sqlite3 -readonly /source/wal.db ".backup '/tmp/should-not-open.db'" 2>/dev/null; then
    echo 'Expected bare WAL source to reject writes in its source directory.' >&2
    exit 1
fi
mkdir -p /tmp/mock-bin
cat > /tmp/mock-bin/kubectl <<'MOCK'
#!/bin/bash
set -euo pipefail
while [[ $1 == --* ]]; do shift; done
printf '%s\n' "$*" >> /tmp/offline-operations
case "$*" in
    'get configmap '*) printf false ;;
    'get hpa '*) printf '{"items":[]}' ;;
    'get deployment fixture -o jsonpath='*) printf 1 ;;
    'get deployment fixture -o json'*) printf '{"spec":{"selector":{"matchLabels":{"app":"fixture"}}}}' ;;
    'get pods '*) printf '{"items":[]}' ;;
    'scale deployment fixture --replicas=0') printf 0 > /tmp/offline-replicas ;;
    'scale deployment fixture --replicas=1') printf 1 > /tmp/offline-replicas ;;
    'patch configmap '*) : ;;
    *) exit 1 ;;
esac
MOCK
cat > /tmp/mock-bin/cp <<'MOCK'
#!/bin/bash
if [[ ${FIXTURE_FAIL_COPY:-false} == true && $* == *'/source/wal.db'* ]]; then
    exit 1
fi
exec /bin/cp "$@"
MOCK
chmod 0755 /tmp/mock-bin/*
original_path=$PATH
export PATH="/tmp/mock-bin:$PATH" KUBECONFIG=/tmp/mock-config POD_NAMESPACE=fixture
export SERVICE=offlinetest SQLITE_FILE=/source/wal.db SQLITE_BACKUP_NAME=sqlite.db
export APPLICATION=fixture STATE_CONFIGMAP=fixture-backups-state
cp /usr/local/lib/backup/captures/sqlite-offline.sh /scripts/capture.sh
before=$(sha256sum /source/wal.db)
bash /usr/local/lib/backup/run.sh >/tmp/offline.log
[[ $(cat /tmp/offline-replicas) == 1 ]]
[[ $before == "$(sha256sum /source/wal.db)" ]]
[[ ! -e /source/wal.db-wal && ! -e /source/wal.db-shm ]]
archive=$(find /published -name 'offlinetest-*.tar.gz.age' -print -quit)
age -d -i /tmp/identity -o /tmp/offline.tar.gz "$archive"
mkdir /tmp/offline-restored
tar -xzf /tmp/offline.tar.gz -C /tmp/offline-restored
[[ $(sqlite3 /tmp/offline-restored/sqlite.db 'PRAGMA integrity_check;') == ok ]]
[[ $(sqlite3 /tmp/offline-restored/sqlite.db 'SELECT COUNT(*) FROM fixture;') == 1 ]]
export FIXTURE_FAIL_COPY=true
if bash /usr/local/lib/backup/run.sh >/tmp/offline-failed.log 2>&1; then exit 1; fi
[[ $(cat /tmp/offline-replicas) == 1 ]]
[[ ! -e /published/.backup-lock ]]
unset FIXTURE_FAIL_COPY

# Crash-style WAL fixture: committed rows remain solely in a WAL after exit.
chmod 0755 /source
python3 - <<'FIXTURE'
import os,sqlite3
connection=sqlite3.connect('/source/wal.db')
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute('INSERT INTO fixture VALUES (2)')
connection.commit()
os._exit(0)
FIXTURE
chmod 0555 /source
[[ -f /source/wal.db-wal ]]
cp /source/wal.db-wal /tmp/original-wal
export SERVICE=waltest
bash /usr/local/lib/backup/run.sh >/tmp/wal.log
cmp /source/wal.db-wal /tmp/original-wal
[[ $(cat /tmp/offline-replicas) == 1 ]]
archive=$(find /published -name 'waltest-*.tar.gz.age' -print -quit)
age -d -i /tmp/identity -o /tmp/wal.tar.gz "$archive"
mkdir /tmp/wal-restored
tar -xzf /tmp/wal.tar.gz -C /tmp/wal-restored
[[ $(sqlite3 /tmp/wal-restored/sqlite.db 'PRAGMA integrity_check;') == ok ]]
[[ $(sqlite3 /tmp/wal-restored/sqlite.db 'SELECT COUNT(*) FROM fixture;') == 2 ]]

# Kill the real engine after its persisted pause: EXIT cannot run on SIGKILL.
# The independent recovery must refuse the live capture, then resume it once
# the API reports its pod terminal, without touching the abandoned volume lock.
cat > /tmp/mock-bin/kubectl <<'MOCK'
#!/usr/bin/env python3
import json,sys
from pathlib import Path
args=[a for a in sys.argv[1:] if not a.startswith('--kubeconfig=')
      and not a.startswith('--namespace=') and not a.startswith('--request-timeout=')]
path=Path('/tmp/recovery-state')
state=json.loads(path.read_text())
result={}
if args[:2] == ['get','configmap']:
    result=state if args[-1]=='json' else state['data']['paused']
elif args[:2] == ['get','hpa']:
    result={'items':[]}
elif args[:2] == ['get','deployment']:
    result={'spec':{'selector':{'matchLabels':{'app':'fixture'}}}} if args[-1]=='json' else Path('/tmp/recovery-replicas').read_text()
elif args[:2] == ['get','pods']:
    result=json.loads(Path('/tmp/recovery-pods').read_text()) if 'backup-job' in args[3] else {'items':[]}
elif args[:2] == ['patch','configmap']:
    patch=json.loads(args[-1])
    if '--type=json' in args:
        assert patch[0]['value']==state['metadata']['resourceVersion']
        state['data']['paused']=patch[1]['value']
    else:
        state['data'].update(patch['data'])
    state['metadata']['resourceVersion']+='1'
    path.write_text(json.dumps(state))
elif args[:2] == ['scale','deployment']:
    Path('/tmp/recovery-replicas').write_text(args[-1].split('=')[1])
else:
    raise AssertionError(args)
print(json.dumps(result) if isinstance(result,dict) else result)
MOCK
chmod 0755 /tmp/mock-bin/kubectl
printf '{"metadata":{"resourceVersion":"1"},"data":{"paused":"false","replicas":""}}' > /tmp/recovery-state
printf '{"items":[{"status":{"phase":"Running"}}]}' > /tmp/recovery-pods
printf 1 > /tmp/recovery-replicas
cat > /scripts/capture.sh <<'CAPTURE'
source /usr/local/lib/backup/deployment.sh
prepare_backup() {
    pause_deployment
    touch /tmp/capture-paused
    # No child is left behind when the test kills this process.
    while :; do :; done
}
resume_application() { resume_deployment; }
CAPTURE
export SERVICE=crashtest RECOVERY_KIND=deployment
bash /usr/local/lib/backup/run.sh >/tmp/crash.log 2>&1 &
capture_pid=$!
for ((attempt=0; attempt<100; attempt++)); do
    [[ -f /tmp/capture-paused ]] && break
    sleep 0.05
done
[[ -f /tmp/capture-paused && $(cat /tmp/recovery-replicas) == 0 ]]
bash /usr/local/lib/backup/recover.sh >/tmp/recovery-active.log
[[ $(cat /tmp/recovery-replicas) == 0 ]]
kill -KILL "$capture_pid"
capture_status=0
wait "$capture_pid" || capture_status=$?
[[ $capture_status == 137 ]]
[[ $(jq -r '.data.paused' /tmp/recovery-state) == true ]]
[[ -d /published/.backup-lock ]]
printf '{"items":[{"status":{"phase":"Failed"}}]}' > /tmp/recovery-pods
bash /usr/local/lib/backup/recover.sh >/tmp/recovery-terminal.log
[[ $(cat /tmp/recovery-replicas) == 1 ]]
[[ $(jq -r '.data.paused' /tmp/recovery-state) == false ]]
[[ -d /published/.backup-lock ]]
if bash /usr/local/lib/backup/run.sh >/tmp/recovery-locked.log 2>&1; then exit 1; fi
rmdir /published/.backup-lock

# Exercise the actual kubectl binary and engine helper against an isolated fake
# API: no Kubernetes environment or real token is available to fall back to.
export PATH=$original_path
cat > /tmp/api.py <<'API'
import http.server,json,ssl
from pathlib import Path
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get('Authorization') != 'Bearer synthetic-only':
            self.send_error(401)
            return
        if self.path.split('?', 1)[0] != '/api/v1/namespaces/fixture/configmaps/fixture':
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'data':{'paused':'false'}}).encode())
    def log_message(self,*args):
        pass
server=http.server.HTTPServer(('127.0.0.1',0),Handler)
context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/fixture-tls/cert.pem','/fixture-tls/key.pem')
server.socket=context.wrap_socket(server.socket,server_side=True)
Path('/tmp/api-port').write_text(str(server.server_port))
server.serve_forever()
API
python3 /tmp/api.py &
api_pid=$!
trap 'kill "$api_pid" 2>/dev/null || true' EXIT
for ((attempt=0; attempt<100; attempt++)); do
    [[ -s /tmp/api-port ]] && break
    sleep 0.05
done
port=$(cat /tmp/api-port)
printf 'synthetic-only' > /tmp/api-token
cat > /tmp/api-config.yaml <<CONFIG
apiVersion: v1
kind: Config
clusters:
- name: fixture
  cluster:
    server: https://127.0.0.1:$port
    certificate-authority: /fixture-tls/cert.pem
users:
- name: fixture
  user:
    tokenFile: /tmp/api-token
contexts:
- name: fixture
  context:
    cluster: fixture
    user: fixture
current-context: fixture
CONFIG
cat > /scripts/capture.sh <<'CAPTURE'
prepare_backup() {
    k get --raw=/api/v1/namespaces/fixture/configmaps/fixture | jq -e '.data.paused == "false"' >/dev/null
}
CAPTURE
export SERVICE=apitest KUBECONFIG=/tmp/api-config.yaml
bash /usr/local/lib/backup/run.sh >/tmp/api.log
[[ -n $(find /published -name 'apitest-*.tar.gz.age' -print -quit) ]]
printf 'Synthetic runtime checks passed.\n'
