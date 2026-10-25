import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from collector.locking import WriterBusy
from collector.policy import PolicyGate
from collector.storage import CorruptStore, StorageEngine
from collector.validation import ValidationError, check, strict_loads


ROOT = Path(__file__).resolve().parents[1]


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'data'
        self.fixture = json.loads((ROOT / 'fixtures/offline.json').read_text(encoding='utf-8'))
        self.account = self.fixture['account']
        self.post = self.fixture['post']
        self.context = self.fixture['context']
        self.gate = PolicyGate(self.fixture['policies'])

    def ingest(self, store, posts=None, checkpoint=None, gate=None):
        return store.ingest([self.post] if posts is None else posts, self.account,
                            gate or self.gate, self.context, checkpoint)

    def checkpoint(self):
        return {'account_id': self.account['account_id'], 'previous_cursor': None,
                'requested_window': '2026-09-24/2026-10-24', 'overlap_start': '2026-10-22T00:00:00Z',
                'candidate_cursor': self.post['post_id'], 'committed_cursor': self.post['post_id'],
                'coverage_status': 'UNKNOWN', 'coverage_reason': 'offline-no-coverage-evidence',
                'last_success_at': '2026-10-24T13:00:00Z'}

    def test_T08_replay_three_times_and_restart(self):
        for _ in range(3):
            with StorageEngine(self.root) as store:
                self.ingest(store)
                self.assertEqual(store.status()['posts'], 1)
                self.assertEqual(len(store.history(self.post['post_id'])), 1)
        with StorageEngine(self.root) as store:
            self.assertEqual(store.status()['events'], 1)

    def test_revision_history_and_observation_does_not_revise(self):
        with StorageEngine(self.root) as store:
            self.ingest(store)
            changed = copy.deepcopy(self.post)
            changed['text'] += ' 第二版'
            self.assertEqual(self.ingest(store, [changed])['revised'], 1)
            changed['observed_at'] = '2026-10-25T00:00:00Z'
            changed['metrics'] = {'likes': 99}
            self.assertEqual(self.ingest(store, [changed])['duplicate'], 1)
        with StorageEngine(self.root) as store:
            versions = store.history(self.post['post_id'])
            self.assertEqual([versions[0]['version_number'], versions[1]['version_number']], [1, 2])
            self.assertEqual(len(store.current_posts()), 1)
            self.assertEqual(versions[0]['first_seen_at'], versions[1]['first_seen_at'])

    def test_T03_malformed_items_quarantine_only_sanitized_diagnostics(self):
        invalid = []
        for field, value in (('post_id', 123), ('post_id', '123\n'), ('published_at', 'bad-secret-date'),
                             ('author_external_id', '999'), ('source_url', 'https://127.0.0.1'),
                             ('text', {'token': 'sensitive-fixture-marker'}), ('media_refs', 'drift')):
            item = copy.deepcopy(self.post)
            item[field] = value
            invalid.append(item)
        missing = copy.deepcopy(self.post)
        del missing['post_id']
        invalid.extend([missing, None, 'untrusted-secret-string'])
        with StorageEngine(self.root) as store:
            result = self.ingest(store, [self.post] + invalid)
            self.assertEqual(result['inserted'], 1)
            self.assertEqual(result['quarantined'], len(invalid))
            self.assertEqual(store.status()['posts'], 1)
        for path in (self.root / 'journal/segments').glob('*.jsonl'):
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('sensitive-fixture-marker', text)
            self.assertNotIn('untrusted-secret-string', text)

    def test_duplicate_in_same_batch_and_revision(self):
        changed = dict(self.post)
        changed['text'] = 'changed'
        with StorageEngine(self.root) as store:
            result = self.ingest(store, [self.post, self.post, changed, changed])
            self.assertEqual((result['inserted'], result['revised'], result['duplicate']), (1, 1, 2))

    def test_post_id_ownership_cannot_change(self):
        with StorageEngine(self.root) as store:
            self.ingest(store)
            changed = dict(self.post)
            changed['published_at'] = '2026-10-23T12:00:00Z'
            self.assertEqual(self.ingest(store, [changed])['quarantined'], 1)
            self.assertEqual(len(store.history(self.post['post_id'])), 1)

    def test_tombstone_survives_replay_and_blocks_resurrection(self):
        with StorageEngine(self.root) as store:
            self.ingest(store)
            store.tombstone(self.post['post_id'], self.context)
            self.assertEqual(store.status()['posts'], 0)
            self.assertIsNone(store.tombstone(self.post['post_id'], self.context))
        with StorageEngine(self.root) as store:
            self.assertEqual(self.ingest(store)['quarantined'], 1)
            self.assertEqual(store.status()['posts'], 0)
            self.assertEqual(store.status()['tombstones'], 1)
            self.assertEqual(len(store.history(self.post['post_id'])), 1)

    def test_checkpoint_and_posts_commit_together(self):
        with StorageEngine(self.root) as store:
            self.ingest(store, checkpoint=self.checkpoint())
        with StorageEngine(self.root) as store:
            self.assertEqual(store.checkpoint(self.account['account_id'])['committed_cursor'], self.post['post_id'])
            self.assertEqual(store.status()['posts'], 1)

    def test_quarantined_batch_cannot_advance_checkpoint(self):
        with StorageEngine(self.root) as store:
            with self.assertRaisesRegex(ValueError, 'CHECKPOINT'):
                self.ingest(store, [self.post, {}], checkpoint=self.checkpoint())
            self.assertIsNone(store.checkpoint(self.account['account_id']))
            self.assertEqual(store.status()['events'], 0)

    def test_T09_crash_at_each_boundary_and_cursor_consistency(self):
        for stage in ('AFTER_PENDING', 'AFTER_SEGMENT', 'AFTER_MANIFEST', 'AFTER_POINTER'):
            with self.subTest(stage=stage):
                root = self.root / stage
                with StorageEngine(root) as store:
                    self.ingest(store)
                changed = dict(self.post)
                changed['text'] = 'new-version'

                def fail(current):
                    if current == stage:
                        raise OSError('INJECTED')

                with StorageEngine(root, fault=fail) as store:
                    with self.assertRaises(OSError):
                        self.ingest(store, [changed], checkpoint=self.checkpoint())
                    with self.assertRaises(CorruptStore):
                        self.ingest(store)
                with StorageEngine(root) as store:
                    committed = stage == 'AFTER_POINTER'
                    self.assertEqual(len(store.history(self.post['post_id'])), 2 if committed else 1)
                    self.assertEqual(store.checkpoint(self.account['account_id']) is not None, committed)

    def test_process_killed_without_cleanup_releases_lock(self):
        # A real process exit, rather than an exception/context-manager cleanup.
        code = "from collector.storage import StorageEngine; import os,sys; s=StorageEngine(sys.argv[1]); os._exit(23)"
        process = subprocess.run([sys.executable, '-c', code, str(self.root)], cwd=ROOT, capture_output=True)
        self.assertEqual(process.returncode, 23)
        with StorageEngine(self.root) as store:
            self.assertEqual(self.ingest(store)['inserted'], 1)

    def test_T09_real_process_exit_at_all_commit_boundaries(self):
        for stage in ('AFTER_PENDING', 'AFTER_SEGMENT', 'AFTER_MANIFEST', 'AFTER_POINTER'):
            with self.subTest(stage=stage):
                root = self.root / stage
                with StorageEngine(root) as store:
                    self.ingest(store)
                process = subprocess.run([sys.executable, str(ROOT / 'tests/crash_writer.py'), str(root), stage],
                                         cwd=ROOT, capture_output=True)
                self.assertEqual(process.returncode, 37, process.stderr)
                with StorageEngine(root) as store:
                    self.assertEqual(len(store.history(self.post['post_id'])), 2 if stage == 'AFTER_POINTER' else 1)
                    self.assertEqual(store.checkpoint(self.account['account_id']) is not None, stage == 'AFTER_POINTER')

    def test_first_commit_crash_has_no_phantom_posts(self):
        for stage in ('AFTER_PENDING', 'AFTER_SEGMENT', 'AFTER_MANIFEST'):
            with self.subTest(stage=stage):
                root = self.root / stage
                process = subprocess.run([sys.executable, str(ROOT / 'tests/crash_writer.py'), str(root), stage],
                                         cwd=ROOT, capture_output=True)
                self.assertEqual(process.returncode, 37)
                with StorageEngine(root) as store:
                    self.assertEqual(store.status()['posts'], 0)
                    self.assertIsNone(store.checkpoint(self.account['account_id']))

    def test_history_current_flag_is_derived(self):
        with StorageEngine(self.root) as store:
            self.ingest(store)
            changed = dict(self.post)
            changed['text'] = 'revision'
            self.ingest(store, [changed])
            history = store.history(self.post['post_id'])
            self.assertFalse(history[0]['is_current'])
            self.assertTrue(history[1]['is_current'])
            store.tombstone(self.post['post_id'], self.context)
            self.assertFalse(store.history(self.post['post_id'])[1]['is_current'])

    def test_source_policy_binding_is_enforced(self):
        self.account['source_policy_id'] = 'another-policy'
        with StorageEngine(self.root) as store:
            with self.assertRaises(PermissionError):
                self.ingest(store)

    def test_cross_process_single_writer(self):
        code = "from collector.storage import StorageEngine; import sys; StorageEngine(sys.argv[1])"
        with StorageEngine(self.root):
            result = subprocess.run([sys.executable, '-c', code, str(self.root)], cwd=ROOT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'WRITER_ALREADY_RUNNING', result.stderr)
            with self.assertRaises(WriterBusy):
                StorageEngine(self.root)

    def test_corrupt_segment_refuses_recovery(self):
        with StorageEngine(self.root) as store:
            self.ingest(store)
        path = next((self.root / 'journal/segments').glob('*.jsonl'))
        path.write_bytes(path.read_bytes()[:-10])
        with self.assertRaises(CorruptStore):
            StorageEngine(self.root)

    def test_corrupt_manifest_refuses_recovery(self):
        with StorageEngine(self.root) as store:
            self.ingest(store)
        path = next((self.root / 'journal/manifests').glob('*.json'))
        path.write_bytes(b'{}')
        with self.assertRaises(CorruptStore):
            StorageEngine(self.root)

    def test_corrupt_pointer_refuses_recovery(self):
        with StorageEngine(self.root) as store:
            self.ingest(store)
        (self.root / 'journal/CURRENT.json').write_bytes(b'{')
        with self.assertRaises(CorruptStore):
            StorageEngine(self.root)

    def test_disposable_index_is_rebuilt(self):
        with StorageEngine(self.root) as store:
            self.ingest(store)
        (self.root / 'indexes/current.json').write_bytes(b'broken')
        with StorageEngine(self.root) as store:
            index = json.loads((self.root / 'indexes/current.json').read_bytes())
            self.assertEqual(len(index['posts']), store.status()['posts'])

    def test_policy_four_operations_are_independent(self):
        for operation in ('access', 'download_media', 'redistribute'):
            with self.assertRaises(PermissionError):
                self.gate.require('x:101', operation, 'offline-fixture', 'mock-retain', 1)

    def test_policy_unknown_revoked_expired_and_wrong_scope_block(self):
        for field, value in (('permission_state', 'UNKNOWN'), ('permission_state', 'REVOKED'),
                             ('valid_until', '2020-01-01T00:00:00Z'), ('source_id', 'x:999'),
                             ('version', 2), ('purpose', 'redistribute')):
            policies = copy.deepcopy(self.fixture['policies'])
            policies[1][field] = value
            with StorageEngine(self.root) as store:
                with self.assertRaises(PermissionError):
                    self.ingest(store, gate=PolicyGate(policies))
                self.assertEqual(store.status()['events'], 0)

    def test_metadata_permission_does_not_include_text(self):
        policies = copy.deepcopy(self.fixture['policies'])
        policies[1]['content_scope'] = ['METADATA']
        with StorageEngine(self.root) as store:
            self.assertEqual(self.ingest(store, gate=PolicyGate(policies))['quarantined'], 1)
            item = dict(self.post)
            item['text'] = None
            self.assertEqual(self.ingest(store, [item], gate=PolicyGate(policies))['inserted'], 1)

    def test_rename_keeps_stable_identity(self):
        self.account['handle_history'] = ['mock_artist']
        self.account['current_handle'] = 'renamed_artist'
        with StorageEngine(self.root) as store:
            self.assertEqual(self.ingest(store)['inserted'], 1)

    def test_no_verified_coverage_without_evidence_in_M1(self):
        checkpoint = self.checkpoint()
        checkpoint['coverage_status'] = 'VERIFIED'
        with StorageEngine(self.root) as store:
            with self.assertRaises(ValidationError):
                self.ingest(store, checkpoint=checkpoint)

    def test_future_schema_and_duplicate_keys_fail_closed(self):
        config = json.loads((ROOT / 'config.example.json').read_bytes())
        config['schema_version'] = '2.0'
        with self.assertRaises(ValidationError):
            check('config', config)
        with self.assertRaises(ValidationError):
            strict_loads('{"schema_version":"1.0","schema_version":"2.0"}')

    def test_unknown_fields_do_not_leak_to_persistence(self):
        item = dict(self.post)
        item['APIFY_TOKEN'] = 'private-token-fixture'
        with StorageEngine(self.root) as store:
            self.assertEqual(self.ingest(store, [item])['quarantined'], 1)
        for path in (self.root / 'journal/segments').glob('*.jsonl'):
            self.assertNotIn('private-token-fixture', path.read_text())

    def test_post_views_cannot_mutate_engine(self):
        with StorageEngine(self.root) as store:
            self.ingest(store)
            view = store.current_posts()
            view.clear()
            self.assertEqual(len(store.current_posts()), 1)

    def test_symlink_root_or_segment_is_refused(self):
        if os.name == 'nt':
            self.skipTest('Windows symlink requires privileges; NTFS case tracked separately')
        target = Path(self.directory.name) / 'target'
        target.mkdir()
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            StorageEngine(self.root)


if __name__ == '__main__':
    unittest.main()
