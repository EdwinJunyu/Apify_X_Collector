"""Reproducible offline expansion evidence; no live network transport here."""
import copy
import json
import tempfile
import time
from pathlib import Path
from .adapter import StandardFixtureAdapter, ScrapeSageAdapter
from .apify import FakeTransport
from .policy import PolicyGate
from .storage import StorageEngine, atomic_bytes
from .sync import SyncCoordinator
from .validation import canonical, utc_now


def offline_trial(count, output=None):
    if count not in (5, 20, 50, 150):
        raise ValueError('ONLY_APPROVED_EXPANSION_STAGES')
    fixture = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/offline.json').read_bytes())
    began = time.monotonic()
    successes, rows, creates, max_queue = 0, 0, 0, 0
    details = []
    with tempfile.TemporaryDirectory() as directory:
        with StorageEngine(Path(directory) / 'data') as store:
            for index in range(count):
                account = copy.deepcopy(fixture['account'])
                account.update({'account_id': 'trial-' + str(index), 'external_user_id': str(1000 + index)})
                context = copy.deepcopy(fixture['context'])
                context['source_id'] = 'x:' + account['external_user_id']
                policies = []
                for item in fixture['policies']:
                    policy = copy.deepcopy(item)
                    policy['source_id'] = context['source_id']
                    if policy['operation'] == 'access':
                        policy['permission_state'] = 'PERMITTED'
                    policies.append(policy)
                posts = []
                for offset in range(4):
                    post = copy.deepcopy(fixture['post'])
                    pid = str(100000 + index * 10 + offset)
                    post.update({'post_id': pid, 'account_id': account['account_id'],
                        'author_external_id': account['external_user_id'],
                        'source_url': 'https://x.com/' + account['current_handle'] + '/status/' + pid})
                    posts.append(post)
                # Five-account pilot intentionally has one diagnosed failing account: 4/5.
                transport = FakeTransport(posts, cost='0.01', failed=count == 5 and index == 4)
                coordinator = SyncCoordinator(store, transport, StandardFixtureAdapter(), PolicyGate(policies), page_size=2)
                jid = coordinator.start(account, context, now='2026-10-25T00:00:00Z')
                max_queue = max(max_queue, 1)
                for _ in range(10):
                    job = coordinator.advance(jid, account, context)
                    if job['state'] in ('DONE', 'FAILED', 'NEEDS_REVIEW'):
                        break
                creates += transport.creates
                if job['state'] == 'DONE':
                    successes += 1
                details.append({'account_id': account['account_id'], 'state': job['state'],
                    'coverage': job['coverage_status'], 'pages': job['pages'], 'observed_usd': job['observed_usd']})
            rows = store.status()['posts']
            finalized = coordinator.budget()['finalized']
            # Replay is part of the trial, not an inferred assertion.
        with StorageEngine(Path(directory) / 'data') as recovered:
            replay_rows = recovered.status()['posts']
    peak_memory = None
    try:
        import resource, sys
        peak_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != 'darwin':
            peak_memory *= 1024
    except ImportError:
        pass
    report = {'mode': 'MOCK', 'created_at': utc_now(), 'accounts': count, 'successes': successes,
        'post_count': rows, 'replayed_post_count': replay_rows, 'actor_creates': creates,
        'max_concurrent_paid_runs': max_queue, 'max_dataset_page_items': 2, 'peak_process_rss_bytes': peak_memory,
        'mock_finalized_usd': str(finalized), 'elapsed_seconds': round(time.monotonic() - began, 3),
        'details': details, 'passed': successes >= (4 if count == 5 else count) and rows == replay_rows,
        'real_permissions_verified': False, 'G2': 'NOT_RUN', 'G3': 'NOT_RUN'}
    if output:
        atomic_bytes(Path(output), canonical(report))
    return report


def classification_sample():
    fixture = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/offline.json').read_bytes())
    adapter = ScrapeSageAdapter()
    accepted = 0
    for index in range(20):
        expected = ('ORIGINAL', 'QUOTE', 'REPLY', 'REPOST')[index % 4]
        row = {'type': 'tweet', 'id': str(8000 + index), 'authorId': fixture['account']['external_user_id'],
               'authorHandle': fixture['account']['current_handle'], 'createdAt': '2026-10-24T12:00:00Z',
               'text': 'synthetic', 'isReply': expected == 'REPLY', 'isQuote': expected == 'QUOTE',
               'isRetweet': expected == 'REPOST', 'quotedTweetId': '7001' if expected == 'QUOTE' else None,
               'inReplyToTweetId': '7002' if expected == 'REPLY' else None,
               'retweetedTweetId': '7003' if expected == 'REPOST' else None}
        post = adapter.normalize(row, fixture['account'], True)
        if post['post_type'] == expected and post['author_external_id'] == row['authorId'] and post['published_at'] == row['createdAt']:
            accepted += 1
    return {'sample_size': 20, 'correct': accepted, 'accuracy': accepted / 20,
            'critical_identity_or_date_errors': 20 - accepted, 'mode': 'SYNTHETIC_GOLD'}


def assess_live_evidence(report):
    # Reporting only: importing a report never starts a paid Run or changes limits.
    if not isinstance(report, dict) or report.get('mode') != 'LIVE':
        raise ValueError('LIVE_EVIDENCE_REQUIRED')
    accounts = report.get('accounts', [])
    if not isinstance(accounts, list) or len(accounts) != 5:
        raise ValueError('FIVE_PILOT_ACCOUNTS_REQUIRED')
    successes, cost = 0, 0
    from decimal import Decimal
    from .sync import money
    for account in accounts:
        if not account.get('permission_evidence_uri') or not account.get('run_id') or not account.get('final_bill_evidence_uri'):
            raise ValueError('PILOT_EVIDENCE_INCOMPLETE')
        value = money(account['finalized_usd'])
        if value > Decimal('0.50'):
            raise ValueError('PILOT_RUN_OVER_LIMIT')
        cost += value
        if account.get('status') == 'SUCCEEDED':
            successes += 1
    sample = report.get('manual_sample', {})
    passed = successes >= 4 and cost <= Decimal('10.00') and sample.get('count', 0) >= 20 and sample.get('accuracy', 0) >= .95 and sample.get('critical_errors') == 0
    return {'G2': 'EVIDENCE_REVIEW_REQUIRED' if passed else 'FAILED', 'reported_successes': successes,
            'reported_finalized_usd': str(cost), 'authenticity': 'HUMAN_REVIEW_REQUIRED',
            'G3': 'REQUIRES_SEPARATE_STAGE_APPROVAL'}
