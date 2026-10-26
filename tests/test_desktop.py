import copy, importlib.util, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
if os.name!='nt' and not os.environ.get('DISPLAY'):os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
HAS_QT=importlib.util.find_spec('PySide6') is not None
ROOT=Path(__file__).resolve().parents[1]
if HAS_QT:
    from PySide6.QtWidgets import QApplication,QDialogButtonBox,QLineEdit
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from desktop.service import WorkerService
    from desktop.ipc import Client,launch_worker,endpoint
    from desktop.gui import MainWindow
    from desktop.widgets import DataPage,EditorDialog
    from collector.storage import StorageEngine
    from collector.validation import digest,strict_loads

def wait_ui(milliseconds):
    deadline=time.monotonic()+milliseconds/1000
    while time.monotonic()<deadline:
        QApplication.instance().processEvents()
        time.sleep(.005)

@unittest.skipUnless(HAS_QT,'Install requirements-desktop.txt')
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'data';self.private=Path(self.tmp.name)/'private';self.private.mkdir()
        self.store=StorageEngine(self.root);self.addCleanup(self.store.close)
        self.s=WorkerService(self.store,self.private);self.s.load_demo()
    def sync(self):
        jid=self.s.start_sync({'account_id':'mock-artist-1'},'a'*32)['job_id']
        for _ in range(5):self.s.tick()
        return jid
    def test_account_conflict_and_stable_id(self):
        row=self.s.accounts()['mock-artist-1'];new=copy.deepcopy(row);new['current_handle']='changed'
        with self.assertRaisesRegex(ValueError,'CONFLICT'):self.s.save_account({'account':new,'revision':'wrong'})
        self.s.save_account({'account':new,'revision':digest(row)})
        new['account_id']='other'
        with self.assertRaisesRegex(ValueError,'STABLE_ID'):self.s.save_account({'account':new,'revision':None})
    def test_pause_does_not_stop_existing_run(self):
        jid=self.s.start_sync({'account_id':'mock-artist-1'},'b'*32)['job_id'];self.s.pause(True)
        with self.assertRaisesRegex(ValueError,'PAUSED'):self.s.start_sync({'account_id':'mock-artist-1'},'c'*32)
        for _ in range(5):self.s.tick()
        self.assertEqual(self.store.jobs()[jid]['state'],'DONE');self.assertEqual(self.s.snapshot()['state'],'PAUSED')
    def test_start_idempotency_survives_restart(self):
        first=self.sync();s=WorkerService(self.store,self.private)
        again=s.start_sync({'account_id':'mock-artist-1'},'a'*32)['job_id']
        self.assertEqual(first,again);self.assertEqual(len(self.store.jobs()),1)
    def test_historical_job_uses_saved_actor_build_and_mode_without_token_for_billing(self):
        from collector.apify import FakeTransport
        from unittest.mock import patch
        jid = self.sync()
        job = self.store.jobs()[jid]
        job.update(actor_id='historic/actor', actor_build='build-A', mode='LIVE',
                   adapter_version='scrapesage-1', finalized_usd=None, bill_evidence_uri=None)
        self.store.save_job(job, self.s.context(self.s.accounts()[job['account_id']]))
        settings = dict(self.s.settings, actor_id='new/actor', actor_build='build-B', mode='OFFLINE')
        self.s.save_settings({'settings':settings, 'revision':digest(self.s.settings)})
        self.store.close()
        self.store = StorageEngine(self.root);self.addCleanup(self.store.close)
        restarted = WorkerService(self.store, self.private)
        restarted.finalize_bill({'job_id':jid, 'actual_usd':'0.10', 'evidence_uri':'manual://invoice'})
        self.assertEqual(self.store.jobs()[jid]['finalized_usd'], '0.10')
        with patch('desktop.service.ApifyTransport', return_value=FakeTransport([])) as live:
            worker, account, context = restarted.coordinator(jid)
            self.assertEqual(worker.adapter.actor_id, 'historic/actor')
            self.assertEqual(worker.adapter.build, 'build-A')
            live.assert_called_once()
            # 真正经过历史任务门禁推进原 Dataset，不能仅比较 adapter 属性。
            pending = self.store.jobs()[jid]
            pending.update(state='READING', offset=0, total=0)
            self.store.save_job(pending, context)
            self.assertEqual(worker.advance(jid, account, context)['state'], 'DONE')
            self.assertEqual(worker.transport.creates, 0)
        fresh = restarted.runtime(restarted.accounts()['mock-artist-1'])
        self.assertEqual(fresh.adapter.actor_id, 'offline-synthetic')
        unsupported = dict(self.store.jobs()[jid], adapter_version='unsupported-old-version',
                           finalized_usd=None, bill_evidence_uri=None)
        self.store.save_job(unsupported, context)
        with self.assertRaisesRegex(PermissionError, 'HISTORICAL_ADAPTER_UNSUPPORTED'):
            restarted.runtime(account, unsupported)
        # 无需解析 Dataset 的核账仍可进行，避免旧 adapter 退役后负债无法结算。
        restarted.finalize_bill({'job_id':jid, 'actual_usd':'0.10', 'evidence_uri':'manual://invoice-confirmed'})

    def test_new_content_does_not_inherit_review_and_stale_dialog_is_rejected(self):
        from datetime import timedelta
        from collector.validation import parse_utc
        self.sync()
        before = next(iter(self.store.current_posts().values()))
        args = {'post_id':before['post_id'], 'status':'REVIEWED', 'category':'SFW',
                'reviewer':'test', 'evidence_uri':'mock://version-one', 'notes':'reviewed first version'}
        self.s.review(args)
        account = self.s.accounts()[before['account_id']]
        changed = dict(before, text='new unreviewed content',
                       observed_at=(parse_utc(before['observed_at']) + timedelta(seconds=1)).isoformat().replace('+00:00','Z'))
        self.store.ingest([changed], account, self.s.gate(), self.s.context(account))
        detail = self.s.post_detail({'post_id':before['post_id']})
        self.assertEqual((detail['version_number'], detail['review_status'], detail['content_rating']), (2, 'STAGING', 'UNKNOWN'))
        self.assertEqual(self.store.admin_events('REVIEW_STATE')[-1]['version_number'], 1)
        with self.assertRaisesRegex(ValueError, 'REVIEW_BEFORE_PUBLISHABLE'):
            self.s.review(dict(args, status='PUBLISHABLE'))
        with self.assertRaisesRegex(ValueError, 'CONTENT_CHANGED'):
            self.s.review(dict(args, version_number=1, content_hash=before['content_hash']))
        self.s.review(args)
        changed['metrics'] = {'likes': 100}
        self.store.ingest([changed], account, self.s.gate(), self.s.context(account))
        self.assertEqual(self.s.post_detail({'post_id':before['post_id']})['review_status'], 'REVIEWED')
        self.assertEqual(self.s.post_detail({'post_id':before['post_id']})['metrics']['likes'], 100)

    def test_unbound_legacy_review_is_retained_but_not_applied(self):
        self.sync()
        post = next(iter(self.store.current_posts().values()))
        self.store.save_admin('REVIEW_STATE', {'post_id':post['post_id'], 'status':'PUBLISHABLE',
            'category':'SFW', 'reviewer':'old', 'evidence_uri':'mock://old', 'notes':None,
            'reviewed_at':'2026-10-24T12:00:00Z'}, self.s.context(self.s.accounts()[post['account_id']]))
        self.assertEqual(self.s.post_detail({'post_id':post['post_id']})['review_status'], 'STAGING')
        self.assertEqual(len(self.store.admin_events('REVIEW_STATE')), 1)

    def test_policy_scope_reduction_and_background_cleanup_are_idempotent(self):
        self.sync()
        post = next(iter(self.store.current_posts().values()))
        policy = next(copy.deepcopy(p) for p in self.s.policies() if p['operation'] == 'retain')
        policy.update(version=2, content_scope=['METADATA'])
        with self.assertRaisesRegex(ValueError, 'CONFIRM_RETENTION'):
            self.s.save_policy({'policy':policy})
        self.s.save_policy({'policy':policy, 'confirmation':policy['policy_id']})
        self.assertTrue(all(row['text'] is None for row in self.store.current_posts().values()))
        for path in (self.root / 'journal/segments').glob('*.jsonl'):
            self.assertNotIn(post['text'], path.read_text(encoding='utf-8'))
        head = copy.deepcopy(self.store.head)
        self.s.cleanup();self.s.cleanup()
        self.assertEqual(self.store.head, head)

    def test_media_can_be_reauthorized_after_redistribute_policy_update(self):
        self.sync()
        pid = next(iter(self.store.current_posts().values()))['post_id']
        mid = self.s.queue_media({'post_id':pid, 'url':'https://pbs.twimg.com/mock.png', 'rating':'SFW',
            'evidence_uri':'mock://rating', 'expires_at':'2027-01-01T00:00:00Z'})['media_id']
        self.s.process_media({'media_id':mid})
        original = self.s.preview({'media_id':mid})['path']
        policy = next(copy.deepcopy(p) for p in self.s.policies() if p['operation'] == 'redistribute')
        policy.update(version=2, permission_state='PERMITTED')
        self.s.save_policy({'policy':policy})
        with self.assertRaises(PermissionError):
            self.s.preview({'media_id':mid})
        self.s.dispatch('reauthorize_media', {'media_id':mid, 'rating':'SFW', 'evidence_uri':'mock://new-authorization',
            'expires_at':'2027-01-01T00:00:00Z'}, 'e'*32)
        self.assertEqual(self.s.preview({'media_id':mid})['path'], original)
    def test_unknown_job_can_be_confirmed_not_created_through_dispatch(self):
        from collector.apify import FakeTransport
        from unittest.mock import patch
        with patch.object(FakeTransport, 'create', side_effect=TimeoutError):
            jid = self.s.start_sync({'account_id':'mock-artist-1'}, 'b'*32)['job_id']
        job = self.store.jobs()[jid]
        job.update(mode='LIVE', actor_id='historic/actor', actor_build='old-build', adapter_version='unavailable-adapter')
        self.store.save_job(job, self.s.context(self.s.accounts()[job['account_id']]))
        args = {'job_id':jid, 'not_created':True, 'run_id':'',
                'evidence_uri':'manual://console-and-billing-checked', 'confirmation':jid}
        with self.assertRaisesRegex(ValueError, 'MUST_NOT_HAVE_RUN_ID'):
            self.s.reconcile(dict(args, run_id='existing-run'))
        with self.assertRaisesRegex(ValueError, 'EVIDENCE_AND_JOB_CONFIRMATION'):
            self.s.reconcile(dict(args, confirmation='wrong'))
        with patch('desktop.service.ApifyTransport', side_effect=AssertionError('no remote lookup for not-created evidence')):
            result = self.s.dispatch('reconcile', args, 'c'*32)
        self.assertTrue(result['reconciled'])
        self.assertEqual(self.store.jobs()[jid]['finalized_usd'], '0.00')
        self.assertEqual(self.store.jobs()[jid]['state'], 'FAILED')
        self.sync()

    def test_unknown_retired_adapter_can_reconcile_then_bill_without_dataset(self):
        from collector.apify import FakeTransport
        from unittest.mock import Mock, patch, call
        with patch.object(FakeTransport, 'create', side_effect=TimeoutError):
            jid = self.s.start_sync({'account_id':'mock-artist-1'}, 'b'*32)['job_id']
        job = self.store.jobs()[jid]
        job.update(mode='LIVE', actor_id='historic/actor', actor_build='old-build', adapter_version='unavailable-adapter')
        self.store.save_job(job, self.s.context(self.s.accounts()[job['account_id']]))
        policy = next(copy.deepcopy(p) for p in self.s.policies() if p['operation'] == 'access')
        policy.update(version=2, permission_state='REVOKED')
        self.s.save_policy({'policy':policy})
        self.store.close()
        self.store = StorageEngine(self.root);self.addCleanup(self.store.close)
        self.s = WorkerService(self.store, self.private)
        args = {'job_id':jid, 'run_id':'matched-existing-run', 'evidence_uri':'manual://matched-console-run'}
        # 退役适配器不再阻断核对，但实际查询已创建的 Run 仍须 Token。
        with self.assertRaisesRegex(PermissionError, 'LIVE_TRANSPORT_DISABLED_OR_NO_TOKEN'):
            self.s.reconcile(args)
        self.assertEqual(self.store.jobs()[jid]['state'], 'REMOTE_OUTCOME_UNKNOWN')
        transport = Mock()
        transport.run.return_value = {'id':args['run_id'], 'status':'SUCCEEDED'}
        transport.dataset.side_effect = AssertionError('must not read dataset')
        transport.items.side_effect = AssertionError('must not read content')
        self.s.failed_ticks.add(jid)
        with patch('desktop.service.ApifyTransport', return_value=transport):
            self.s.reconcile(args)
            self.assertEqual(self.store.jobs()[jid]['run_id'], args['run_id'])
            self.s.finalize_bill({'job_id':jid, 'actual_usd':'0.10', 'evidence_uri':'manual://final-invoice'})
        self.assertEqual(transport.run.call_args_list, [call(args['run_id']), call(args['run_id'])])
        transport.create.assert_not_called();transport.dataset.assert_not_called();transport.items.assert_not_called()
        self.assertNotIn(jid, self.s.failed_ticks)
        self.assertEqual(self.store.jobs()[jid]['finalized_usd'], '0.10')
        self.assertEqual(self.store.jobs()[jid]['state'], 'FAILED')
    def test_review_stages_and_redistribute(self):
        self.sync();pid=next(iter(self.store.current_posts().values()))['post_id']
        args={'post_id':pid,'status':'PUBLISHABLE','category':'SFW','reviewer':'test','evidence_uri':'mock://review','notes':''}
        with self.assertRaisesRegex(ValueError,'REVIEW_BEFORE'):self.s.review(args)
        args['status']='REVIEWED';self.s.review(args);args['status']='PUBLISHABLE'
        with self.assertRaises(PermissionError):self.s.review(args)
        policy=copy.deepcopy(self.s.policies()[-1]);policy.update({'operation':'redistribute','permission_state':'PERMITTED','version':2})
        self.s.save_policy({'policy':policy});self.s.review(args)
        self.assertEqual(self.s.posts()[0]['review_status'],'PUBLISHABLE')
    def test_policy_bundle_version_keeps_posts(self):
        self.sync();policy=copy.deepcopy(self.s.policies()[0]);policy['version']=2
        self.s.save_policy({'policy':policy})
        self.assertEqual(len(self.store.current_posts()),3)
        self.assertEqual(set(row['version'] for row in self.s.policies()),{2})
        self.assertEqual(self.s.posts()[0]['rights_status'],'PERMITTED')
    def test_publishable_view_rechecks_revoked_and_expired_redistribution(self):
        from unittest.mock import patch
        from collector.migration import export_posts
        self.sync()
        pid = next(iter(self.store.current_posts().values()))['post_id']
        policy = next(copy.deepcopy(p) for p in self.s.policies() if p['operation'] == 'redistribute')
        policy.update(version=2, permission_state='PERMITTED', valid_until='2030-01-01T00:00:00Z')
        self.s.save_policy({'policy':policy})
        review = {'post_id':pid, 'status':'REVIEWED', 'category':'SFW', 'reviewer':'test', 'evidence_uri':'mock://review'}
        self.s.review(review);self.s.review(dict(review, status='PUBLISHABLE'))
        self.assertEqual(self.s.post_detail({'post_id':pid})['review_status'], 'PUBLISHABLE')
        with patch('collector.policy.utc_now', return_value='2031-01-01T00:00:00Z'):
            self.assertEqual(self.s.post_detail({'post_id':pid})['review_status'], 'REVIEWED')
        policy.update(version=3, permission_state='REVOKED')
        self.s.save_policy({'policy':policy})
        self.assertEqual(self.s.post_detail({'post_id':pid})['review_status'], 'REVIEWED')
        self.assertNotIn('PUBLISHABLE', Path(export_posts(self.s)['path']).read_text(encoding='utf-8-sig'))
        self.assertEqual(self.store.admin_events('REVIEW_STATE')[-1]['status'], 'PUBLISHABLE')

    def test_schedule_requires_matching_mode_build_adapter_and_manual_history(self):
        from unittest.mock import Mock
        jid = self.sync()
        original = self.store.jobs()[jid]
        context = self.s.context(self.s.accounts()[original['account_id']])
        for field, value in (('mode','LIVE'), ('actor_id','different/actor'),
                             ('actor_build','older-build'), ('adapter_version','older-adapter'),
                             ('context_hash','0'*64)):
            with self.subTest(field=field):
                self.store.save_job(dict(original, **{field:value}), context)
                with self.assertRaisesRegex(ValueError, 'MANUAL_SYNC_TEST_REQUIRED'):
                    self.s.enable_schedule({'confirmation':'ENABLE'})
        self.store.save_job(original, context)
        self.s.schedule.save({'schedule_key':None, 'claimed_day':'2026-10-25', 'missed_days':0,
            'entries':[{'account_id':original['account_id'], 'request_id':original['command_id'],
                        'status':'DONE', 'job_id':jid, 'error_code':None}]})
        with self.assertRaisesRegex(ValueError, 'MANUAL_SYNC_TEST_REQUIRED'):
            self.s.enable_schedule({'confirmation':'ENABLE'})
        # 历史计划条目不能被清空当前队列伪装成手动验收。
        self.s.schedule.save({'schedule_key':None, 'claimed_day':'2026-10-25', 'missed_days':0, 'entries':[]})
        with self.assertRaisesRegex(ValueError, 'MANUAL_SYNC_TEST_REQUIRED'):
            self.s.enable_schedule({'confirmation':'ENABLE'})
        fresh = self.s.start_sync({'account_id':original['account_id']}, 'c'*32)['job_id']
        for _ in range(5):self.s.tick()
        self.assertEqual(self.store.jobs()[fresh]['state'], 'DONE')
        self.s.backups.registry = Mock(return_value=[{'vault_id':'test'}])
        self.s.backups._vault = Mock()
        self.s.task_backend.register = Mock(return_value={'installed':True})
        self.assertTrue(self.s.enable_schedule({'confirmation':'ENABLE'})['installed'])

    def test_scheduled_start_rechecks_manual_test_after_enable(self):
        self.s.settings['schedule_enabled'] = True
        with self.assertRaisesRegex(ValueError, 'MANUAL_SYNC_TEST_REQUIRED'):
            self.s.start_sync({'account_id':'mock-artist-1', 'scheduled':True}, 'b'*32)
        self.assertEqual(self.store.jobs(), {})
        self.s.settings['schedule_enabled'] = False
        self.sync()
        self.s.settings['schedule_enabled'] = True
        self.s.schedule.step(self.s, now='2026-10-26T18:00:00Z')
        self.assertEqual(self.s.schedule.current()['entries'][0]['status'], 'STARTED')

    def test_schedule_configuration_change_disables_before_backend_failure(self):
        from unittest.mock import Mock
        for field, value in (('mode','LIVE'), ('actor_id','new/actor'), ('actor_build','new-build')):
            with self.subTest(field=field):
                self.s.settings['schedule_enabled'] = True
                self.s.task_backend.disable = Mock(side_effect=RuntimeError('system-task-unavailable'))
                settings = dict(self.s.settings, **{field:value})
                with self.assertRaisesRegex(RuntimeError, 'system-task-unavailable'):
                    self.s.save_settings({'settings':settings, 'revision':digest(self.s.settings)})
                self.assertFalse(self.s.settings['schedule_enabled'])
                self.assertFalse(WorkerService(self.store, self.private).settings['schedule_enabled'])
                self.s.task_backend.disable.assert_called_once()

    def test_cleanup_pending_is_visible_and_clears_after_success(self):
        from unittest.mock import Mock
        complete = {'reports':[], 'active_cleanup_completed':True, 'pending':[], 'backup_policy_sync':[]}
        pending = dict(complete, active_cleanup_completed=False, pending=['ARTIFACT_CHANGED'])
        backup_pending = dict(complete, backup_policy_sync=[{'completed':False}])
        self.s.cleanup = Mock(side_effect=[RuntimeError('interrupted'), pending, complete, backup_pending, complete])
        for expected in ('NEEDS_ATTENTION','NEEDS_ATTENTION','IDLE','NEEDS_ATTENTION','IDLE'):
            self.s.next_retention_sweep = 0
            self.s.tick()
            self.assertEqual(self.s.snapshot()['state'], expected)

    def test_inflight_bill_after_revocation_does_not_read_content(self):
        from unittest.mock import Mock, patch
        jid = self.s.start_sync({'account_id':'mock-artist-1'}, 'b'*32)['job_id']
        job = self.store.jobs()[jid]
        job.update(mode='LIVE', actor_id='historic/actor', actor_build='old-build', adapter_version='unavailable-adapter')
        self.store.save_job(job, self.s.context(self.s.accounts()[job['account_id']]))
        transport = Mock()
        transport.run.return_value = {'id':job['run_id'], 'status':'SUCCEEDED'}
        transport.items.side_effect = AssertionError('must not read content')
        transport.dataset.side_effect = AssertionError('must not read dataset')
        policy = next(copy.deepcopy(p) for p in self.s.policies() if p['operation'] == 'access')
        policy.update(version=2, permission_state='REVOKED')
        self.s.save_policy({'policy':policy})
        self.s.tick()
        self.assertIn(jid, self.s.failed_ticks)
        with patch('desktop.service.ApifyTransport', return_value=transport):
            self.s.finalize_bill({'job_id':jid, 'actual_usd':'0.10', 'evidence_uri':'manual://invoice'})
        self.assertNotIn(jid, self.s.failed_ticks)
        self.assertEqual(self.store.jobs()[jid]['state'], 'FAILED')
        self.assertEqual(self.store.jobs()[jid]['finalized_usd'], '0.10')
        transport.run.assert_called_once_with(job['run_id'])
        transport.items.assert_not_called();transport.dataset.assert_not_called()
    def test_revoke_retain_requires_confirmation(self):
        self.sync();p=copy.deepcopy(self.s.policies()[1]);p.update({'version':2,'permission_state':'REVOKED'})
        with self.assertRaisesRegex(ValueError,'CONFIRM'):self.s.save_policy({'policy':p})
        self.s.save_policy({'policy':p,'confirmation':p['policy_id']});self.assertEqual(self.store.current_posts(),{})
    def test_token_excluded_from_logs_snapshot(self):
        token='placeholder-test-secret';self.s.dispatch('save_token',{'token':token},'d'*32)
        self.assertTrue(self.s.snapshot()['token_configured']);self.assertNotIn(token,str(self.s.snapshot()))
        exported=self.s.export_diagnostics()
        self.assertNotIn(token,Path(exported['path']).read_text())
        self.assertNotIn('APIFY_TOKEN',Path(exported['path']).read_text())
        for p in (self.root/'journal/segments').glob('*.jsonl'):self.assertNotIn(token,p.read_text())
        if os.name!='nt':self.assertEqual((self.private/'.env').stat().st_mode&0o777,0o600)
    def test_limits_and_scheduler_disabled(self):
        settings=copy.deepcopy(self.s.settings);settings['schedule_enabled']=True
        with self.assertRaises(ValueError):self.s.save_settings({'settings':settings,'revision':digest(self.s.settings)})
        settings['schedule_enabled']=False;settings['min_free_bytes']=1
        with self.assertRaises(ValueError):self.s.save_settings({'settings':settings,'revision':digest(self.s.settings)})
    def test_composed_filter_and_paging(self):
        self.sync();result=self.s.query({'kind':'posts','filters':{'post_type':'ORIGINAL','from':'2026-10-24','until':'2026-10-24'},'limit':1})
        self.assertEqual(result['total'],3);self.assertEqual(len(result['rows']),1);self.assertNotIn('text',result['rows'][0])
        with self.assertRaises(ValueError):self.s.query({'kind':'posts','limit':1000})
    def test_deleted_review_notes_purged(self):
        self.sync();pid=next(iter(self.store.current_posts().values()))['post_id']
        self.s.review({'post_id':pid,'status':'REVIEWED','category':'SFW','reviewer':'test','evidence_uri':'mock://review','notes':'unique-deleted-review'})
        self.s.delete_post({'post_id':pid,'confirmation':pid})
        for p in (self.root/'journal/segments').glob('*.jsonl'):self.assertNotIn('unique-deleted-review',p.read_text())
    def test_live_requires_confirmation(self):
        self.s.settings['mode']='LIVE'
        with self.assertRaises(PermissionError):self.s.start_sync({'account_id':'mock-artist-1'},'e'*32)
        self.assertEqual(self.store.jobs(),{})
    def test_preview_rechecks_revoked_rights(self):
        self.sync();pid=next(iter(self.store.current_posts().values()))['post_id']
        mid=self.s.queue_media({'post_id':pid,'url':'https://pbs.twimg.com/mock.png','rating':'SFW','evidence_uri':'mock://rating','expires_at':'2027-01-01T00:00:00Z'})['media_id']
        self.s.process_media({'media_id':mid});self.assertTrue(self.s.preview({'media_id':mid})['path'])
        p=copy.deepcopy(self.s.policies()[2]);p.update({'version':2,'permission_state':'REVOKED'});self.s.save_policy({'policy':p})
        with self.assertRaises(PermissionError):self.s.preview({'media_id':mid})

