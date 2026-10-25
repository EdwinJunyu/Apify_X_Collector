import copy
import json
import tempfile
import unittest
from pathlib import Path
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
