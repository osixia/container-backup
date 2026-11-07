"""Exercise the real recovery shell script against a disposable fake API."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

KUBECTL = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
path = Path(os.environ['FAKE_API'])
fixture = json.loads(path.read_text())
args = [a for a in sys.argv[1:] if not a.startswith('--kubeconfig=')
        and not a.startswith('--namespace=') and not a.startswith('--request-timeout=')]
fixture['calls'].append(args)
def save():
    path.write_text(json.dumps(fixture))
def fail():
    save()
    sys.exit(1)
if args[:2] == ['get', 'configmap']:
    result = fixture['state']
elif args[:2] == ['get', 'pods']:
    if fixture.get('fail') == 'pods':
        fail()
    result = {'items': fixture['pods']}
    if fixture.get('conflict'):
        fixture['state']['metadata']['resourceVersion'] = 'changed'
        fixture['state']['data']['paused'] = 'false'
elif args[:2] == ['patch', 'configmap']:
    patch = json.loads(args[-1])
    if '--type=json' in args:
        if patch[0]['value'] != fixture['state']['metadata']['resourceVersion']:
            fail()
        fixture['state']['data']['paused'] = patch[1]['value']
    else:
        if fixture.get('fail') == 'clear':
            fail()
        fixture['state']['data'].update(patch['data'])
    fixture['state']['metadata']['resourceVersion'] += '1'
    result = {}
elif args[0] == 'scale':
    if fixture.get('fail') == 'scale':
        fail()
    fixture['replicas'] = int(args[-1].split('=')[1])
    fixture['resumed_kind'] = args[1]
    result = {}
elif args[:2] == ['get', 'daemonset']:
    result = {'spec': {'template': {'spec': {'nodeSelector': fixture['nodeSelector']}}}}
elif args[:2] == ['patch', 'daemonset']:
    if fixture.get('fail') == 'daemonset':
        fail()
    patch = json.loads(args[-1])
    assert patch == [{'op': 'remove', 'path': '/spec/template/spec/nodeSelector/backup.osixia.net~1paused'}]
    del fixture['nodeSelector']['backup.osixia.net/paused']
    result = {}
else:
    raise AssertionError(args)
save()
print(json.dumps(result))
'''


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.api = self.directory / 'api.json'
        client = self.directory / 'kubectl'
        client.write_text(KUBECTL)
        client.chmod(0o755)
        self.fixture = {
            'state': {'metadata': {'resourceVersion': '42'},
                      'data': {'paused': 'true', 'replicas': '2'}},
            'pods': [{'status': {'phase': 'Failed', 'containerStatuses': [
                {'state': {'terminated': {'reason': 'OOMKilled', 'exitCode': 137}}}]}}],
            'replicas': 0,
            'nodeSelector': {'placement': 'mail-node', 'backup.osixia.net/paused': 'synthetic-uuid'},
            'calls': [],
        }

    def run_recovery(self, kind='deployment', fixture=None):
        self.api.write_text(json.dumps(self.fixture if fixture is None else fixture))
        result = subprocess.run(['bash', str(ROOT / 'runtime/recover.sh')],
                                capture_output=True, text=True, env={
            **os.environ, 'PATH': str(self.directory) + os.pathsep + os.environ['PATH'],
            'FAKE_API': str(self.api), 'SERVICE': 'fixture', 'APPLICATION': 'writers',
            'STATE_CONFIGMAP': 'fixture-backups-state', 'RECOVERY_KIND': kind,
            'POD_NAMESPACE': 'synthetic', 'KUBECONFIG': '/synthetic/kubeconfig',
        })
        return result, json.loads(self.api.read_text())

    def assert_untouched(self, actual):
        self.assertEqual(actual['replicas'], 0)
        self.assertEqual(actual['state']['data']['paused'], 'true')
        self.assertFalse(any(call[0] in {'patch', 'scale'} for call in actual['calls']))

    def test_oom_and_sigkill_resume_saved_replicas(self):
        for kind in ('deployment', 'statefulset'):
            with self.subTest(kind=kind):
                result, actual = self.run_recovery(kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(actual['replicas'], 2)
                self.assertEqual(actual['resumed_kind'], kind)
                self.assertEqual(actual['state']['data']['paused'], 'false')
                claim = next(call for call in actual['calls'] if call[0] == 'patch')
                self.assertEqual(json.loads(claim[-1])[0], {
                    'op': 'test', 'path': '/metadata/resourceVersion', 'value': '42'})
                pod_query = next(call for call in actual['calls'] if call[:2] == ['get', 'pods'])
                self.assertEqual(pod_query[3], 'app.kubernetes.io/name=backup-job,backup.osixia.net/service=fixture')

    def test_no_pods_after_garbage_collection_can_resume(self):
        self.fixture['pods'] = []
        result, actual = self.run_recovery()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actual['replicas'], 2)

    def test_succeeded_capture_with_failed_cleanup_can_resume(self):
        self.fixture['pods'] = [{'status': {'phase': 'Succeeded'}}]
        result, actual = self.run_recovery()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actual['replicas'], 2)

    def test_active_unknown_and_terminating_pods_block_recovery(self):
        for phase in ('Pending', 'Running', 'Unknown', None):
            with self.subTest(phase=phase):
                self.fixture['pods'].append({'metadata': {'deletionTimestamp': 'synthetic'},
                                             'status': {} if phase is None else {'phase': phase}})
                result, actual = self.run_recovery()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_untouched(actual)
                self.fixture['pods'].pop()

    def test_api_failure_does_not_authorize_recovery(self):
        self.fixture['fail'] = 'pods'
        result, actual = self.run_recovery()
        self.assertNotEqual(result.returncode, 0)
        self.assert_untouched(actual)

    def test_changed_snapshot_aborts_recovery(self):
        self.fixture['conflict'] = True
        result, actual = self.run_recovery()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(actual['replicas'], 0)
        self.assertFalse(any(call[0] == 'scale' for call in actual['calls']))

    def test_no_pause_means_no_work_and_repeated_recovery_is_idempotent(self):
        result, actual = self.run_recovery()
        self.assertEqual(result.returncode, 0, result.stderr)
        actual['calls'] = []
        result, actual = self.run_recovery(fixture=actual)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[:2] for call in actual['calls']], [['get', 'configmap']])

    def test_bad_replicas_and_states_fail_closed(self):
        for field, values in (('replicas', ('', '0', '-1', 'bad', '1;exit')),
                              ('paused', ('', 'bad'))):
            for value in values:
                with self.subTest(field=field, value=value):
                    fixture = copy.deepcopy(self.fixture)
                    fixture['state']['data'][field] = value
                    result, actual = self.run_recovery(fixture=fixture)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(any(call[0] in {'patch', 'scale'} for call in actual['calls']))

    def test_failed_scale_or_state_clear_retries_from_recovering(self):
        for failure in ('scale', 'clear'):
            with self.subTest(failure=failure):
                self.fixture['fail'] = failure
                result, actual = self.run_recovery()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(actual['state']['data']['paused'], 'recovering')
                del actual['fail']
                result, actual = self.run_recovery(fixture=actual)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(actual['replicas'], 2)
                self.assertEqual(actual['state']['data']['paused'], 'false')

    def test_mail_preserves_placement_and_handles_already_resumed(self):
        for paused in (True, False):
            with self.subTest(selector_present=paused):
                fixture = copy.deepcopy(self.fixture)
                if not paused:
                    del fixture['nodeSelector']['backup.osixia.net/paused']
                result, actual = self.run_recovery('daemonset', fixture)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(actual['nodeSelector'], {'placement': 'mail-node'})
                self.assertEqual(actual['state']['data']['paused'], 'false')
                self.assertFalse(any(call[0] == 'scale' for call in actual['calls']))

    def test_failed_mail_patch_preserves_recovery_state(self):
        self.fixture['fail'] = 'daemonset'
        result, actual = self.run_recovery('daemonset')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(actual['state']['data']['paused'], 'recovering')
        self.assertIn('backup.osixia.net/paused', actual['nodeSelector'])


if __name__ == '__main__':
    unittest.main()
