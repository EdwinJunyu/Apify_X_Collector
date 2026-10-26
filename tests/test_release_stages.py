import copy,json,os,sqlite3,tempfile,unittest,uuid
from contextlib import closing
from datetime import date,datetime,timezone
from pathlib import Path
from unittest.mock import Mock
from collector.backup import BackupManager
from collector.deletion import DeletionManager
from collector.security import physical_disks,secure_path
from collector.scheduler import due_on,latest_due,ScheduleEngine,WindowsTasks
from collector.migration import csv_cell,import_legacy
from collector.release import build_manifest,verify_release,install,activate
from collector.acceptance import offline_trial,classification_sample,assess_live_evidence
from collector.storage import StorageEngine,atomic_bytes
from collector.policy import PolicyGate
from collector.validation import strict_loads,canonical,digest

ROOT=Path(__file__).resolve().parents[1]

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.parent=Path(self.tmp.name);self.root=self.parent/'data';self.vault=self.parent/'vault'
        self.store=StorageEngine(self.root);self.addCleanup(self.store.close)
        f=json.loads((ROOT/'fixtures/offline.json').read_bytes());self.f=f
        self.gate=PolicyGate(f['policies']);self.context=f['context'];self.post=f['post']
        self.store.ingest([self.post],f['account'],self.gate,self.context)
        self.store.backup_disk_probe=lambda path:{'disk2' if Path(path).is_relative_to(self.vault) else 'disk1'}
        self.manager=BackupManager(self.store);self.row=self.manager.register(self.vault)
    def snapshot(self):return self.manager.create(self.row,self.gate,[self.context])
    def test_backup_list_does_not_hash_payloads_and_restore_still_verifies(self):
        from unittest.mock import patch
        from desktop.service import WorkerService
        import collector.backup as module
        snap = self.snapshot()
        service = WorkerService(self.store, self.parent / 'private')
        service.backups = self.manager
        with patch.object(module, 'sha_file', wraps=module.sha_file) as hashes:
            for _ in range(5):
                self.assertEqual(service.list_backups()[0]['status'], 'NOT_VERIFIED')
            self.assertEqual(hashes.call_count, 0)
            self.manager.verify(self.row, snap['snapshot_id'])
            self.assertGreater(hashes.call_count, 0)
        self.assertEqual(service.list_backups()[0]['status'], 'VERIFIED_PREVIOUSLY')
        self.assertIsNotNone(service.list_backups()[0]['last_verified_at'])
        folder = self.manager.snapshot_directory(self.row, snap['snapshot_id'])
        segment = next(folder.glob('journal/segments/*.jsonl'))
        raw = segment.read_bytes();segment.write_bytes(b'!' + raw[1:])
        # 列表的历史结果不是恢复授权，同尺寸篡改仍须在恢复前发现。
        with self.assertRaisesRegex(ValueError, 'SNAPSHOT_HASH_MISMATCH'):
            self.manager.restore(self.row, snap['snapshot_id'], self.parent / 'bad-restore')
        self.assertFalse((self.parent / 'bad-restore').exists())
        self.assertEqual(service.list_backups()[0]['status'], 'VERIFICATION_FAILED')

    def test_backup_revision_invalidates_verification_record(self):
        snap = self.snapshot()
        self.manager.verify(self.row, snap['snapshot_id'])
        self.manager._write_snapshot(self.store, self.row, snap['snapshot_id'], rights=snap['rights'])
        self.assertEqual(self.manager.describe(self.row, snap['snapshot_id'])['verification_status'], 'NOT_VERIFIED')

    def test_observation_survives_scope_cleanup_backup_and_restore(self):
        self.store.ingest([dict(self.post, metrics={'likes':23}, observed_at='2026-10-27T00:00:00Z')],
                          self.f['account'], self.gate, self.context)
        for policy in self.f['policies']:
            policy['version'] = 2
            if policy['operation'] == 'retain':policy['content_scope'] = ['METADATA']
        gate = PolicyGate(self.f['policies']);context = dict(self.context, policy_version=2)
        snap = self.manager.create(self.row, gate, [context])
        target = self.parent / 'observed-restore'
        self.manager.restore(self.row, snap['snapshot_id'], target)
        with StorageEngine(target) as store:
            post = next(iter(store.current_posts().values()))
            self.assertEqual(post['metrics']['likes'], 23)
            self.assertEqual(post['version_number'], 1);self.assertIsNone(post['text'])

    def test_other_partition_same_physical_disk_rejected(self):
        with self.assertRaises(ValueError):BackupManager(self.store,disk_probe=lambda p:{'same'}).register(self.parent/'same')
    def test_unknown_disk_fails_closed(self):
        def unknown(path):raise RuntimeError('unknown')
        with self.assertRaises(RuntimeError):BackupManager(self.store,disk_probe=unknown).register(self.parent/'unknown')
    def test_nonempty_unowned_backup_is_not_adopted(self):
        other=self.vault/'foreign';other.mkdir();(other/'user.txt').write_text('keep')
        with self.assertRaises(ValueError):self.manager.register(other)
        self.assertTrue((other/'user.txt').exists())
    def test_snapshot_restore_preserves_data_hash_and_cursor(self):
        snap=self.snapshot();target=self.parent/'restored'
        result=self.manager.restore(self.row,snap['snapshot_id'],target)
        self.assertEqual(result['status']['posts'],1)
        with StorageEngine(target) as store:
            self.assertEqual(store.current_posts()['X:'+self.post['post_id']]['content_hash'],self.store.current_posts()['X:'+self.post['post_id']]['content_hash'])
    def test_restore_rejects_nonempty_target(self):
        snap=self.snapshot();target=self.parent/'user';target.mkdir();(target/'keep').write_text('safe')
        with self.assertRaises(ValueError):self.manager.restore(self.row,snap['snapshot_id'],target)
        self.assertEqual((target/'keep').read_text(),'safe')
    def test_missing_erasure_ledger_blocks_restore_without_original_disk(self):
        snapshots = [self.snapshot(), self.snapshot()]
        # 每次只清理一个快照，第二个快照仍含已删除正文。
        DeletionManager(self.store).purge(self.post['post_id'], self.context)
        pending = [snap for snap in snapshots if any(
            self.post['text'] in path.read_text(encoding='utf-8')
            for path in self.manager.snapshot_directory(
                self.row, snap['snapshot_id']).glob('journal/segments/*.jsonl'))]
        self.assertEqual(len(pending), 1)
        old = pending[0]
        (self.vault / 'ERASURES.json').rename(self.vault / 'ERASURES.lost')
        self.store.close()
        self.root.rename(self.parent / 'unavailable-original')
        with StorageEngine(self.parent / 'recovery-control') as control:
            atomic_bytes(control._path('deletions/store-identity.json'),
                         canonical({'store_id': self.manager.store_id}))
            control.backup_disk_probe = self.store.backup_disk_probe
            recovery = BackupManager(control)
            target = self.parent / 'must-not-restore'
            with self.assertRaisesRegex(ValueError, 'VAULT_ERASURES_MISSING'):
                recovery.restore(self.row, old['snapshot_id'], target)
            self.assertFalse(target.exists())
            self.assertFalse((self.vault / 'ERASURES.json').exists())
    def test_corrupt_backup_refuses_restore(self):
        snap=self.snapshot();directory=self.manager.snapshot_directory(self.row,snap['snapshot_id'])
        relative=next(name for name in snap['files'] if name.endswith('.jsonl'))
        (directory/relative).write_bytes(b'broken')
        with self.assertRaises(ValueError):self.manager.restore(self.row,snap['snapshot_id'],self.parent/'bad')
    def test_existing_vault_cannot_reinitialize_missing_erasure_ledger(self):
        (self.vault / 'ERASURES.json').rename(self.vault / 'ERASURES.lost')
        with self.assertRaisesRegex(ValueError, 'VAULT_ERASURES_MISSING'):
            self.manager.register(self.vault)
        with self.assertRaisesRegex(ValueError, 'VAULT_ERASURES_MISSING'):
            self.snapshot()
        self.assertFalse((self.vault / 'ERASURES.json').exists())

    def test_invalid_erasure_ledger_is_not_replaced(self):
        snap = self.snapshot()
        path = self.vault / 'ERASURES.json'
        for bad in (b'{}', b'null', b'{"posts":[],"media":["invalid"]}', b'{'):
            with self.subTest(bad=bad):
                path.write_bytes(bad)
                target = self.parent / 'invalid-ledger-target'
                with self.assertRaises(ValueError):
                    self.manager.restore(self.row, snap['snapshot_id'], target)
                self.assertFalse(target.exists())
                self.assertEqual(path.read_bytes(), bad)
    def test_backup_excludes_credentials_private_raw_and_exports(self):
        (self.root/'.env').write_text('APIFY_TOKEN=private-do-not-copy')
        (self.root/'raw/secret.txt').write_text('not-authorized')
        snap=self.snapshot()
        for path in self.manager.snapshot_directory(self.row,snap['snapshot_id']).rglob('*'):
            if path.is_file():
                self.assertNotIn(b'private-do-not-copy',path.read_bytes());self.assertNotIn(b'not-authorized',path.read_bytes())
    def test_deletion_scrubs_registered_older_snapshot_and_restore(self):
        snap=self.snapshot();sid=snap['snapshot_id']
        result=DeletionManager(self.store).purge(self.post['post_id'],self.context)
        self.assertTrue(result['external_backup_cleanup']['completed'])
        target=self.parent/'old-restore';restored=self.manager.restore(self.row,sid,target)
        self.assertEqual(restored['status']['posts'],0)
        for path in self.manager.snapshot_directory(self.row,sid).rglob('*'):
            if path.is_file():self.assertNotIn(self.post['text'].encode(),path.read_bytes())
        for path in (target/'journal/segments').glob('*.jsonl'):self.assertNotIn(self.post['text'],path.read_text())
    def test_text_scope_reduction_scrubs_backup_and_cannot_reappear_after_regrant(self):
        snapshots = [self.snapshot(), self.snapshot()]
        policies = copy.deepcopy(self.f['policies'])
        for policy in policies:
            policy['version'] = 2
            if policy['operation'] == 'retain':
                policy['content_scope'] = ['METADATA']
        context = dict(self.context, policy_version=2)
        gate = PolicyGate(policies)
        self.manager.update_policies(gate, [context])
        DeletionManager(self.store).enforce_policy_expiry(gate, [context])
        self.manager.propagate_erasure()
        for snap in snapshots:
            folder = self.manager.snapshot_directory(self.row, snap['snapshot_id'])
            for path in folder.glob('journal/segments/*.jsonl'):
                self.assertNotIn(self.post['text'], path.read_text(encoding='utf-8'))
        # 以后重新许可 TEXT，也不能让旧快照中的已撤回版本重新出现全文。
        for policy in policies:
            policy['version'] = 3
            if policy['operation'] == 'retain':
                policy['content_scope'] = ['METADATA', 'TEXT']
        self.manager.update_policies(PolicyGate(policies), [dict(self.context, policy_version=3)])
        self.store.close()
        with StorageEngine(self.parent / 'no-original-control') as control:
            atomic_bytes(control._path('deletions/store-identity.json'), canonical({'store_id':self.manager.store_id}))
            control.backup_disk_probe = self.store.backup_disk_probe
            target = self.parent / 'scope-restored'
            BackupManager(control).restore(self.row, snapshots[-1]['snapshot_id'], target)
        with StorageEngine(target) as restored:
            self.assertEqual(restored.status()['posts'], 1)
            self.assertIsNone(restored.current_posts()['X:' + self.post['post_id']]['text'])

    def test_media_scope_reduction_preserves_text_and_erases_blobs_and_refs(self):
        from collector.media import MediaManager
        from collector.media_http import FakeMediaHTTP
        from desktop.service import PNG
        policies = copy.deepcopy(self.f['policies'])
        for policy in policies:
            if policy['operation'] != 'redistribute':
                policy.update(permission_state='PERMITTED', content_scope=['METADATA', 'TEXT', 'MEDIA'])
        gate = PolicyGate(policies)
        post = dict(self.post, media_refs=['https://pbs.twimg.com/private.png'])
        self.store.ingest([post], self.f['account'], gate, self.context)
        media = MediaManager(self.store, gate, FakeMediaHTTP(PNG))
        mid = media.enqueue(post['post_id'], 'https://pbs.twimg.com/private.png', self.context,
                            'SFW', 'mock://review', '2090-01-01T00:00:00Z')
        task = media.process(mid, self.context)
        self.manager.create(self.row, gate, [self.context])
        for policy in policies:
            policy['version'] = 2
            if policy['operation'] == 'retain':
                policy['content_scope'] = ['METADATA', 'TEXT']
        gate = PolicyGate(policies)
        context = dict(self.context, policy_version=2)
        self.manager.update_policies(gate, [context])
        DeletionManager(self.store).enforce_policy_expiry(gate, [context])
        current = self.store.current_posts()['X:' + post['post_id']]
        self.assertEqual(current['text'], post['text'])
        self.assertEqual(current['media_refs'], [])
        self.assertEqual(self.store.media_tasks(), {})
        self.assertFalse(self.store._path(task['local_path']).exists())
        self.assertTrue(all(row['media_refs'] == [] for row in self.store.history(post['post_id'])))
    def test_unavailable_backup_does_not_report_external_cleanup_done(self):
        self.snapshot();self.vault.rename(self.parent/'detached')
        result=DeletionManager(self.store).purge(self.post['post_id'],self.context)
        self.assertFalse(result['external_backup_cleanup']['completed'])
    def test_backup_requires_policy_review(self):
        with self.assertRaises(PermissionError):self.manager.create(self.row)
    def test_snapshot_hash_index_rejects_traversal(self):
        snap=self.snapshot();path=self.vault/'snapshot-index.json'
        path.write_text(json.dumps({snap['snapshot_id']:'../../escape'}))
        with self.assertRaises(ValueError):self.manager.verify(self.row,snap['snapshot_id'])

    def test_latest_permission_revocation_applies_to_old_snapshot(self):
        snap=self.snapshot()
        policies=copy.deepcopy(self.f['policies'])
        for policy in policies:
            policy['version']=2
            if policy['operation']=='retain':policy['permission_state']='REVOKED'
        context=dict(self.context,policy_version=2)
        self.manager.update_policies(PolicyGate(policies),[context])
        result=self.manager.restore(self.row,snap['snapshot_id'],self.parent/'rights-restored')
        self.assertEqual(result['status']['posts'],0)

    def test_partial_restore_root_cannot_open(self):
        target=self.parent/'partial';target.mkdir()
        (target/'.restore-in-progress.json').write_text('{"token":"pending"}')
        with self.assertRaisesRegex(ValueError,'RESTORE_INCOMPLETE'):StorageEngine(target)

    def test_owned_orphan_snapshot_is_scrubbed(self):
        import shutil
        snap=self.snapshot();directory=self.manager.snapshot_directory(self.row,snap['snapshot_id'])
        orphan=directory.parent/uuid.uuid4().hex;shutil.copytree(directory,orphan)
        self.manager.cleanup_orphans(self.row)
        self.assertFalse(orphan.exists());self.manager.verify(self.row,snap['snapshot_id'])

    def test_restore_failure_leaves_root_sealed(self):
        snap=self.snapshot();target=self.parent/'failed-restore'
        self.manager._copy_files=Mock(side_effect=RuntimeError('injected copy interruption'))
        with self.assertRaises(RuntimeError):self.manager.restore(self.row,snap['snapshot_id'],target)
        with self.assertRaisesRegex(ValueError,'RESTORE_INCOMPLETE'):StorageEngine(target)

    def test_restore_media_uses_current_copy_permission(self):
        from collector.media import MediaManager
        from collector.media_http import FakeMediaHTTP
        from desktop.service import PNG
        policies=copy.deepcopy(self.f['policies'])
        for policy in policies:
            if policy['operation']!='redistribute':
                policy['permission_state']='PERMITTED';policy['content_scope']=['METADATA','TEXT','MEDIA']
        gate=PolicyGate(policies)
        manager=MediaManager(self.store,gate,FakeMediaHTTP(PNG))
        mid=manager.enqueue(self.post['post_id'],'https://pbs.twimg.com/test.png',self.context,'SFW','mock://rating','2090-01-01T00:00:00Z')
        manager.process(mid,self.context)
        snap=self.manager.create(self.row,gate,[self.context])
        for policy in policies:
            policy['version']=2
            if policy['operation']=='download_media':policy['permission_state']='REVOKED'
        self.manager.update_policies(PolicyGate(policies),[dict(self.context,policy_version=2)])
        target=self.parent/'revoked-media-restore';self.manager.restore(self.row,snap['snapshot_id'],target)
        with StorageEngine(target) as restored:
            self.assertEqual(restored.status()['posts'],1);self.assertEqual(restored.media_tasks(),{})
            self.assertFalse(list((target/'media/blobs').rglob('*/*')))

    def test_restored_schedule_disabled_and_new_work_paused(self):
        from desktop.service import defaults,ADMIN
        state=defaults();state['schedule_enabled']=True
        self.store.save_admin('SETTINGS_STATE',state,ADMIN)
        snap=self.snapshot();target=self.parent/'paused-restore';self.manager.restore(self.row,snap['snapshot_id'],target)
        with StorageEngine(target) as restored:
            state=restored.admin_events('SETTINGS_STATE')[-1]
            self.assertFalse(state['schedule_enabled']);self.assertTrue(state['paused'])

    def test_restore_replays_pages_missing_from_snapshot(self):
        from collector.apify import FakeTransport
        snap = self.snapshot()
        post = dict(self.post, post_id='987654', source_url='https://x.com/mock_artist/status/987654')
        transport = FakeTransport([post])
        worker = self.coordinator(self.store, transport)
        jid = worker.start(self.f['account'], self.context, now='2026-10-25T00:00:00Z')
        worker.advance(jid, self.f['account'], self.context)
        worker.advance(jid, self.f['account'], self.context)
        target = self.parent / 'replay-pages'
        self.manager.restore(self.row, snap['snapshot_id'], target)
        with StorageEngine(target) as restored:
            self.assertEqual(restored.jobs()[jid]['offset'], 0)
            worker = self.coordinator(restored, transport)
            worker.advance(jid, self.f['account'], self.context)
            worker.advance(jid, self.f['account'], self.context)
            self.assertIn('X:987654', restored.current_posts())
            self.assertEqual(transport.creates, 1)

    def test_unchanged_erasure_does_not_rewrite_snapshots(self):
        self.snapshot()
        self.manager.propagate_erasure()
        before = self.manager._index(self.row)
        self.manager._copy_files = Mock(side_effect=AssertionError('unnecessary copy'))
        result = self.manager.propagate_erasure()
        self.assertTrue(result[0]['completed'])
        self.assertEqual(before, self.manager._index(self.row))

    def coordinator(self, store, transport):
        from collector.adapter import StandardFixtureAdapter
        from collector.sync import SyncCoordinator
        policies = copy.deepcopy(self.f['policies'])
        for policy in policies:
            if policy['operation'] == 'access':
                policy['permission_state'] = 'PERMITTED'
        return SyncCoordinator(store, transport, StandardFixtureAdapter(), PolicyGate(policies))

    def test_old_snapshot_keeps_later_bill_and_unknown_run(self):
        from collector.apify import FakeTransport
        snap = self.snapshot()
        transport = FakeTransport([])
        worker = self.coordinator(self.store, transport)
        jid = worker.start(self.f['account'], self.context)
        for step in range(3):
            worker.advance(jid, self.f['account'], self.context)
        transport.unknown_create = True
        unknown = worker.start(self.f['account'], self.context)
        target = self.parent / 'accounting-restored'
        self.manager.restore(self.row, snap['snapshot_id'], target)
        with StorageEngine(target) as restored:
            retry = FakeTransport([])
            coordinator = self.coordinator(restored, retry)
            balance = coordinator.budget()
            self.assertEqual(str(balance['finalized']), '0.10')
            self.assertEqual(str(balance['reserved']), '0.50')
            self.assertTrue(balance['blocked'])
            self.assertEqual(restored.jobs()[unknown]['state'], 'REMOTE_OUTCOME_UNKNOWN')
            with self.assertRaises(RuntimeError):
                coordinator.start(self.f['account'], self.context)
            self.assertEqual(retry.creates, 0)

    def test_vault_unavailable_blocks_before_paid_post(self):
        from collector.apify import FakeTransport
        self.vault.rename(self.parent / 'detached')
        transport = FakeTransport([])
        with self.assertRaises(FileNotFoundError):
            self.coordinator(self.store, transport).start(self.f['account'], self.context)
        self.assertEqual(transport.creates, 0)
        job = next(iter(self.store.jobs().values()))
        self.assertEqual(job['state'], 'FAILED')
        self.assertEqual(job['error_code'], 'CONFIRMED_NOT_CREATED')
        self.assertEqual(job['finalized_usd'], '0.00')
        self.store.close()
        with StorageEngine(self.root) as reopened:
            reopened.backup_disk_probe = self.store.backup_disk_probe
            worker = self.coordinator(reopened, transport)
            self.assertFalse(worker.budget()['blocked'])
            (self.parent / 'detached').rename(self.vault)
            worker.start(self.f['account'], self.context)
            self.assertEqual(transport.creates, 1)
            mirrored = strict_loads((self.vault / 'ACCOUNTING.json').read_bytes())['payload']['jobs']
            self.assertEqual(next(item for item in mirrored if item['job_id'] == job['job_id'])['finalized_usd'], '0.00')

    def test_recovery_without_original_disk_keeps_accounting(self):
        from collector.apify import FakeTransport
        snap = self.snapshot()
        worker = self.coordinator(self.store, FakeTransport([], unknown_create=True))
        jid = worker.start(self.f['account'], self.context)
        self.store.close()
        import shutil
        shutil.rmtree(self.root)
        with StorageEngine(self.parent / 'recovery-control') as control:
            atomic_bytes(control._path('deletions/store-identity.json'), canonical({'store_id': self.manager.store_id}))
            control.backup_disk_probe = self.store.backup_disk_probe
            recovery = BackupManager(control)
            recovery.restore(self.row, snap['snapshot_id'], self.parent / 'lost-disk-restored')
        with StorageEngine(self.parent / 'lost-disk-restored') as restored:
            self.assertEqual(restored.jobs()[jid]['state'], 'REMOTE_OUTCOME_UNKNOWN')
            self.assertTrue(self.coordinator(restored, FakeTransport([])).budget()['blocked'])

    def test_corrupt_or_missing_accounting_seals_restore(self):
        snap = self.snapshot()
        ledger = self.vault / 'ACCOUNTING.json'
        ledger.write_text('{"store_id":"invalid","payload":{},"payload_hash":"bad"}')
        for name in ('corrupt-accounting', 'missing-accounting'):
            target = self.parent / name
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.manager.restore(self.row, snap['snapshot_id'], target)
            with self.assertRaisesRegex(ValueError, 'RESTORE_INCOMPLETE'):
                StorageEngine(target)
            ledger.unlink(missing_ok=True)

    def test_registered_vault_unknown_disk_mapping_fails_closed(self):
        self.manager.disk_probe = lambda path: set()
        with self.assertRaisesRegex(ValueError, 'VAULT_DISK_CHANGED'):
            self.manager._vault(self.row)

    def test_registration_mirrors_accounting_immediately(self):
        self.assertEqual(strict_loads((self.vault / 'ACCOUNTING.json').read_bytes())['store_id'], self.manager.store_id)

    def test_conflicting_remote_run_refuses_restore(self):
        from collector.apify import FakeTransport
        worker = self.coordinator(self.store, FakeTransport([]))
        jid = worker.start(self.f['account'], self.context)
        snap = self.snapshot()
        job = self.store.jobs()[jid]
        job['run_id'] = 'other-remote-run'
        self.store.save_job(job, self.context)
        target = self.parent / 'run-conflict'
        with self.assertRaisesRegex(ValueError, 'RESTORE_ACCOUNTING_REMOTE_CONFLICT'):
            self.manager.restore(self.row, snap['snapshot_id'], target)
        with self.assertRaisesRegex(ValueError, 'RESTORE_INCOMPLETE'):
            StorageEngine(target)

