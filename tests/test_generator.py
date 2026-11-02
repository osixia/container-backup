import copy
import importlib.util
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
            self.config['services'][profile] = {'profile': profile, 'namespace': 'example', 'settings': {'APPLICATION': 'workload'} if profile in {'mail', 'nextcloud'} else {}}
        output = self.generate()
        self.assertEqual(len([d for d in self.build(output) if d['kind'] == 'CronJob']), 6)
        docs = self.build(output)
        for profile in ('mail', 'nextcloud'):
            role = next(d for d in docs if d['kind'] == 'Role' and d['metadata']['name'] == profile + '-backups-control')
            self.assertTrue(any(rule.get('resourceNames') == ['workload'] for rule in role['rules']))
            self.assertTrue(any(rule.get('resourceNames') == [profile + '-backups-state'] for rule in role['rules']))
        del self.config['services']['mail']
        self.generate()
        self.assertFalse((output / 'mail/capture.sh').exists())
        self.assertEqual(len([d for d in self.build(output) if d['kind'] == 'CronJob']), 5)

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
