import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('generate', ROOT / 'tools/generate.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class GeneratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backup = Path(self.tmp.name) / 'backup'
        shutil.copytree(ROOT / 'examples/project/backup', self.backup, ignore=shutil.ignore_patterns('generated'))
        self.path = self.backup / 'config.yaml'
        self.config = yaml.safe_load(self.path.read_text())

    def generate(self):
        self.path.write_text(yaml.safe_dump(self.config, sort_keys=False))
        return generator.generate(self.path)

    def build(self, output):
        result = subprocess.run(['kustomize', 'build', str(output)], check=True, capture_output=True, text=True)
        return list(yaml.safe_load_all(result.stdout))

    def test_two_services_same_namespace_and_repeat(self):
        self.config['services']['other'] = copy.deepcopy(self.config['services']['portal'])
        output = self.generate()
        before = {str(p.relative_to(output)): p.read_bytes() for p in output.rglob('*') if p.is_file()}
        self.generate()
        self.assertEqual(before, {str(p.relative_to(output)): p.read_bytes() for p in output.rglob('*') if p.is_file()})
        docs = self.build(output)
        jobs = [d for d in docs if d['kind'] == 'CronJob']
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            self.assertTrue(job['spec']['suspend'])
            pod = job['spec']['jobTemplate']['spec']['template']['spec']
            self.assertEqual(pod['containers'][0]['command'], ['bash', '/usr/local/lib/backup/run.sh'])
            refs = [v['configMap']['name'] for v in pod['volumes'] if 'configMap' in v]
            names = {d['metadata']['name'] for d in docs if d['kind'] == 'ConfigMap'}
            self.assertEqual(len([ref for ref in refs if ref in names]), 1)  # recipient is operator supplied
        exports = [d for d in docs if d['kind'] == 'Service']
        self.assertNotEqual(exports[0]['spec']['selector'], exports[1]['spec']['selector'])

    def test_edits_refused_before_writes(self):
        output = self.generate()
        target = output / 'portal/capture.sh'
        target.write_text('manual edit\n')
        with self.assertRaises(ValueError):
            self.generate()
        self.assertEqual(target.read_text(), 'manual edit\n')

    def test_removed_services_and_all_profiles(self):
        for profile in sorted(generator.PROFILES):
            self.config['services'][profile.replace('-', '')] = {'profile': profile, 'namespace': 'example', 'settings': ({'APPLICATION': 'workload'} if profile in {'mail', 'nextcloud', 'sqlite-offline', 'vaultwarden', 'statefulset-offline'} else {'GITLAB_BACKUP_CRONJOB': 'gitlab-toolbox-backup'} if profile == 'gitlab' else {})}
        output = self.generate()
        self.assertEqual(len([d for d in self.build(output) if d['kind'] == 'CronJob']), len(generator.PROFILES) + 1)
        docs = self.build(output)
        for profile in ('mail', 'nextcloud', 'sqliteoffline'):
            role = next(d for d in docs if d['kind'] == 'Role' and d['metadata']['name'] == profile + '-backups-control')
            self.assertTrue(any(rule.get('resourceNames') == ['workload'] for rule in role['rules']))
            self.assertTrue(any(rule.get('resourceNames') == [profile + '-backups-state'] for rule in role['rules']))
        del self.config['services']['mail']
        self.generate()
        self.assertFalse((output / 'mail/capture.sh').exists())
        self.assertEqual(len([d for d in self.build(output) if d['kind'] == 'CronJob']), len(generator.PROFILES))

    def test_controlled_profiles_use_explicit_service_account_configuration(self):
        for profile in ('mail', 'nextcloud', 'sqlite-offline', 'vaultwarden'):
            self.config['services'] = {'fixture': {'profile': profile, 'namespace': 'example', 'settings': {'APPLICATION': 'workload'}}}
            docs = self.build(self.generate())
            job = next(d for d in docs if d['kind'] == 'CronJob')
            pod = job['spec']['jobTemplate']['spec']['template']['spec']
            self.assertTrue(pod['automountServiceAccountToken'])
            container = pod['containers'][0]
            self.assertIn({'name': 'KUBECONFIG', 'value': '/kube/config'}, container['env'])
            volume = next(v for v in pod['volumes'] if v['name'] == 'kubeconfig')
            configmap = next(d for d in docs if d['kind'] == 'ConfigMap' and d['metadata']['name'] == volume['configMap']['name'])
            client = yaml.safe_load(configmap['data']['config'])
            self.assertEqual(client['clusters'][0]['cluster']['server'], 'https://kubernetes.default.svc')
            self.assertEqual(client['users'][0]['user'], {'tokenFile': '/var/run/secrets/kubernetes.io/serviceaccount/token'})


    def test_gitlab_capture_has_only_read_permissions(self):
        self.config['services'] = {'gitlab': {'profile': 'gitlab', 'namespace': 'example', 'settings': {'GITLAB_BACKUP_CRONJOB': 'gitlab-toolbox-backup'}}}
        docs = self.build(self.generate())
        role = next(d for d in docs if d['kind'] == 'Role')
        for rule in role['rules']:
            self.assertLessEqual(set(rule['verbs']), {'get', 'list'})
            self.assertLessEqual(set(rule['resources']), {'cronjobs', 'jobs'})
        self.assertTrue(any(rule.get('resourceNames') == ['gitlab-toolbox-backup'] for rule in role['rules']))
        self.assertFalse(any(d['kind'] == 'ConfigMap' and 'backups-state' in d['metadata']['name'] for d in docs))

    def coverage_config(self):
        return {'profile': 'gitlab', 'namespace': 'another-project', 'settings': {
            'GITLAB_BACKUP_CRONJOB': 'my-native-backup',
            'GITLAB_TOOLBOX_IMAGE': 'registry.example/toolbox:19.5.3',
            'GITLAB_NATIVE_PVC': 'another-native-data',
            'GITLAB_S3_CONFIG_SECRET': 'another-s3-config',
            'GITLAB_S3_CONFIG_KEY': 's3cfg',
            'GITLAB_OBJECT_BUCKETS': json.dumps({name: 'another-' + name for name in ('registry', 'artifacts', 'lfs', 'uploads', 'packages')})}}

    def test_gitlab_coverage_is_reusable_and_has_no_extra_api_permissions(self):
        self.config['services'] = {'code': self.coverage_config()}
        docs = self.build(self.generate())
        pod = next(d for d in docs if d['kind'] == 'CronJob')['spec']['jobTemplate']['spec']['template']['spec']
        init = pod['initContainers'][0]
        self.assertEqual(init['image'], 'registry.example/toolbox:19.5.3')
        self.assertEqual(next(v for v in pod['volumes'] if v['name'] == 'native')['persistentVolumeClaim'], {'claimName': 'another-native-data', 'readOnly': True})
        secret = next(v for v in pod['volumes'] if v['name'] == 'gitlab-s3-config')['secret']
        self.assertEqual(secret['secretName'], 'another-s3-config')
        self.assertEqual(secret['items'], [{'key': 's3cfg', 'path': 'config'}])
        configs = {d['metadata']['name'] for d in docs if d['kind'] == 'ConfigMap'}
        for volume in pod['volumes']:
            if 'configMap' in volume and volume['name'] != 'recipient':
                self.assertIn(volume['configMap']['name'], configs)
        roles = [d for d in docs if d['kind'] == 'Role']
        self.assertTrue(all(set(rule['resources']) <= {'cronjobs', 'jobs'} for role in roles for rule in role['rules']))
        self.assertIn('coverage.py', next(d for d in docs if d['kind'] == 'ConfigMap' and 'coverage' in d['metadata']['name'])['data'])
        before = (self.backup / 'generated/.generated.json').read_bytes()
        self.generate()
        self.assertEqual(before, (self.backup / 'generated/.generated.json').read_bytes())

    def test_partial_gitlab_coverage_settings_are_refused(self):
        config = self.coverage_config()
        for key in ('GITLAB_OBJECT_BUCKETS', 'GITLAB_TOOLBOX_IMAGE', 'GITLAB_NATIVE_PVC', 'GITLAB_S3_CONFIG_SECRET'):
            self.config['services'] = {'code': copy.deepcopy(config)}
            del self.config['services']['code']['settings'][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.generate()

    def test_additional_gitlab_object_bucket_is_supported(self):
        config = self.coverage_config()
        buckets = json.loads(config['settings']['GITLAB_OBJECT_BUCKETS'])
        buckets['terraform_state'] = 'another-terraform-state'
        config['settings']['GITLAB_OBJECT_BUCKETS'] = json.dumps(buckets)
        self.config['services'] = {'code': config}
        self.build(self.generate())

    def test_digest_with_registry_port(self):
        self.config['image'] = 'registry.example:5000/backup@sha256:' + 'a' * 64
        docs = self.build(self.generate())
        job = next(d for d in docs if d['kind'] == 'CronJob')
        self.assertEqual(job['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['image'], self.config['image'])

    def test_project_capture_survives_regeneration(self):
        custom = b'#!/bin/bash\nprepare_backup() { :; }\n'
        (self.backup / 'custom.sh').write_bytes(custom)
        self.config['services']['portal']['capture'] = 'custom.sh'
        output = self.generate()
        self.generate()
        self.assertEqual((output / 'portal/capture.sh').read_bytes(), custom)

    def test_invalid_input_does_not_create_output(self):
        self.config['services']['portal']['profile'] = 'unknown'
        with self.assertRaises(ValueError):
            self.generate()
        self.assertFalse((self.backup / 'generated').exists())

    def test_foreign_files_and_symlinks_refused(self):
        output = self.generate()
        extra = output / 'extra'
        extra.write_text('user file')
        with self.assertRaises(ValueError):
            self.generate()
        extra.unlink()
        extra.symlink_to(self.path)
        with self.assertRaises(ValueError):
            self.generate()


if __name__ == '__main__':
    unittest.main()
