import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError
from collector.adapter import StandardFixtureAdapter, ScrapeSageAdapter, ActorRegistry
from collector.apify import FakeTransport, ApifyTransport, RemoteError
from collector.policy import PolicyGate
from collector.storage import StorageEngine
from collector.sync import SyncCoordinator, money

ROOT = Path(__file__).resolve().parents[1]

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'data'
        f = json.loads((ROOT/'fixtures/offline.json').read_bytes())
        self.account, self.context, self.post = f['account'], f['context'], f['post']
        f['policies'][0]['permission_state'] = 'PERMITTED'
        self.gate = PolicyGate(f['policies'])
        self.fake = FakeTransport(self.rows(3))
        self.adapter = StandardFixtureAdapter()

    def rows(self, count):
        rows=[]
        for i in range(count):
            row=copy.deepcopy(self.post)
            row['post_id']=str(1000+i)
            row['source_url']='https://x.com/mock_artist/status/'+row['post_id']
            rows.append(row)
        return rows

    def worker(self, store, **options):
        return SyncCoordinator(store,self.fake,self.adapter,self.gate,page_size=2,**options)

    def start(self, worker):
        return worker.start(self.account,self.context,now='2026-10-25T00:00:00Z')

    def finish(self, worker, jid):
        for _ in range(20):
            job=worker.advance(jid,self.account,self.context)
            if job['state'] in ('DONE','FAILED','NEEDS_REVIEW'):
                return job
        self.fail('not finished')

    def test_billing_survives_permission_revocation_and_handle_change(self):
        with StorageEngine(self.root) as store:
            worker = self.worker(store)
            jid = self.start(worker)
            self.finish(worker, jid)
            job = store.jobs()[jid]
            job['finalized_usd'] = None
            store.save_job(job, self.context)
            worker.gate = PolicyGate([])
            account = dict(self.account, current_handle='renamed_artist')
            worker.finalize_bill(jid, account, self.context, '0.10', 'manual://bill')
            self.assertEqual(store.jobs()[jid]['finalized_usd'], '0.10')
            with self.assertRaises(PermissionError):
                worker.advance(jid, account, self.context)

    def test_inflight_bill_closes_confirmed_remote_terminal_without_content_access(self):
        for reading in (False, True):
            with self.subTest(reading=reading), StorageEngine(self.root / str(reading)) as store:
                worker = self.worker(store)
                jid = self.start(worker)
                if reading:
                    worker.advance(jid, self.account, self.context)
                worker.gate = PolicyGate([])
                account = dict(self.account, current_handle='renamed_artist')
                with self.assertRaises(PermissionError):
                    worker.advance(jid, account, self.context)
                self.fake.dataset = Mock(side_effect=AssertionError('billing must not read Dataset'))
                self.fake.items = Mock(side_effect=AssertionError('billing must not read content'))
                worker.finalize_bill(jid, account, self.context, '0.10', 'manual://final-invoice')
                job = store.jobs()[jid]
                self.assertEqual(job['state'], 'FAILED')
                self.assertEqual(job['error_code'], 'CONTENT_IMPORT_STOPPED_AFTER_BILLING')
                self.assertEqual(job['finalized_usd'], '0.10')
                self.assertFalse(worker.budget()['blocked'])
                self.assertEqual(store.current_posts(), {})
                self.assertIsNone(store.checkpoint(account['account_id']))
                self.fake.dataset.assert_not_called()
                self.fake.items.assert_not_called()
            # 下一个分支独立创建模拟远端，不能继承禁止 Dataset 的 mock。
            self.fake = FakeTransport(self.rows(3))
            with StorageEngine(self.root / str(reading)) as store:
                self.assertEqual(store.jobs()[jid]['finalized_usd'], '0.10')

    def test_inflight_bill_requires_matching_terminal_run_and_valid_evidence(self):
        with StorageEngine(self.root) as store:
            worker = self.worker(store)
            jid = self.start(worker)
            original = store.jobs()[jid]
            self.fake.run = Mock()
            for result in ({'id':'fake-run', 'status':'RUNNING'},
                           {'id':'another-run', 'status':'SUCCEEDED'}):
                self.fake.run.return_value = result
                with self.assertRaises(ValueError):
                    worker.finalize_bill(jid, self.account, self.context, '0.10', 'manual://invoice')
                self.assertEqual(store.jobs()[jid], original)
            self.fake.run.reset_mock()
            for evidence in ('', '  ', None):
                with self.assertRaisesRegex(ValueError, 'BILL_NOT_READY'):
                    worker.finalize_bill(jid, self.account, self.context, '0.10', evidence)
            with self.assertRaises(ValueError):
                worker.finalize_bill(jid, self.account, self.context, '-1.00', 'manual://invoice')
            self.fake.run.assert_not_called()
            self.assertTrue(worker.budget()['blocked'])

    def test_lower_new_run_cap_does_not_reclassify_historical_bill_as_overspend(self):
        with StorageEngine(self.root) as store:
            worker = self.worker(store)
            jid = self.start(worker)
            self.finish(worker, jid)
            reduced = self.worker(store, per_run='0.05')
            self.assertFalse(reduced.budget()['blocked'])
            next_id = self.start(reduced)
            self.assertEqual(store.jobs()[next_id]['reserved_usd'], '0.05')
            # 真正超过自身历史上限的账单仍必须阻断，不能用提高当前限额掩盖。
            self.finish(reduced, next_id)
            self.assertTrue(self.worker(store).budget()['blocked'])

    def test_paged_sync_and_checkpoint(self):
        with StorageEngine(self.root) as store:
            worker=self.worker(store);jid=self.start(worker)
            job=self.finish(worker,jid)
            self.assertEqual(job['state'],'DONE')
            self.assertEqual(job['pages'],2)
            self.assertEqual(store.status()['posts'],3)
            self.assertEqual(store.checkpoint('mock-artist-1')['committed_cursor'],'1002')
            self.assertEqual(job['coverage_status'],'PARTIAL')
            self.assertEqual(str(worker.budget()['finalized']),'0.10')

    def test_rejected_create_releases_budget_and_can_start_after_restart(self):
        transport = ApifyTransport('placeholder-test-only', enabled=True)
        transport.is_mock = True  # 只模拟 HTTP 响应，不调用真实收费接口。
        transport._opener = Mock()
        transport._opener.open.side_effect = HTTPError(
            'https://api.apify.com/v2/acts/test/runs', 401, 'private-body', {}, None)
        with StorageEngine(self.root) as store:
            worker = SyncCoordinator(store, transport, self.adapter, self.gate)
            jid = self.start(worker)
            job = store.jobs()[jid]
            self.assertEqual(job['state'], 'FAILED')
            self.assertEqual(job['error_code'], 'CONFIRMED_NOT_CREATED')
            self.assertEqual(job['finalized_usd'], '0.00')
            self.assertIn('401', job['run_evidence_uri'])
            self.assertFalse(worker.budget()['blocked'])
            self.assertEqual(transport._opener.open.call_count, 1)
        with StorageEngine(self.root) as store:
            worker = self.worker(store)
            self.assertEqual(worker.budget()['reserved'], 0)
            self.assertEqual(self.finish(worker, self.start(worker))['state'], 'DONE')

    def test_deleted_overlap_finishes_without_resurrection_after_restart(self):
        from collector.deletion import DeletionManager
        with StorageEngine(self.root) as store:
            worker = self.worker(store)
            self.finish(worker, self.start(worker))
            DeletionManager(store).purge('1002', self.context)
        with StorageEngine(self.root) as store:
            worker = self.worker(store)
            jid = worker.start(self.account, self.context, now='2026-10-26T00:00:00Z')
            job = self.finish(worker, jid)
            self.assertEqual(job['state'], 'DONE')
            self.assertEqual(job['rejected'], 0)
            self.assertEqual(job['accepted'], 2)
            self.assertNotIn('X:1002', store.current_posts())
            self.assertEqual(store.history('1002'), [])
            self.assertEqual(store.quarantine_records(), [])
            self.assertEqual(store.checkpoint(self.account['account_id'])['last_success_at'],
                             '2026-10-26T00:00:00Z')
            self.assertEqual(self.finish(worker, self.start(worker))['state'], 'DONE')

    def test_stale_content_overlap_finishes_without_rolling_back_or_freezing_account(self):
        current = dict(self.post, text='new trusted version', metrics={'likes':10},
                       observed_at='2026-10-26T12:00:00Z')
        self.fake.rows = [dict(self.post, text='old delayed version', metrics={'likes':1},
                               observed_at='2026-10-25T12:00:00Z')]
        with StorageEngine(self.root) as store:
            store.ingest([current], self.account, self.gate, self.context)
            worker = self.worker(store)
            job = self.finish(worker, self.start(worker))
            self.assertEqual(job['state'], 'DONE')
            self.assertEqual(job['accepted'], 0)
            self.assertEqual(job['rejected'], 0)
            self.assertIsNone(job['candidate_cursor'])
            self.assertEqual(store.quarantine_records(), [])
            self.assertEqual(store.current_posts()['X:' + current['post_id']]['text'], current['text'])
            self.assertEqual(len(store.history(current['post_id'])), 1)
            self.assertEqual(self.finish(worker, self.start(worker))['state'], 'DONE')

    def test_http_create_errors_preserve_unknown_outcomes(self):
        for status in (400, 401, 403, 404, 405, 429, 302, 408, 500, 502, 503):
            with self.subTest(status=status), StorageEngine(self.root / str(status)) as store:
                transport = ApifyTransport('placeholder-test-only', enabled=True)
                transport.is_mock = True
                transport._opener = Mock()
                transport._opener.open.side_effect = HTTPError(
                    'https://api.apify.com/v2/acts/test/runs', status, 'secret-error', {}, None)
                worker = SyncCoordinator(store, transport, self.adapter, self.gate)
                jid = self.start(worker)
                rejected = status in (400, 401, 403, 404, 405, 429)
                self.assertEqual(store.jobs()[jid]['state'],
                                 'FAILED' if rejected else 'REMOTE_OUTCOME_UNKNOWN')
                self.assertEqual(worker.budget()['blocked'], not rejected)
                self.assertEqual(transport._opener.open.call_count, 1)
                self.assertNotIn('secret-error', str(store.jobs()))

    def test_manual_not_created_requires_evidence_and_cannot_erase_known_run(self):
        self.fake.unknown_create = True
        with StorageEngine(self.root) as store:
            worker = self.worker(store)
            jid = self.start(worker)
            for evidence, confirmation in (('', jid), ('  ', jid), ('manual://checked', 'wrong')):
                with self.assertRaisesRegex(ValueError, 'EVIDENCE_AND_JOB_CONFIRMATION'):
                    worker.confirm_not_created(jid, self.account, self.context, evidence, confirmation)
            self.assertTrue(worker.budget()['blocked'])
            with self.assertRaisesRegex(ValueError, 'BILL_NOT_READY'):
                worker.finalize_bill(jid, self.account, self.context, '0.00', 'manual://zero-bill')
            worker.confirm_not_created(jid, self.account, self.context, 'manual://no-run-found-and-verified', jid)
            self.assertEqual(self.fake.creates, 1)
            self.assertFalse(worker.budget()['blocked'])
            with self.assertRaisesRegex(ValueError, 'UNRESOLVED_CREATE'):
                worker.confirm_not_created(jid, self.account, self.context, 'manual://checked', jid)
        self.fake.unknown_create = False
        with StorageEngine(self.root) as store:
            worker = self.worker(store)
            self.assertEqual(store.jobs()[jid]['error_code'], 'CONFIRMED_NOT_CREATED')
            next_id = self.start(worker)
            with self.assertRaisesRegex(ValueError, 'UNRESOLVED_CREATE'):
                worker.confirm_not_created(next_id, self.account, self.context, 'manual://checked', next_id)
            self.assertTrue(worker.budget()['blocked'])

    def test_all_deleted_overlap_commits_checkpoint_without_coverage_claim(self):
        with StorageEngine(self.root) as store:
            for post in self.fake.rows:
                store.tombstone(post['post_id'], self.context)
            worker = self.worker(store)
            job = self.finish(worker, self.start(worker))
            self.assertEqual(job['state'], 'DONE')
            self.assertEqual(job['accepted'], 0)
            self.assertEqual(job['coverage_status'], 'UNKNOWN')
            self.assertIsNone(job['candidate_cursor'])
            self.assertIsNotNone(store.checkpoint(self.account['account_id']))
            self.assertEqual(store.current_posts(), {})

    def test_unknown_POST_never_retried_across_restart(self):
        self.fake.unknown_create=True
        with StorageEngine(self.root) as store:
            worker=self.worker(store);jid=self.start(worker)
        with StorageEngine(self.root) as store:
            worker=self.worker(store)
            with self.assertRaises(RuntimeError):worker.advance(jid,self.account,self.context)
            with self.assertRaises(RuntimeError):self.start(worker)
            self.assertEqual(self.fake.creates,1)
            self.assertEqual(str(worker.budget()['reserved']),'0.50')

    def test_manual_unknown_run_reconciliation(self):
        self.fake.unknown_create=True
        with StorageEngine(self.root) as store:
            worker=self.worker(store);jid=self.start(worker)
            worker.reconcile_run(jid,self.account,self.context,'fake-run','mock://matched-fingerprint')
            self.assertEqual(self.finish(worker,jid)['state'],'DONE')
            self.assertEqual(self.fake.creates,1)

    def test_page_failure_resume_without_new_run(self):
        with StorageEngine(self.root) as store:
            worker=self.worker(store);jid=self.start(worker)
            worker.advance(jid,self.account,self.context)
            worker.advance(jid,self.account,self.context)
            self.fake.fail_offset=2
            with self.assertRaises(RemoteError):worker.advance(jid,self.account,self.context)
            self.assertEqual(store.status()['posts'],2)
            self.assertIsNone(store.checkpoint('mock-artist-1'))
        self.fake.fail_offset=None
        with StorageEngine(self.root) as store:
            worker=self.worker(store)
            self.assertEqual(self.finish(worker,jid)['state'],'DONE')
            self.assertEqual(self.fake.offsets,[0,2,2])
            self.assertEqual(self.fake.creates,1)

    def test_normalization_failure_preserves_valid_posts_no_cursor(self):
        self.fake.rows[1]={'bad':'secret-raw-marker'}
        with StorageEngine(self.root) as store:
            worker=self.worker(store);jid=self.start(worker)
            self.assertEqual(self.finish(worker,jid)['state'],'NEEDS_REVIEW')
            self.assertEqual(store.status()['posts'],2)
            self.assertEqual(store.status()['quarantined'],1)
            self.assertIsNone(store.checkpoint('mock-artist-1'))
        for path in (self.root/'journal/segments').glob('*.jsonl'):
            self.assertNotIn('secret-raw-marker',path.read_text())

    def test_budget_boundaries_and_unsettled_block(self):
        with StorageEngine(self.root) as store:
            worker=self.worker(store,total='0.50');jid=self.start(worker)
            with self.assertRaises(RuntimeError):self.start(worker)
            self.finish(worker,jid)
            with self.assertRaises(RuntimeError):self.start(worker)

    def test_failed_run_does_not_touch_cursor(self):
        self.fake.failed=True
        with StorageEngine(self.root) as store:
            worker=self.worker(store);jid=self.start(worker)
            self.assertEqual(self.finish(worker,jid)['state'],'FAILED')
            self.assertIsNone(store.checkpoint('mock-artist-1'))

    def test_overlap_window(self):
        with StorageEngine(self.root) as store:
            worker=self.worker(store);jid=self.start(worker);self.finish(worker,jid)
            next_id=worker.start(self.account,self.context,now='2026-10-26T00:00:00Z')
            job=store.jobs()[next_id]
            self.assertEqual(job['window_start'],'2026-10-23T00:00:00Z')
            self.assertEqual(job['previous_cursor'],'1002')

    def test_empty_dataset_not_verified(self):
        self.fake.rows=[]
        with StorageEngine(self.root) as store:
            worker=self.worker(store);jid=self.start(worker)
            self.assertEqual(self.finish(worker,jid)['coverage_status'],'UNKNOWN')

    def test_policy_precedes_any_network(self):
        self.gate.policies[('mock-retain','x:101','access')]['permission_state']='UNKNOWN'
        with StorageEngine(self.root) as store:
            with self.assertRaises(PermissionError):self.start(self.worker(store))
            self.assertEqual(self.fake.creates,0)
            self.assertEqual(store.jobs(),{})

    def test_changed_account_cannot_resume_job(self):
        with StorageEngine(self.root) as store:
            worker=self.worker(store);jid=self.start(worker)
            self.account['current_handle']='other'
            with self.assertRaises(PermissionError):worker.advance(jid,self.account,self.context)

    def test_live_transport_requires_explicit_enable_and_token(self):
        for token in ('',None,'bad\nheader'):
            with self.assertRaises(PermissionError):ApifyTransport(token,enabled=True)
        with self.assertRaises(PermissionError):ApifyTransport('test-placeholder')

    def test_live_runner_requires_price_review_before_intent(self):
        self.fake.is_mock=False
        with StorageEngine(self.root) as store:
            with self.assertRaises(PermissionError):self.start(self.worker(store))
            self.assertEqual(self.fake.creates,0)

    def test_money_rejects_float_nan_negative_and_excess_limits(self):
        for value in (0.5,'NaN','-1','Infinity','0.0000001'):
            with self.assertRaises(ValueError):money(value)
        with StorageEngine(self.root) as store:
            with self.assertRaises(ValueError):self.worker(store,total='11.00')

    def test_scrapesage_strict_mapping(self):
        adapter=ScrapeSageAdapter()
        raw={'type':'tweet','id':'1001','authorId':'101','authorHandle':'mock_artist',
             'createdAt':'2026-10-24T12:00:00Z','text':'synthetic','isReply':False,'isRetweet':False,'isQuote':False}
        self.assertEqual(adapter.normalize(raw,self.account,True)['post_type'],'ORIGINAL')
        raw['isQuote']=True;raw['quotedTweetId']='900'
        self.assertEqual(adapter.normalize(raw,self.account,False)['post_type'],'QUOTE')
        self.assertIsNone(adapter.normalize(raw,self.account,False)['text'])
        raw['id']=123
        with self.assertRaises(ValueError):adapter.normalize(raw,self.account,True)
        self.assertFalse(adapter.supports_since_id)

    def test_actor_media_metrics_entities_and_observation_mapping(self):
        raw = {'type':'tweet', 'id':'1001', 'authorId':'101', 'authorHandle':'mock_artist',
               'createdAt':'2026-10-24T12:00:00Z', 'scrapedAt':'2026-10-25T13:00:00+01:00',
               'text':'synthetic', 'isReply':False, 'isRetweet':False, 'isQuote':False,
               'favoriteCount':12, 'retweetCount':3, 'replyCount':0, 'quoteCount':2, 'viewCount':None,
               'hashtags':['art', 'art'], 'mentions':['artist'],
               'media':[{'type':'photo', 'url':'https://pbs.twimg.com/media/owned.png'},
                        {'type':'video', 'url':'https://video.twimg.com/owned.mp4'}]}
        adapter = ScrapeSageAdapter()
        post = adapter.normalize(raw, self.account, True)
        self.assertEqual(post['metrics'], {'likes':12, 'retweets':3, 'replies':0, 'quotes':2})
        self.assertEqual(post['hashtags'], ['art']);self.assertEqual(post['mentions'], ['artist'])
        self.assertEqual(post['media_refs'], [item['url'] for item in raw['media']])
        self.assertEqual(post['observed_at'], '2026-10-25T12:00:00Z')
        restricted = adapter.normalize(raw, self.account, False, False)
        self.assertIsNone(restricted['text']);self.assertEqual(restricted['media_refs'], [])
        self.assertEqual(restricted['metrics']['likes'], 12)
        legacy = ScrapeSageAdapter('scrapesage-1').normalize(raw, self.account, True)
        self.assertEqual((legacy['metrics'], legacy['media_refs']), ({}, []))
        del raw['authorId']
        with self.assertRaisesRegex(ValueError, 'RAW_IDENTITY'):
            adapter.normalize(raw, self.account, True)

    def test_actor_mapping_reaches_storage_and_overlap_updates_observation_only(self):
        raw = {'type':'tweet', 'id':'1001', 'authorId':'101', 'authorHandle':'mock_artist',
               'createdAt':'2026-10-24T12:00:00Z', 'scrapedAt':'2026-10-25T00:00:00Z',
               'text':'synthetic', 'favoriteCount':1,
               'media':[{'url':'https://pbs.twimg.com/owned.png'}]}
        policies = copy.deepcopy(list(self.gate.policies.values()))
        for policy in policies:
            if policy['operation'] == 'retain':policy['content_scope'] = ['METADATA','TEXT','MEDIA']
        fake = FakeTransport([raw])
        with StorageEngine(self.root) as store:
            worker = SyncCoordinator(store, fake, ScrapeSageAdapter(), PolicyGate(policies))
            first = self.start(worker);self.finish(worker, first)
            post = store.current_posts()['X:1001']
            self.assertEqual(post['media_refs'], ['https://pbs.twimg.com/owned.png'])
            self.assertEqual(post['metrics']['likes'], 1)
            self.assertEqual(store.media_tasks(), {})
            raw.update(favoriteCount=5, scrapedAt='2026-10-26T00:00:00Z')
            second = worker.start(self.account,self.context,now='2026-10-26T00:00:00Z')
            self.finish(worker,second)
            self.assertEqual(store.current_posts()['X:1001']['metrics']['likes'], 5)
            self.assertEqual(len(store.history('1001')), 1)
            self.assertEqual(store.jobs()[second]['adapter_version'], 'scrapesage-2')

    def test_actor_optional_field_drift_and_unsafe_media_fail_closed(self):
        raw = {'type':'tweet', 'id':'1001', 'authorId':'101', 'authorHandle':'mock_artist',
               'createdAt':'2026-10-24T12:00:00Z', 'text':'synthetic'}
        for value in (-1, True, '12', 1.5):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'RAW_METRICS'):
                ScrapeSageAdapter().normalize(dict(raw, favoriteCount=value), self.account, True)
        for url in ('http://pbs.twimg.com/a.png', 'https://127.0.0.1/a',
                    'https://pbs.twimg.com@evil.test/a', 'https://pbs.twimg.com/a#secret'):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'RAW_MEDIA_URL'):
                ScrapeSageAdapter().normalize(dict(raw, media=[{'url':url}]), self.account, True)
        for field, value in (('hashtags',[{'text':'art'}]), ('mentions','artist'), ('media','drift')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                ScrapeSageAdapter().normalize(dict(raw, **{field:value}), self.account, True)

    def test_registry_rejects_unsupported(self):
        registry=ActorRegistry();registry.register('primary',self.adapter)
        self.assertIs(registry.get('primary'),self.adapter)
        with self.assertRaises(ValueError):registry.get('missing')

    def test_exact_ten_dollar_budget_blocks_twenty_first_run(self):
        self.fake.cost='0.50'
        self.fake.rows=[]
        with StorageEngine(self.root) as store:
            worker=self.worker(store)
            for _ in range(20):
                jid=self.start(worker);self.finish(worker,jid)
            self.assertEqual(str(worker.budget()['finalized']),'10.00')
            with self.assertRaises(RuntimeError):self.start(worker)
            self.assertEqual(self.fake.creates,20)

    def test_unknown_bill_blocks_until_evidence_finalized(self):
        with StorageEngine(self.root) as store:
            worker=self.worker(store);jid=self.start(worker)
            self.fake.is_mock=False
            self.finish(worker,jid)
            self.assertTrue(worker.budget()['blocked'])
            worker.finalize_bill(jid,self.account,self.context,'0.12','mock://final-invoice')
            self.assertFalse(worker.budget()['blocked'])
            self.assertEqual(store.jobs()[jid]['bill_evidence_uri'],'mock://final-invoice')

    def test_review_retry_reuses_run(self):
        original=copy.deepcopy(self.fake.rows)
        self.fake.rows[1]={}
        with StorageEngine(self.root) as store:
            worker=self.worker(store);jid=self.start(worker)
            self.finish(worker,jid)
            self.fake.rows=original
            worker.retry_review(jid,self.account,self.context)
            self.assertEqual(self.finish(worker,jid)['state'],'DONE')
            self.assertEqual(store.status()['posts'],3)
            self.assertEqual(self.fake.creates,1)

    def test_twenty_accounts_thirteenth_failure_keeps_others(self):
        with StorageEngine(self.root) as store:
            for index in range(20):
                account=copy.deepcopy(self.account);context=copy.deepcopy(self.context)
                account['account_id']='account-'+str(index)
                account['external_user_id']=str(200+index)
                context['source_id']='x:'+account['external_user_id']
                policies=[]
                for policy in self.gate.policies.values():
                    item=copy.deepcopy(policy);item['source_id']=context['source_id'];policies.append(item)
                row=copy.deepcopy(self.post);row['account_id']=account['account_id']
                row['author_external_id']=account['external_user_id'];row['post_id']=str(5000+index)
                row['source_url']='https://x.com/mock_artist/status/'+row['post_id']
                fake=FakeTransport([row],failed=index==12)
                worker=SyncCoordinator(store,fake,self.adapter,PolicyGate(policies))
                jid=worker.start(account,context,now='2026-10-25T00:00:00Z')
                for _ in range(5):
                    job=worker.advance(jid,account,context)
                    if job['state'] in ('DONE','FAILED'):break
            self.assertEqual(store.status()['posts'],19)
            self.assertIsNone(store.checkpoint('account-12'))
            self.assertIsNotNone(store.checkpoint('account-11'))
            self.assertIsNotNone(store.checkpoint('account-19'))

    def test_http_transport_auth_cap_and_no_post_retry(self):
        from unittest.mock import Mock
        transport=ApifyTransport('placeholder-test-only',enabled=True)
        response=Mock()
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        response.read.return_value=b'{"data":{"id":"run1"}}'
        transport._opener=Mock();transport._opener.open.return_value=response
        transport.create('owner/actor','1.0',{},'0.50')
        request=transport._opener.open.call_args[0][0]
        self.assertEqual(request.get_header('Authorization'),'Bearer placeholder-test-only')
        self.assertNotIn('placeholder-test-only',request.full_url)
        self.assertIn('maxTotalChargeUsd=0.50',request.full_url)
        transport._opener.open.side_effect=TimeoutError()
        with self.assertRaises(RemoteError):transport.create('owner/actor','1.0',{},'0.50')
        self.assertEqual(transport._opener.open.call_count,2)

if __name__ == '__main__':
    unittest.main()
