import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gitlab_coverage', ROOT / 'kubernetes/profiles/gitlab/coverage.py')
coverage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage)


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / '1794000000_2026_11_06_19.5.3_gitlab_backup.tar'
        self.proof = self.root / 'proof.json'
        self.destination = self.root / 'validated.json'
        self.mapping = {name: 'synthetic-' + name for name in coverage.REQUIRED}
        self.env = patch.dict(os.environ, {'GITLAB_OBJECT_BUCKETS': json.dumps(self.mapping)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def make_archive(self, skipped=(), missing=(), contradiction=(), database=True, repositories=True):
        text = ':gitlab_version: 19.5.3\n:installation_type: gitlab-helm-chart\n:backup_id: ' + self.archive.name.removesuffix('_gitlab_backup.tar') + '\n:skipped: ' + ','.join(sorted(skipped)) + '\n'
        with tarfile.open(self.archive, 'w') as archive:
            def add(name, payload):
                member = tarfile.TarInfo('./' + name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            add('backup_information.yml', text.encode())
            if database:
                add('db/database.sql.gz', b'synthetic-only')
            if repositories:
                member = tarfile.TarInfo('./repositories')
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            for component in coverage.REQUIRED:
                if component not in missing and (component not in skipped or component in contradiction):
                    add(component + '.tar.gz', b'synthetic-only')

    def preflight(self, empty=True):
        with patch.object(coverage, 'bucket_empty', return_value=empty) as check:
            coverage.preflight(self.root, self.proof)
        return check

    def validate(self):
        coverage.validate(self.archive, self.proof, self.destination)

    def test_archived_components_do_not_require_s3_access(self):
        self.make_archive()
        check = self.preflight()
        check.assert_not_called()
        self.validate()
        self.assertEqual(set(json.loads(self.destination.read_text())['components'].values()), {'archived'})

    def test_empty_buckets_need_fresh_bound_proof(self):
        self.make_archive(skipped={'registry', 'artifacts', 'lfs', 'packages'})
        check = self.preflight()
        self.assertEqual(check.call_count, 4)
        self.validate()
        self.assertEqual(json.loads(self.destination.read_text())['components']['uploads'], 'archived')

    def test_skipped_nonempty_bucket_is_refused(self):
        self.make_archive(skipped={'registry'})
        with self.assertRaises(ValueError):
            self.preflight(empty=False)
        self.assertFalse(self.proof.exists())

    def test_s3_failure_is_not_empty(self):
        for status in (1, 5, 12):
            with self.subTest(status=status), patch.object(coverage.subprocess, 'run', return_value=subprocess.CompletedProcess([], status, stdout=b'', stderr=b'synthetic-sensitive-detail')):
                with self.assertRaises(ValueError) as error:
                    coverage.bucket_empty('synthetic-registry')
                self.assertNotIn('synthetic-sensitive-detail', str(error.exception))

    def test_s3_timeout_is_refused(self):
        with patch.object(coverage.subprocess, 'run', side_effect=subprocess.TimeoutExpired('synthetic', 60)):
            with self.assertRaises(subprocess.TimeoutExpired):
                coverage.bucket_empty('synthetic-registry')

    def test_s3_objects_or_prefixes_are_nonempty(self):
        for output in (b'object', b'DIR prefix/'):
            with patch.object(coverage.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=output, stderr=b'')):
                self.assertFalse(coverage.bucket_empty('synthetic-registry'))

    def test_empty_s3_stdout_ignores_unrelated_stderr_warnings(self):
        with patch.object(coverage.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=b'', stderr=b'synthetic-warning')):
            self.assertTrue(coverage.bucket_empty('synthetic-registry'))

    def test_unskipped_missing_payload_is_refused(self):
        self.make_archive(missing={'lfs'})
        with self.assertRaises(ValueError):
            self.preflight()

    def test_database_and_repositories_can_never_be_skipped(self):
        for component in ('db', 'repositories'):
            self.make_archive(skipped={component})
            with self.subTest(component=component), self.assertRaises(ValueError):
                self.preflight()

    def test_missing_database_or_repositories_is_refused(self):
        for kwargs in ({'database': False}, {'repositories': False}):
            self.make_archive(**kwargs)
            with self.assertRaises(ValueError):
                self.preflight()

    def test_contradictory_native_metadata_is_refused(self):
        self.make_archive(skipped={'registry'}, contradiction={'registry'})
        with self.assertRaises(ValueError):
            self.preflight()

    def test_stale_or_future_proof_is_refused(self):
        self.make_archive(skipped={'registry'})
        self.preflight()
        proof = json.loads(self.proof.read_text())
        for delta in (-301, 1):
            proof['verified_at'] = time.time() + delta
            self.proof.write_text(json.dumps(proof))
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                self.validate()

    def test_changed_archive_is_refused(self):
        self.make_archive()
        self.preflight()
        with self.archive.open('ab') as handle:
            handle.write(b'synthetic')
        with self.assertRaises(ValueError):
            self.validate()

    def test_changed_bucket_mapping_is_refused(self):
        self.make_archive(skipped={'registry'})
        self.preflight()
        self.mapping['registry'] = 'different-synthetic-registry'
        os.environ['GITLAB_OBJECT_BUCKETS'] = json.dumps(self.mapping)
        with self.assertRaises(ValueError):
            self.validate()

    def test_missing_or_contradictory_proof_is_refused(self):
        self.make_archive()
        with self.assertRaises(FileNotFoundError):
            self.validate()
        self.preflight()
        proof = json.loads(self.proof.read_text())
        proof['components']['registry'] = 'empty'
        self.proof.write_text(json.dumps(proof))
        with self.assertRaises(ValueError):
            self.validate()

    def test_hash_detects_same_size_change_with_restored_mtime(self):
        self.make_archive()
        self.preflight()
        info = self.archive.stat()
        with self.archive.open('r+b') as handle:
            handle.seek(-100, 2)
            handle.write(b'1')
        os.utime(self.archive, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaises(ValueError):
            self.validate()

    def test_additional_declared_component_is_checked(self):
        self.mapping['terraform_state'] = 'synthetic-terraform-state'
        os.environ['GITLAB_OBJECT_BUCKETS'] = json.dumps(self.mapping)
        self.make_archive(skipped={'terraform_state'})
        check = self.preflight()
        check.assert_called_once_with('synthetic-terraform-state')
        self.validate()

    def test_symlink_archive_is_refused(self):
        self.make_archive()
        original = self.root / 'original'
        self.archive.rename(original)
        self.archive.symlink_to(original)
        with self.assertRaises(ValueError):
            self.preflight()


if __name__ == '__main__':
    unittest.main()