class ScheduleTests(unittest.TestCase):
    def test_local_nine_am_follows_dst(self):
        winter=due_on(date(2026,1,10),'09:00','America/Vancouver')
        summer=due_on(date(2026,7,10),'09:00','America/Vancouver')
        self.assertEqual((winter.hour,summer.hour),(17,16))
    def test_spring_gap_resolves_first_valid_minute(self):
        due=due_on(date(2026,3,8),'02:30','America/Vancouver')
        self.assertEqual(due.isoformat(),'2026-03-08T10:00:00+00:00')
    def test_fall_fold_only_first_occurrence(self):
        due=due_on(date(2026,11,1),'01:30','America/Vancouver')
        self.assertEqual(due.isoformat(),'2026-11-01T08:30:00+00:00')
    def test_before_due_does_not_claim_today(self):
        now=datetime(2026,10,24,15,59,tzinfo=timezone.utc)
        self.assertEqual(latest_due(now,'09:00','America/Vancouver'),date(2026,10,23))
    def test_missed_days_are_merged_and_claim_is_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            with StorageEngine(Path(directory)/'data') as store:
                schedule=ScheduleEngine(store)
                schedule.save({'schedule_key':None,'claimed_day':'2026-10-01','missed_days':0,'entries':[]})
                settings={'schedule_enabled':True,'paused':False,'schedule_local_time':'09:00','timezone':'America/Vancouver'}
                account={'account_id':'a','active_status':'ACTIVE'}
                claimed=schedule.claim(settings,[account],now='2026-10-24T18:00:00Z')
                self.assertEqual(claimed['missed_days'],23);self.assertEqual(len(claimed['entries']),1)
                rid=claimed['entries'][0]['request_id']
                again=schedule.claim(settings,[account],now='2026-10-25T18:00:00Z')
                self.assertEqual(again['entries'][0]['request_id'],rid)
    def test_disabled_schedule_queues_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            with StorageEngine(Path(directory)/'data') as store:
                result=ScheduleEngine(store).claim({'schedule_enabled':False,'paused':False},[])
                self.assertEqual(result['entries'],[])
    def test_task_xml_least_privilege_correct_escaping(self):
        import xml.etree.ElementTree as ET
        xml=WindowsTasks('C:/Data & ACG','C:/private').xml('09:00','S-1-5-21-123')
        document=ET.fromstring(xml);text=xml.decode('utf-16')
        self.assertIn('LeastPrivilege',text);self.assertIn('InteractiveToken',text)
        self.assertIn('&amp;',text);self.assertIn('IgnoreNew',text);self.assertNotIn('Password',text)
    def test_windows_extent_parser_collapses_same_disk_partitions(self):
        import struct
        from collector.security import parse_disk_extents
        data=struct.pack('<II',2,0)+struct.pack('<IIQQ',3,0,0,4096)+struct.pack('<IIQQ',3,0,4096,8192)
        self.assertEqual(parse_disk_extents(data),{3})

    def test_windows_extent_parser_rejects_incomplete_device_data(self):
        import struct
        from collector.security import parse_disk_extents
        for data in (b'',struct.pack('<II',0,0),struct.pack('<II',2,0)+b'0'*24,struct.pack('<II',17,0)):
            with self.assertRaises(RuntimeError):parse_disk_extents(data)

    def test_native_scheduler_not_silently_enabled_on_nonwindows(self):
        if os.name=='nt':self.skipTest('platform-specific negative test')
        with self.assertRaises(RuntimeError):WindowsTasks('.','.').register({'timezone':'America/Vancouver'})