@unittest.skipUnless(HAS_QT,'Install requirements-desktop.txt')
class QtWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([]);cls.app.setQuitOnLastWindowClosed(False)
    def test_nine_pages_navigation(self):
        class NoClient:
            def call(self,*args):return {'rows':[],'total':0,'offset':0}
        w=MainWindow(NoClient(),polling=False);w.show();wait_ui(60)
        self.assertEqual(w.stack.count(),9);self.assertEqual(w.navigation.accessibleName(),'主导航')
        for i in range(9):w.navigation.setCurrentRow(i);wait_ui(15);self.assertEqual(w.stack.currentIndex(),i)
        w.close();w.pool.waitForDone(1000);wait_ui(30)
    def test_table_selection_sort_clear(self):
        p=DataPage('帖子','说明',[('post_id','ID'),('account_id','账号')]);p.populate({'rows':[{'post_id':'2','account_id':'b'},{'post_id':'1','account_id':'a'}],'total':2})
        p.show();p.table.sortItems(0,Qt.SortOrder.AscendingOrder);p.table.selectRow(0)
        self.assertEqual(p.selected()['post_id'],'1');p.search.setText('query');self.assertEqual(p.query_args('posts')['search'],'query')
        p.search.clear();self.assertEqual(p.offset,0);p.close()
    def test_form_preserves_errors_and_masks_secret(self):
        d=EditorDialog(None,'测试',[('token','Token','secret',''),('mode','模式',['OFFLINE','LIVE'],'OFFLINE')]);d.show();d.widgets['token'].setText('placeholder')
        self.assertEqual(d.widgets['token'].echoMode(),QLineEdit.EchoMode.Password)
        calls=[];d.submitted.connect(calls.append);QTest.mouseClick(d.box.button(QDialogButtonBox.StandardButton.Save),Qt.MouseButton.LeftButton)
        self.assertTrue(d.busy);self.assertEqual(len(calls),1);d.failed('VALIDATION_ERROR')
        self.assertEqual(d.widgets['token'].text(),'placeholder');self.assertFalse(d.busy);self.assertTrue(d.error.isVisible())
        d.original=d.values();d.close()
    def test_native_combo_popup(self):
        d=EditorDialog(None,'模式',[('mode','模式',['OFFLINE','LIVE'],'OFFLINE')]);d.show();c=d.widgets['mode'];c.setFocus();c.showPopup();wait_ui(30)
        self.assertTrue(c.view().isVisible());QTest.keyClick(c,Qt.Key.Key_Escape);c.setCurrentIndex(1);self.assertEqual(d.values()['mode'],'LIVE')
        d.original=d.values();d.close()
    def test_unknown_reconciliation_form_keeps_confirmation_on_error(self):
        class NoClient:
            def call(self, command, *args):
                if command == 'reconcile':
                    raise RuntimeError('NOT_CREATED_REQUIRES_EVIDENCE_AND_JOB_CONFIRMATION')
                return {'rows':[], 'total':0, 'offset':0}
        w = MainWindow(NoClient(), polling=False)
        w.resize(960, 640)
        w.show()
        w.selected = lambda kind: {'job_id':'a'*32, 'run_id':None}
        w.reconcile()
        d = w.dialogs[-1]
        self.assertFalse(d.widgets['not_created'].isChecked())
        d.widgets['not_created'].setFocus()
        QTest.keyClick(d.widgets['not_created'], Qt.Key.Key_Space)
        d.widgets['confirmation'].setText('wrong-id')
        d.widgets['evidence_uri'].setText('manual://checked')
        QTest.mouseClick(d.box.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton)
        for _ in range(40):
            wait_ui(25)
            if not d.busy:
                break
        self.assertTrue(d.isVisible())
        self.assertTrue(d.widgets['not_created'].isChecked())
        self.assertEqual(d.widgets['confirmation'].text(), 'wrong-id')
        self.assertIn('EVIDENCE_AND_JOB_CONFIRMATION', d.error.text())
        d.original = d.values()
        QTest.keyClick(d, Qt.Key.Key_Escape)
        w.close();w.pool.waitForDone(1000);wait_ui(30)
    def test_theme_tokens(self):
        from desktop.theme import TOKENS
        for value in TOKENS.values():self.assertIn(value,(ROOT/'DESIGN.md').read_text())

    def test_open_detail_refreshes_redacted_content_without_overlapping_requests(self):
        from PySide6.QtWidgets import QPlainTextEdit
        from PySide6.QtCore import QTimer
        class NoClient:
            def call(self,*args):return {'rows':[], 'total':0, 'offset':0}
        window = MainWindow(NoClient(), polling=False)
        window.selected = lambda kind: {'post_id':'123'}
        requests = []
        def call(command, args=None, success=None, failure=None):
            requests.append((success, failure))
        window.call = call
        window.show_detail('posts')
        requests.pop()[0]({'post_id':'123', 'text':'withdrawn-secret', 'rights_status':'PERMITTED'})
        dialog = window.dialogs[-1]
        text = dialog.findChild(QPlainTextEdit)
        timer = dialog.findChild(QTimer)
        timer.timeout.emit();timer.timeout.emit()
        self.assertEqual(len(requests), 1)
        requests.pop()[0]({'post_id':'123', 'text':None, 'rights_status':'PERMITTED'})
        self.assertNotIn('withdrawn-secret', text.toPlainText())
        self.assertIsNone(json.loads(text.toPlainText())['text'])
        timer.timeout.emit()
        allowed, denied = requests.pop()
        denied('permission-revoked')
        allowed({'post_id':'123', 'text':'stale-secret'})
        self.assertNotIn('stale-secret', text.toPlainText())
        self.assertFalse(timer.isActive())
        window.close();window.pool.waitForDone(1000);wait_ui(30)

    def test_media_reauthorization_form_retains_input_and_submits_same_media(self):
        class NoClient:
            def call(self, *args):return {'rows':[], 'total':0, 'offset':0}
        window = MainWindow(NoClient(), polling=False)
        window.show()
        window.selected = lambda kind: {'media_id':'a'*32, 'content_rating':'SFW',
                                       'expires_at':'2027-01-01T00:00:00Z'}
        calls = []
        def submit(command, args, saved, failed):
            calls.append((command, args))
            if len(calls) == 1:
                failed('REAUTHORIZATION_EVIDENCE_REQUIRED')
            else:
                saved({'reauthorized':True})
        window.call = submit
        window.action_done = lambda *args: None
        window.reauthorize_media()
        dialog = window.dialogs[-1]
        self.assertFalse(dialog.widgets['allow_r18'].isChecked())
        QTest.mouseClick(dialog.box.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton)
        self.assertTrue(dialog.isVisible())
        self.assertIn('EVIDENCE_REQUIRED', dialog.error.text())
        dialog.widgets['evidence_uri'].setText('manual://new-license')
        QTest.mouseClick(dialog.box.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton)
        self.assertFalse(dialog.isVisible())
        self.assertEqual(calls[-1][0], 'reauthorize_media')
        self.assertEqual(calls[-1][1]['media_id'], 'a'*32)
        self.assertEqual(calls[-1][1]['evidence_uri'], 'manual://new-license')
        window.close();window.pool.waitForDone(1000);wait_ui(30)

