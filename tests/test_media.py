import copy
import json
import shutil
import socket
import tempfile
import unittest
import base64
from pathlib import Path
from collector.deletion import DeletionManager
from collector.media import MediaManager
from collector.media_http import FakeMediaHTTP, MediaError, validate_url
from collector.policy import PolicyGate
from collector.quota import Quota, NoSpace, GiB
from collector.storage import StorageEngine, CorruptStore
from collector.validation import digest

ROOT = Path(__file__).resolve().parents[1]
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=')

class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'data'
        f=json.loads((ROOT/'fixtures/offline.json').read_bytes())
        self.account,self.context,self.post=f['account'],f['context'],f['post']
        self.policies=f['policies']
        for p in self.policies:
            if p['operation']!='redistribute':
                p['permission_state']='PERMITTED';p['content_scope']=['METADATA','TEXT','MEDIA']
        self.gate=PolicyGate(self.policies)
        self.http=FakeMediaHTTP(PNG)

    def setup_media(self, store, **options):
        store.ingest([self.post],self.account,self.gate,self.context)
        return MediaManager(store,self.gate,self.http,**options)

    def enqueue(self, manager, **options):
        defaults={'rating':'SFW','rating_evidence':'mock://manual-review','expires_at':'2090-01-01T00:00:00Z'}
        defaults.update(options)
        return manager.enqueue(self.post['post_id'],'https://pbs.twimg.com/media/mock.png',self.context,**defaults)

    def test_download_chunk_count_does_not_multiply_root_scans(self):
        from unittest.mock import patch
        import collector.quota as quota_module
        class TinyChunks:
            def stream(self, url):
                yield {'length':len(PNG), 'mime':'image/png'}
                for byte in PNG:yield bytes([byte])
        with StorageEngine(self.root) as store:
            manager = self.setup_media(store);mid = self.enqueue(manager)
            manager.transport = TinyChunks()
            with patch.object(quota_module.os, 'walk', wraps=quota_module.os.walk) as walk:
                self.assertEqual(manager.process(mid, self.context)['state'], 'DOWNLOADED')
                self.assertEqual(walk.call_count, 2)

    def test_quota_display_cache_never_authorizes_writes(self):
        from unittest.mock import patch
        import collector.quota as quota_module
        self.root.mkdir()
        now = [0.0]
        quota = Quota(self.root, limit=100000, free_probe=lambda:20*GiB, clock=lambda:now[0])
        with patch.object(quota_module.os, 'walk', wraps=quota_module.os.walk) as walk:
            for _ in range(20):self.assertEqual(quota.status()['used_bytes'], 0)
            self.assertEqual(walk.call_count, 1)
            (self.root / 'external').write_bytes(b'x'*100000)
            with self.assertRaises(NoSpace):quota.require(1)
            now[0] = 6
            self.assertEqual(quota.status()['used_bytes'], 100000)

    def test_quota_transfer_checks_total_free_space_and_periodic_external_changes(self):
        self.root.mkdir()
        now, free = [0.0], [20*GiB]
        quota = Quota(self.root, limit=100000, free_probe=lambda:free[0], clock=lambda:now[0])
        part = self.root / 'part'
        with quota.transfer(part) as reserve, part.open('wb') as output:
            reserve(10000);output.write(b'x'*10000)
            with self.assertRaises(NoSpace):reserve(25000)
            free[0] = 10*GiB
            with self.assertRaises(NoSpace):reserve(1)
            free[0] = 20*GiB
            (self.root / 'external').write_bytes(b'x'*30000)
            now[0] = 6
            with self.assertRaises(NoSpace):reserve(1)
        self.assertEqual(quota.status()['used_bytes'], 40000)

    def test_download_hash_and_preview(self):
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store);mid=self.enqueue(manager)
            result=manager.process(mid,self.context)
            self.assertEqual(result['state'],'DOWNLOADED')
            self.assertEqual(manager.preview_path(mid,self.context).read_bytes(),PNG)
            self.assertEqual(store.status()['posts'],1)
            with self.assertRaises(PermissionError):manager.may_redistribute(mid,self.context)

    def test_batch_exhaustion_defers_without_spending_retry_attempts(self):
        for unknown_length in (False, True):
            with self.subTest(unknown_length=unknown_length), StorageEngine(self.root / str(unknown_length)) as store:
                manager = self.setup_media(store, max_file=1000, batch_limit=100)
                if unknown_length:
                    class UnknownLength:
                        def stream(self, url):
                            yield {'length': None, 'mime': 'image/png'}
                            yield PNG
                    manager.transport = UnknownLength()
                first, second = self.enqueue(manager), self.enqueue(manager)
                results = manager.process_batch(self.context)
                self.assertEqual([row['state'] for row in results], ['DOWNLOADED', 'QUEUED'])
                self.assertEqual(results[-1]['error_code'], 'BATCH_LIMIT')
                self.assertEqual(results[-1]['attempts'], 0)
                self.assertFalse(list((store.root / 'media').glob('*.part')))
                self.assertEqual(manager.process_batch(self.context)[0]['state'], 'DOWNLOADED')
                self.assertEqual(store.media_tasks()[second]['attempts'], 1)

    def test_file_larger_than_entire_batch_does_not_block_queue(self):
        for known_length in (True, False):
            with self.subTest(known_length=known_length), StorageEngine(self.root / str(known_length)) as store:
                manager = self.setup_media(store, max_file=len(PNG) * 3, batch_limit=len(PNG))
                class SizedMedia:
                    calls = 0
                    def stream(self, url):
                        self.calls += 1
                        payload = PNG * 2 if self.calls == 1 else PNG
                        yield {'length':len(payload) if known_length else None, 'mime':'image/png'}
                        yield payload
                manager.transport = SizedMedia()
                first, second = self.enqueue(manager), self.enqueue(manager)
                results = manager.process_batch(self.context)
                self.assertEqual([row['state'] for row in results], ['FAILED', 'DOWNLOADED'])
                self.assertEqual(store.media_tasks()[first]['error_code'], 'OVERSIZE')
                self.assertEqual(store.media_tasks()[second]['file_size'], len(PNG))
                manager.process_batch(self.context)
                self.assertEqual(manager.transport.calls, 2)
                self.assertFalse(list((store.root / 'media').glob('*.part')))

    def test_reauthorization_preserves_file_and_authorization_history(self):
        with StorageEngine(self.root) as store:
            manager = self.setup_media(store)
            mid = self.enqueue(manager)
            manager.process(mid, self.context)
            before = store.media_tasks()[mid]
            for policy in self.policies:
                policy['version'] = 2
            manager.gate = PolicyGate(self.policies)
            current = dict(self.context, policy_version=2)
            with self.assertRaisesRegex(PermissionError, 'CONTEXT_MISMATCH'):
                manager.preview_path(mid, current)
            manager.reauthorize(mid, current, 'SFW', 'mock://rechecked', '2090-01-01T00:00:00Z')
            self.assertEqual(manager.preview_path(mid, current).read_bytes(), PNG)
            self.assertEqual(self.http.calls, 1)
            self.assertEqual(store.media_tasks()[mid]['local_path'], before['local_path'])
            history = [event['payload'] for event in store._events if event['event_type'] == 'MEDIA_STATE']
            self.assertEqual(history[0]['context_hash'], digest(self.context))
            self.assertEqual(history[-1]['context_hash'], digest(current))
            for bad in (dict(current, source_id='x:999'), dict(current, policy_version=3)):
                with self.assertRaises(PermissionError):
                    manager.reauthorize(mid, bad, 'SFW', 'mock://bad', '2090-01-01T00:00:00Z')
            with self.assertRaises(MediaError):
                manager.reauthorize(mid, current, 'UNKNOWN', 'mock://bad', '2090-01-01T00:00:00Z')
            self.assertEqual(store.media_tasks()[mid]['content_rating'], 'SFW')

    def test_resume_cleanup_cleans_tracked_artifacts_and_parts_at_every_boundary(self):
        for stage in ('AFTER_ERASURE_BARRIER', 'BEFORE_ERASURE_POINTER', 'AFTER_ERASURE_POINTER'):
            root = self.root / stage
            with StorageEngine(root) as store:
                manager = self.setup_media(store)
                mid = self.enqueue(manager)
                artifact = root / 'exports/owned.txt'
                artifact.write_text('owned private copy', encoding='utf-8')
                part = root / ('media/' + mid + '.part')
                part.write_bytes(b'partial private media')
                deletion = DeletionManager(store)
                deletion.track_artifact([self.post['post_id']], 'exports/owned.txt')
                def crash(current):
                    if current == stage:
                        raise OSError('injected')
                store.fault = crash
                with self.assertRaises(OSError):
                    deletion.purge(self.post['post_id'], self.context)
            with StorageEngine(root) as store:
                result = DeletionManager(store).resume_cleanup()
                self.assertTrue(result['active_cleanup_completed'])
                self.assertFalse(artifact.exists())
                self.assertFalse(part.exists())
                self.assertEqual(store.current_posts(), {})
                self.assertFalse(list((root / 'quarantine/orphans').iterdir()))
                head = copy.deepcopy(store.head)
                files = {path.name: path.stat().st_mtime_ns for path in (root / 'journal/segments').iterdir()}
                DeletionManager(store).resume_cleanup()
                DeletionManager(store).resume_cleanup()
                self.assertEqual(store.head, head)
                self.assertEqual(files, {path.name: path.stat().st_mtime_ns for path in (root / 'journal/segments').iterdir()})

    def test_single_post_barrier_interruption_resumes_without_empty_chain(self):
        with StorageEngine(self.root) as store:
            store.ingest([self.post], self.account, self.gate, self.context)
            def crash(stage):
                if stage == 'AFTER_ERASURE_BARRIER':
                    raise OSError('injected')
            store.fault = crash
            with self.assertRaises(OSError):
                DeletionManager(store).purge(self.post['post_id'], self.context)
        with StorageEngine(self.root) as store:
            self.assertTrue(DeletionManager(store).resume_cleanup()['active_cleanup_completed'])
            self.assertEqual(store.status()['tombstones'], 1)

    def test_text_scope_reduction_cleans_all_versions_and_survives_interruption(self):
        with StorageEngine(self.root) as store:
            self.setup_media(store)
            store.ingest([dict(self.post, text='second private version')], self.account, self.gate, self.context)
            artifact = self.root / 'exports/text.txt'
            artifact.write_text('second private version')
            DeletionManager(store).track_artifact([self.post['post_id']], 'exports/text.txt')
            for policy in self.policies:
                policy['version'] = 2
                if policy['operation'] == 'retain':
                    policy['content_scope'] = ['METADATA', 'MEDIA']
            gate = PolicyGate(self.policies)
            context = dict(self.context, policy_version=2)
            def crash(stage):
                if stage == 'AFTER_SCOPE_BARRIER':
                    raise OSError('injected')
            store.fault = crash
            with self.assertRaises(OSError):
                DeletionManager(store).enforce_policy_expiry(gate, [context])
        with StorageEngine(self.root) as store:
            self.assertTrue(all(row['text'] is None for row in store.history(self.post['post_id'])))
            self.assertEqual(len(store.history(self.post['post_id'])), 2)
            self.assertTrue(DeletionManager(store).resume_cleanup()['active_cleanup_completed'])
            self.assertFalse(artifact.exists())
            for path in (self.root / 'journal/segments').glob('*.jsonl'):
                self.assertNotIn('second private version', path.read_text(encoding='utf-8'))
                self.assertNotIn(self.post['text'], path.read_text(encoding='utf-8'))
            head = copy.deepcopy(store.head)
            self.assertEqual(DeletionManager(store).enforce_policy_expiry(gate, [context]), [])
            self.assertEqual(store.head, head)
            # 权限收窄后合法生成的新元数据导出，不属于上一次清理的目标。
            safe = self.root / 'exports/metadata-only.txt'
            safe.write_text(self.post['post_id'])
            DeletionManager(store).track_artifact([self.post['post_id']], 'exports/metadata-only.txt')
            self.assertTrue(DeletionManager(store).resume_cleanup()['active_cleanup_completed'])
            self.assertTrue(safe.exists())
            for policy in self.policies:
                policy['version'] = 3
                if policy['operation'] == 'retain':
                    policy['content_scope'] = ['METADATA', 'TEXT', 'MEDIA']
            renewed = dict(self.context, policy_version=3)
            store.ingest([dict(self.post, text='newly permitted content')], self.account, PolicyGate(self.policies), renewed)
            store.recover()
            history = store.history(self.post['post_id'])
            self.assertEqual([row['text'] for row in history], [None, None, 'newly permitted content'])

    def test_corrupt_scope_barrier_refuses_to_open(self):
        with StorageEngine(self.root):
            (self.root / 'deletions/scope-erasure-barrier.json').write_text('{"text":[],"media":["invalid-id"]}')
        with self.assertRaisesRegex(CorruptStore, 'INVALID_SCOPE_BARRIER'):
            StorageEngine(self.root)

    def test_hash_dedup_preserves_independent_permissions(self):
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store)
            first=self.enqueue(manager);second=self.enqueue(manager)
            a=manager.process(first,self.context);b=manager.process(second,self.context)
            self.assertEqual(a['local_path'],b['local_path'])
            self.assertEqual(len(list((self.root/'media/blobs').rglob('*/*'))),1)
            self.assertEqual(len(store.media_tasks()),2)

    def test_UNKNOWN_R18_and_restricted_block_no_request(self):
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store)
            for options in ({'rating':'UNKNOWN'},{'rating':'R18'},{'restricted':True},{'rating_evidence':None}):
                mid=self.enqueue(manager,**options)
                self.assertEqual(manager.process(mid,self.context)['state'],'BLOCKED')
            self.assertEqual(self.http.calls,0)

    def test_explicit_R18_review_is_separate_from_account_rating(self):
        self.account['account_content_rating']='R18'
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store)
            mid=self.enqueue(manager,rating='SFW')
            self.assertEqual(manager.process(mid,self.context)['state'],'DOWNLOADED')

    def test_unknown_media_rights_block(self):
        self.gate.policies[('mock-retain','x:101','download_media')]['permission_state']='UNKNOWN'
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store);mid=self.enqueue(manager)
            self.assertEqual(manager.process(mid,self.context)['error_code'],'NO_RIGHTS')
            self.assertEqual(self.http.calls,0)

    def test_revocation_after_enqueue_checked_before_network(self):
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store);mid=self.enqueue(manager)
            self.gate.policies[('mock-retain','x:101','access')]['permission_state']='REVOKED'
            self.assertEqual(manager.process(mid,self.context)['error_code'],'NO_RIGHTS')
            self.assertEqual(self.http.calls,0)

    def test_network_retries_are_bounded_and_posts_survive(self):
        self.http.error='NETWORK_ERROR'
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store);mid=self.enqueue(manager)
            for _ in range(5):manager.process(mid,self.context)
            self.assertEqual(self.http.calls,3)
            self.assertEqual(store.status()['posts'],1)
            self.assertFalse(list((self.root/'media').glob('*.part')))

    def test_oversize_and_mime_mismatch(self):
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store,max_file=8);mid=self.enqueue(manager)
            self.assertEqual(manager.process(mid,self.context)['error_code'],'OVERSIZE')
            manager=MediaManager(store,self.gate,FakeMediaHTTP(b'<html>secret</html>','image/png'))
            mid=self.enqueue(manager)
            self.assertEqual(manager.process(mid,self.context)['error_code'],'UNSUPPORTED_FORMAT')
            self.assertEqual(store.status()['posts'],1)

    def test_disk_limits_and_free_space(self):
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store);mid=self.enqueue(manager)
            store.quota=Quota(self.root,limit=1,free_probe=lambda:11*GiB)
            self.assertEqual(manager.process(mid,self.context)['error_code'],'NO_SPACE')
            self.assertEqual(store.status()['posts'],1)
            with self.assertRaises(NoSpace):store.quota.require(1)
        with self.assertRaises(NoSpace):Quota(self.root,free_probe=lambda:10*GiB).require(1)
        with self.assertRaises(ValueError):Quota(self.root,min_free=0)

    def test_url_security_and_mixed_dns(self):
        good=lambda *args,**kw:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
        for url in ('http://pbs.twimg.com/a','https://127.0.0.1/a','https://pbs.twimg.com.evil/a',
                    'https://user@pbs.twimg.com/a','https://pbs.twimg.com:444/a','file:///etc/passwd',
                    'https://pbs.twimg.com/a\r\nHeader:x'):
            with self.assertRaises(MediaError):validate_url(url,good)
        self.assertEqual(validate_url('https://pbs.twimg.com/a',good)[1],'93.184.216.34')
        mixed=lambda *args,**kw:good()+[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        with self.assertRaises(MediaError):validate_url('https://pbs.twimg.com/a',mixed)

    def test_purge_removes_history_media_and_tracked_snapshot(self):
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store);mid=self.enqueue(manager);manager.process(mid,self.context)
            path=self.root/'snapshots/owned.json';path.write_text('sensitive-old-copy')
            deletion=DeletionManager(store);deletion.track_artifact([self.post['post_id']],'snapshots/owned.json')
            report=deletion.purge(self.post['post_id'],self.context)
            self.assertTrue(report['active_cleanup_completed'])
            self.assertFalse(path.exists());self.assertEqual(store.history(self.post['post_id']),[])
            self.assertFalse(list((self.root/'media/blobs').rglob('*/*')))
            for path in (self.root/'journal/segments').glob('*.jsonl'):
                self.assertNotIn(self.post['text'],path.read_text())
        with StorageEngine(self.root) as store:
            self.assertEqual(store.current_posts(),{})
            result = store.ingest([self.post],self.account,self.gate,self.context)
            self.assertEqual(result['filtered_deleted'], 1)
            self.assertEqual(result['quarantined'], 0)

    def test_old_snapshot_restore_keeps_erasure_barrier(self):
        saved=Path(self.tmp.name)/'old-journal'
        with StorageEngine(self.root) as store:
            self.setup_media(store)
            # 快照只复制已提交内容，运行中的单写者锁不属于备份数据。
            shutil.copytree(self.root/'journal',saved,ignore=shutil.ignore_patterns('writer.lock'))
            DeletionManager(store).purge(self.post['post_id'],self.context)
        shutil.copytree(saved,self.root/'journal',dirs_exist_ok=True)
        with StorageEngine(self.root) as store:
            self.assertEqual(store.current_posts(),{});self.assertEqual(store.history(self.post['post_id']),[])
            DeletionManager(store).purge(self.post['post_id'],self.context)
            for path in (self.root/'journal/segments').glob('*.jsonl'):
                self.assertNotIn(self.post['text'],path.read_text())

    def test_untracked_or_changed_artifact_not_reported_clean(self):
        with StorageEngine(self.root) as store:
            self.setup_media(store)
            path=self.root/'snapshots/unknown.json';path.write_text('unknown')
            report=DeletionManager(store).purge(self.post['post_id'],self.context)
            self.assertFalse(report['active_cleanup_completed'])
            self.assertTrue(path.exists())

    def test_compaction_crash_does_not_resurrect(self):
        for stage in ('AFTER_ERASURE_BARRIER','BEFORE_ERASURE_POINTER','AFTER_ERASURE_POINTER'):
            root=self.root/stage
            with StorageEngine(root) as store:
                self.setup_media(store)
                def crash(current):
                    if current==stage:raise OSError('injected')
                store.fault=crash
                with self.assertRaises(OSError):DeletionManager(store).purge(self.post['post_id'],self.context)
            with StorageEngine(root) as store:
                self.assertEqual(store.current_posts(),{})
                self.assertTrue(DeletionManager(store).purge(self.post['post_id'],self.context)['active_cleanup_completed'])

    def test_expiry_cleanup(self):
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store);mid=self.enqueue(manager)
            manager.process(mid,self.context)
            result=DeletionManager(store).expire({digest(self.context):self.context},now='2091-01-01T00:00:00Z')
            self.assertEqual(len(result),1);self.assertEqual(len(store.current_posts()),1)
            self.assertEqual(store.media_tasks(),{})

    def test_permanent_error_requires_explicit_repair(self):
        self.http.error='EXPIRED_URL'
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store);mid=self.enqueue(manager)
            manager.process(mid,self.context);manager.process(mid,self.context)
            self.assertEqual(self.http.calls,1)
            self.http.error=None
            manager.repair(mid,self.context,'https://pbs.twimg.com/media/new.png','SFW',
                           'mock://review','2090-01-01T00:00:00Z')
            self.assertEqual(manager.process(mid,self.context)['state'],'DOWNLOADED')

    def test_shared_blob_retained_until_last_authorized_reference_deleted(self):
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store);first=self.enqueue(manager);second=self.enqueue(manager)
            manager.process(first,self.context);manager.process(second,self.context)
            path=manager.preview_path(second,self.context)
            DeletionManager(store).purge_media(first,self.context)
            self.assertTrue(path.exists())
            DeletionManager(store).purge_media(second,self.context)
            self.assertFalse(path.exists())
            self.assertEqual(store.status()['posts'],1)

    def test_retention_revocation_cleans_post_history(self):
        with StorageEngine(self.root) as store:
            self.setup_media(store)
            self.gate.policies[('mock-retain','x:101','retain')]['permission_state']='REVOKED'
            reports=DeletionManager(store).enforce_policy_expiry(self.gate,[self.context])
            self.assertEqual(len(reports),1);self.assertEqual(store.current_posts(),{})

    def test_media_erasure_crash_resumes_without_post_loss(self):
        with StorageEngine(self.root) as store:
            manager=self.setup_media(store);mid=self.enqueue(manager);manager.process(mid,self.context)
            def crash(stage):
                if stage=='BEFORE_ERASURE_POINTER':raise OSError('injected')
            store.fault=crash
            with self.assertRaises(OSError):DeletionManager(store).purge_media(mid,self.context)
        with StorageEngine(self.root) as store:
            self.assertEqual(store.media_tasks(),{})
            DeletionManager(store).resume_cleanup()
            self.assertFalse(list((self.root/'media/blobs').rglob('*/*')))
            self.assertEqual(len(store.current_posts()),1)

if __name__ == '__main__':
    unittest.main()