class ScheduledServiceTests(unittest.TestCase):
    def test_manual_gate_enable_catchup_and_disable(self):
        from desktop.service import WorkerService
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory)
            with StorageEngine(parent/'data') as store:
                private=parent/'private';private.mkdir()
                service=WorkerService(store,private);service.load_demo()
                vault=parent/'vault'
                probe=lambda path:{'disk2' if Path(path).is_relative_to(vault) else 'disk1'}
                store.backup_disk_probe=probe;service.backups=BackupManager(store,disk_probe=probe)
                service.backups.register(vault)
                service.task_backend=Mock()
                service.task_backend.register.return_value={'installed':True}
                service.task_backend.disable.return_value={'disabled':True}
                with self.assertRaisesRegex(ValueError,'MANUAL'):service.enable_schedule({'confirmation':'ENABLE'})
                jid=service.start_sync({'account_id':'mock-artist-1'},'7'*32)['job_id']
                for _ in range(5):service.tick()
                service.enable_schedule({'confirmation':'ENABLE'})
                self.assertTrue(service.settings['schedule_enabled']);service.task_backend.register.assert_called_once()
                self.assertEqual(len(service.schedule.claim(service.settings,list(service.accounts().values()),now='2026-10-26T18:00:00Z')['entries']),1)
                service.disable_schedule();self.assertFalse(service.settings['schedule_enabled'])

    def test_scheduled_source_permissions_revalidated(self):
        from desktop.service import WorkerService
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory)
            with StorageEngine(parent/'data') as store:
                private=parent/'private';private.mkdir();service=WorkerService(store,private);service.load_demo()
                service.settings['schedule_enabled']=True
                old=service.policies()[0];old.update({'version':2,'permission_state':'REVOKED'})
                service.save_policy({'policy':old})
                service.schedule.step(service,now='2026-10-25T18:00:00Z')
                state=service.schedule.current();self.assertEqual(state['entries'][0]['status'],'BLOCKED')
                self.assertEqual(store.jobs(),{})

    def test_mock_history_cannot_authorize_real_expansion(self):
        from desktop.service import WorkerService
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory)
            with StorageEngine(parent/'data') as store:
                private=parent/'private';private.mkdir();service=WorkerService(store,private);service.load_demo()
                with self.assertRaisesRegex(ValueError,'PREVIOUS_REAL_STAGE'):
                    service.approve_stage({'stage':20,'reviewer':'test','evidence_uri':'https://example.test/evidence','confirmation':'APPROVE'})
                self.assertEqual(service.approved_stage(),5)

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'source';self.source.mkdir()
        (self.source/'app.py').write_text('print("test")\n');build_manifest(self.source)
    def test_release_hash_tampering_detected(self):
        (self.source/'app.py').write_text('changed')
        with self.assertRaises(ValueError):verify_release(self.source)
    def test_release_secrets_refused(self):
        (self.source/'.env').write_text('APIFY_TOKEN=test-secret')
        with self.assertRaises(PermissionError):build_manifest(self.source)
    def test_user_install_does_not_touch_data(self):
        data=self.root/'data';data.mkdir();(data/'keep').write_text('retained')
        result=install(self.source,self.root/'installed',with_dependencies=False)
        self.assertEqual(result['version'],'0.0.9');self.assertEqual((data/'keep').read_text(),'retained')
    def test_git_checkout_verifies_and_installs_without_git_metadata(self):
        import subprocess
        subprocess.run(['git', 'init', str(self.source)], check=True, capture_output=True)
        self.assertTrue((self.source / '.git/HEAD').is_file())
        verify_release(self.source)
        install(self.source, self.root / 'git-installed', with_dependencies=False)
        installed = self.root / 'git-installed/versions/0.0.9'
        self.assertFalse((installed / '.git').exists())
        verify_release(installed)
        (self.source / 'unexpected.txt').write_text('not a release file')
        with self.assertRaisesRegex(ValueError, 'FILE_SET_MISMATCH'):
            verify_release(self.source)
    def test_incompatible_rollback_blocked(self):
        install(self.source,self.root/'installed',with_dependencies=False)
        data=self.root/'data';(data/'deletions').mkdir(parents=True)
        (data/'deletions/format-floor.json').write_text('{"min_reader_level":11}')
        with self.assertRaises(ValueError):activate(self.root/'installed','0.0.9',data)
    def test_compatible_activation_preserves_barriers(self):
        install(self.source,self.root/'installed',with_dependencies=False)
        data=self.root/'data';(data/'deletions').mkdir(parents=True)
        barrier=data/'deletions/erasure-barrier.json';barrier.write_text('["123"]')
        self.assertTrue(activate(self.root/'installed','0.0.9',data)['data_untouched'])
        self.assertEqual(barrier.read_text(),'["123"]')
    def test_csv_formula_injection_escaped(self):
        for value in ('=SUM(1,2)',' +cmd','-1','@link','\t=1'):
            self.assertTrue(csv_cell(value).startswith("'"))
        self.assertEqual(csv_cell('ordinary'),'ordinary')
    def test_private_posix_mode(self):
        if os.name=='nt':self.skipTest('Windows DACL needs native host')
        secure_path(self.source,directory=True);self.assertEqual(self.source.stat().st_mode&0o777,0o700)