@unittest.skipUnless(HAS_QT,'Install requirements-desktop.txt')
class ProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'data';self.base=Path(self.tmp.name)/'private'
        self.proc=launch_worker(self.root,self.base);self.addCleanup(self.finish);self.client=Client(self.root,self.base,timeout=650)
        for _ in range(50):
            try:self.client.call('snapshot');break
            except Exception:time.sleep(.05)
        else:self.fail('Worker startup failed')
    def finish(self):
        if self.proc.poll() is None:
            try:self.client.call('stop_worker');self.proc.wait(timeout=5)
            except Exception:self.proc.kill();self.proc.wait()
    def test_real_IPC_and_shutdown(self):
        self.client.call('load_demo');self.assertEqual(self.client.call('snapshot')['accounts'],1)
        self.client.call('pause');self.assertEqual(self.client.call('snapshot')['state'],'PAUSED')
        self.client.call('stop_worker');self.assertEqual(self.proc.wait(timeout=5),0)
    def test_bad_token_denied(self):
        p=endpoint(self.root,self.base);original=p.read_bytes();marker=strict_loads(original);marker['token']='wrong';p.write_text(json.dumps(marker))
        try:
            with self.assertRaisesRegex(RuntimeError,'IPC_AUTH_REQUIRED'):self.client.call('snapshot')
        finally:p.write_bytes(original)
    def test_GUI_exit_keeps_worker_running(self):
        self.client.call('load_demo');self.client.call('start_sync',{'account_id':'mock-artist-1'})
        code="from PySide6.QtWidgets import QApplication; from PySide6.QtCore import QTimer; from desktop.gui import MainWindow; from desktop.ipc import Client; import sys; app=QApplication([]); w=MainWindow(Client(sys.argv[1],sys.argv[2]),polling=False); w.show(); QTimer.singleShot(100,w.close); app.exec()"
        result=subprocess.run([sys.executable,'-c',code,str(self.root),str(self.base)],cwd=ROOT,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIsNone(self.proc.poll())
        for _ in range(60):
            status=self.client.call('snapshot')
            if status['active']==0:break
            time.sleep(.08)
        self.assertEqual(status['posts'],3)
    def test_native_GUI_sync_and_review_full_flow(self):
        window=MainWindow(self.client,polling=False);self.addCleanup(window.close);window.show();window.load_demo()
        for _ in range(80):
            wait_ui(50)
            if window.last_snapshot and window.last_snapshot['accounts']==1:break
        window.show_page(1)
        for _ in range(60):
            wait_ui(50)
            if window.pages['accounts'].table.rowCount():break
        window.pages['accounts'].table.selectRow(0);window.start_selected()
        for _ in range(100):
            wait_ui(50);window.refresh_status()
            if window.last_snapshot and window.last_snapshot['posts']==3 and window.last_snapshot['active']==0:break
        window.show_page(3)
        for _ in range(60):
            wait_ui(50)
            if window.pages['posts'].table.rowCount()==3:break
        self.assertEqual(window.pages['posts'].table.rowCount(),3,str(window.last_snapshot)+' '+window.statusBar().currentMessage()+' '+window.pages['posts'].notice.text())
        window.pages['posts'].table.selectRow(0);window.review();dialog=window.dialogs[-1]
        dialog.widgets['reviewer'].setText('QtTest reviewer');dialog.widgets['evidence_uri'].setText('mock://review')
        dialog.widgets['category'].setCurrentText('SFW')
        QTest.mouseClick(dialog.box.button(QDialogButtonBox.StandardButton.Save),Qt.MouseButton.LeftButton)
        for _ in range(60):
            wait_ui(50)
            if not dialog.isVisible():break
        self.assertFalse(dialog.isVisible(),dialog.error.text())
        result=self.client.call('query',{'kind':'posts','filters':{'review_status':'REVIEWED'}})
        self.assertEqual(result['total'],1)
        window.close();window.pool.waitForDone(2000);wait_ui(50)

    def test_duplicate_intent_after_restart(self):
        self.client.call('load_demo');rid='f'*32;first=self.client.call('start_sync',{'account_id':'mock-artist-1'},rid)
        self.client.call('stop_worker');self.proc.wait(timeout=5);self.proc=launch_worker(self.root,self.base)
        for _ in range(50):
            try:self.client.call('snapshot');break
            except Exception:time.sleep(.05)
        again=self.client.call('start_sync',{'account_id':'mock-artist-1'},rid)
        self.assertEqual(first,again);self.assertEqual(self.client.call('snapshot')['jobs'],1)

if __name__=='__main__':unittest.main()