class MigrationAcceptanceTests(unittest.TestCase):
    def test_readonly_legacy_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory);database=parent/'old.sqlite3'
            f=json.loads((ROOT/'fixtures/offline.json').read_bytes());post=f['post']
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute('CREATE TABLE staged_posts(id TEXT,account TEXT,published_at TEXT,url TEXT,post_kind TEXT,lang TEXT,full_text TEXT)')
                connection.execute('INSERT INTO staged_posts VALUES(?,?,?,?,?,?,?)',(post['post_id'],'mock_artist',post['published_at'],post['source_url'],'original','ja','owned fixture'))
            before=database.read_bytes()
            with StorageEngine(parent/'data') as store:
                gate=PolicyGate(f['policies'])
                dry=import_legacy(store,database,f['account'],gate,f['context'])
                self.assertTrue(dry['dry_run']);self.assertEqual(store.status()['posts'],0)
                import_legacy(store,database,f['account'],gate,f['context'],True)
                again=import_legacy(store,database,f['account'],gate,f['context'],True)
                self.assertTrue(again['already_imported']);self.assertEqual(store.status()['posts'],1)
            self.assertEqual(database.read_bytes(),before)
    def test_five_account_mock_and_classification_gold(self):
        report=offline_trial(5);self.assertTrue(report['passed']);self.assertEqual(report['successes'],4)
        sample=classification_sample();self.assertEqual(sample['correct'],20);self.assertEqual(sample['critical_identity_or_date_errors'],0)
    def test_mock_report_cannot_be_used_as_live_evidence(self):
        with self.assertRaises(ValueError):assess_live_evidence({'mode':'MOCK'})

if __name__=='__main__':unittest.main()
