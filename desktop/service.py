"""Worker-owned application service. UI never receives a storage writer."""
import base64
import copy
import csv
import io
import os
import uuid
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from collector.adapter import ScrapeSageAdapter, StandardFixtureAdapter
from collector.apify import ApifyTransport, FakeTransport
from collector.deletion import DeletionManager
from collector.media import MediaManager
from collector.media_http import FakeMediaHTTP, MediaHTTP
from collector.policy import PolicyGate
from collector.quota import Quota, GiB
from collector.sync import SyncCoordinator, money
from collector.validation import check, digest, strict_loads, utc_now, parse_utc
from collector.storage import atomic_bytes
from collector.backup import BackupManager
from collector.scheduler import ScheduleEngine, WindowsTasks
from collector.security import secure_path

ADMIN = {'source_id': 'x:0', 'policy_id': 'local-admin', 'policy_version': 1, 'purpose': 'local-management'}
TERMINAL = ('DONE', 'FAILED', 'NEEDS_REVIEW')
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAmElEQVR4nO3PsQmAUBTFULdwRcF53UFwCRf4r7CKgcAtU9yz7eex3PXcy/2t3/52KAB9KAB9KAB96DPAcnTqA9B9ALoPQPd+gOXo1Aeg+wB0H4Du/QDL0akPQPcB6D4A3fsBlqNTH4DuA9B9ALr3AyxHpz4A3Qeg+wB07wdYjk59ALoPQPcB6N4PsByd+gB0H4DuA9C9HvACcnmRpUYwIfwAAAAASUVORK5CYII=')


def defaults():
    return {'mode': 'OFFLINE', 'paused': False, 'schedule_enabled': False,
        'schedule_local_time': '09:00', 'timezone': 'America/Vancouver', 'backup_root': None,
        'actor_id': 'scrapesage/twitter-scraper', 'actor_build': 'unverified',
        'price_evidence_uri': None, 'price_valid_until': '2020-01-01T00:00:00Z',
        'price_minimum_usd': '0.00', 'cap_supported': False,
        'per_run_usd': '0.50', 'pilot_usd': '10.00', 'quota_bytes': 50 * GiB,
        'min_free_bytes': 10 * GiB, 'max_file_bytes': 250 * 1024 ** 2,
        'batch_bytes': GiB, 'overlap_hours': 48}


class WorkerService:
    def __init__(self, store, private_dir):
        self.store = store
        self.private_dir = Path(private_dir)
        secure_path(self.private_dir, directory=True)
        self.stopping = False
        self.runtimes = {}
        self.failed_ticks = set()
        self.settings = defaults()
        for state in store.admin_events('SETTINGS_STATE'):
            self.settings = state
        self.apply_quota()
        self.backups = BackupManager(store)
        self.schedule = ScheduleEngine(store)
        self.task_backend = WindowsTasks(store.root, self.private_dir)
        self.backup_health = 'NOT_CONFIGURED' if not self.backups.registry() else 'REGISTERED'
        self.next_retention_sweep = 0

    def apply_quota(self):
        self.store.quota = Quota(self.store.root, self.settings['quota_bytes'], self.settings['min_free_bytes'])

    def accounts(self):
        result = {}
        for row in self.store.admin_events('ACCOUNT_STATE'):
            result[row['account_id']] = row
        return result

    def policies(self):
        result = {}
        for row in self.store.admin_events('POLICY_STATE'):
            result[(row['policy_id'], row['source_id'], row['operation'])] = row
        return list(result.values())

    def gate(self):
        return PolicyGate(self.policies())

    def context(self, account):
        versions = []
        for row in self.policies():
            if row['policy_id'] == account['source_policy_id'] and row['source_id'] == 'x:' + str(account['external_user_id']):
                versions.append(row['version'])
        return {'source_id': 'x:' + str(account['external_user_id']),
                'policy_id': account['source_policy_id'] or 'pending',
                'policy_version': max(versions or [1]), 'purpose': 'acg-research'}

    def _permit_post(self, post):
        account = self.accounts().get(post['account_id'])
        if account is None:
            raise PermissionError('ACCOUNT_NOT_REGISTERED')
        context = self.context(account)
        self.gate().require(context['source_id'], 'retain', context['purpose'], context['policy_id'], context['policy_version'])
        return account, context

    def posts(self):
        reviews = {}
        for row in self.store.admin_events('REVIEW_STATE'):
            reviews[(row['post_id'], row.get('version_number'), row.get('content_hash'))] = row
        result = []
        for post in self.store.current_posts().values():
            try:
                account, context = self._permit_post(post)
                policy = self.gate().require(context['source_id'], 'retain', context['purpose'], context['policy_id'], context['policy_version'])
                post['rights_status'] = 'PERMITTED'
                if 'TEXT' not in policy['content_scope']:
                    post['text'] = None
                # 旧的无版本审核仍保留为历史证据，但不能授权当前内容。
                review = reviews.get((post['post_id'], post['version_number'], post['content_hash']), {})
                post['review_status'] = review.get('status', 'STAGING')
                post['content_rating'] = review.get('category', 'UNKNOWN')
                if post['review_status'] == 'PUBLISHABLE':
                    # 审核证据可以保留，但历史发布许可不能覆盖当前撤销或到期状态。
                    try:
                        self.gate().require(context['source_id'], 'redistribute', context['purpose'],
                                            context['policy_id'], context['policy_version'])
                    except PermissionError:
                        post['review_status'] = 'REVIEWED'
            except PermissionError:
                # Only safe identity/status shown, no restricted text or URL.
                post = {'post_id': post['post_id'], 'account_id': post['account_id'],
                        'rights_status': 'RESTRICTED', 'review_status': 'STAGING', 'text': None}
            result.append(post)
        return result

    def read_token(self):
        path = self.private_dir / '.env'
        if not path.exists() or path.stat().st_size > 8192:
            return None
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.startswith('APIFY_TOKEN='):
                value = line.partition('=')[2].strip()
                return value or None
        return None

    def runtime(self, account, job=None, accounting_only=False):
        gate = self.gate()
        mode = self.settings['mode'] if job is None else job.get(
            'mode', 'OFFLINE' if job['actor_id'] == 'offline-synthetic' else 'LIVE')
        if mode == 'OFFLINE':
            adapter = StandardFixtureAdapter()
            rows = []
            for offset in range(3):
                post_id = str(int(account['external_user_id']) * 100000 + offset)
                rows.append({'platform': 'X', 'post_id': post_id, 'account_id': account['account_id'],
                    'author_external_id': account['external_user_id'], 'author_handle_at_observation': account['current_handle'],
                    'post_type': 'ORIGINAL', 'quoted_post_id': None, 'in_reply_to_id': None, 'reposted_post_id': None,
                    'published_at': '2026-10-24T12:00:00Z', 'source_url': 'https://x.com/' + account['current_handle'] + '/status/' + post_id,
                    'language': 'ja', 'text': '合成テスト：新作発表（実際の投稿ではありません）', 'media_refs': [],
                    'metrics': {}, 'hashtags': [], 'mentions': [], 'content_rating': 'UNKNOWN', 'coverage_evidence_id': None, 'raw_ref': None})
            transport = FakeTransport(rows)
        else:
            adapter = ScrapeSageAdapter(job.get('adapter_version', 'scrapesage-1')
                                       if job is not None and not accounting_only else None)
            adapter.actor_id = self.settings['actor_id'] if job is None else job['actor_id']
            adapter.build = self.settings['actor_build'] if job is None else job['actor_build']
            # 核账只登记既有负债，不需要 Token，也不能顺带访问内容。
            transport = None
        if job is not None:
            if (job['actor_id'] != adapter.actor_id or job['actor_build'] != adapter.build or
                    (not accounting_only and job.get('adapter_version', adapter.adapter_version) != adapter.adapter_version)):
                raise PermissionError('HISTORICAL_ADAPTER_UNSUPPORTED')
        if mode == 'LIVE' and not accounting_only:
            transport = ApifyTransport(self.read_token(), enabled=True)
        return SyncCoordinator(self.store, transport, adapter, gate,
                self.settings['per_run_usd'], self.settings['pilot_usd'])

    def coordinator(self, job_id, accounting_only=False):
        job = self.store.jobs()[job_id]
        account = self.accounts()[job['account_id']]
        if accounting_only:
            return self.runtime(account, job, True), account, self.context(account)
        if job_id not in self.runtimes:
            # Recover the existing adapter/run, never create a new Run here.
            runtime = self.runtime(account, job)
            self.runtimes[job_id] = runtime
        self.runtimes[job_id].gate = self.gate()
        return self.runtimes[job_id], account, self.context(account)

    def tick(self):
        if time.monotonic() >= self.next_retention_sweep:
            self.next_retention_sweep = time.monotonic() + 60
            try:
                cleanup = self.cleanup()
                if not cleanup['active_cleanup_completed'] or cleanup['pending']:
                    self.failed_ticks.add('RETENTION_CLEANUP_PENDING')
                else:
                    self.failed_ticks.discard('RETENTION_CLEANUP_PENDING')
                receipts = self.backups.propagate_erasure() if self.backups.registry() else []
                if any(not row['completed'] for row in receipts + cleanup['backup_policy_sync']):
                    self.failed_ticks.add('BACKUP_ERASURE_PENDING')
                else:
                    self.failed_ticks.discard('BACKUP_ERASURE_PENDING')
            except Exception:
                self.failed_ticks.add('RETENTION_CLEANUP_PENDING')
        active_found = False
        for job in self.store.jobs().values():
            if job['state'] in TERMINAL or job['job_id'] in self.failed_ticks:
                continue
            active_found = True
            try:
                worker, account, context = self.coordinator(job['job_id'])
                worker.advance(job['job_id'], account, context)
            except Exception as exc:
                self.failed_ticks.add(job['job_id'])
                self.log('SYNC_STEP', False, type(exc).__name__)
            break  # one bounded step per event-loop tick

        if not self.stopping:
            try:
                self.schedule.step(self)
            except Exception as exc:
                self.failed_ticks.add('SCHEDULE_BLOCKED')

    def log(self, action, success=True, error=None):
        try:
            self.store.save_admin('OPERATION_LOG', {'operation': action, 'outcome': 'SUCCESS' if success else 'FAILED',
                'error_code': error, 'at': utc_now()}, ADMIN)
        except Exception:
            # A failed audit write is surfaced in snapshot, not treated as success.
            self.failed_ticks.add('AUDIT_WRITE_FAILED')

    def snapshot(self):
        budget = Decimal('0')
        reserved = Decimal('0')
        active = 0
        for job in self.store.jobs().values():
            if job['state'] not in TERMINAL:
                active += 1
            if job['finalized_usd'] is None:
                reserved += money(job['reserved_usd'])
            else:
                budget += money(job['finalized_usd'])
        schedule_blocked = any(entry['status'] == 'BLOCKED' for entry in self.schedule.current()['entries'])
        state = 'NEEDS_ATTENTION' if self.failed_ticks or schedule_blocked else ('RUNNING' if active else ('PAUSED' if self.settings['paused'] else 'IDLE'))
        today = utc_now()[:10]
        new_today = 0
        for post in self.store.current_posts().values():
            if post['first_seen_at'][:10] == today:
                new_today += 1
        from collector import __version__
        return {'version': __version__, 'state': state, 'accounts': len(self.accounts()), 'posts': len(self.store.current_posts()),
                'today': new_today, 'jobs': len(self.store.jobs()), 'active': active,
                'media': len(self.store.media_tasks()), 'finalized_usd': str(budget), 'reserved_usd': str(reserved),
                'quota': self.store.quota.status(), 'settings': copy.deepcopy(self.settings),
                'settings_revision': digest(self.settings), 'token_configured': bool(self.read_token()),
                'root': str(self.store.root), 'schedule_status': 'ENABLED' if self.settings['schedule_enabled'] else 'DISABLED', 'backup_status': self.backup_health,
                'schedule': self.schedule.current(), 'approved_stage': self.approved_stage()}

    def query(self, args):
        kind = args.get('kind')
        mapping = {'accounts': lambda: list(self.accounts().values()), 'policies': self.policies,
                   'jobs': lambda: list(self.store.jobs().values()), 'posts': self.posts,
                   'media': lambda: list(self.store.media_tasks().values()),
                   'logs': lambda: self.store.admin_events('OPERATION_LOG'), 'backups': self.list_backups,
                   'deletions': lambda: self.store.admin_events('TOMBSTONE')}
        if kind not in mapping:
            raise ValueError('UNKNOWN_DATASET')
        offset, limit = args.get('offset', 0), args.get('limit', 100)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('PAGING_LIMIT')
        query = str(args.get('search', '')).casefold()[:200]
        filters = args.get('filters', {})
        if not isinstance(filters, dict):
            raise ValueError('INVALID_FILTERS')
        rows = []
        for row in mapping[kind]():
            if query and query not in str(row).casefold():
                continue
            match = True
            for key, value in filters.items():
                if not value:
                    continue
                if key == 'from':
                    match = match and row.get('published_at', '') >= value
                elif key == 'until':
                    match = match and row.get('published_at', '')[:10] <= value
                else:
                    match = match and str(row.get(key, '')) == value
            if match:
                row = copy.deepcopy(row)
                row.pop('text', None)
                rows.append(row)
        rows.reverse()
        return {'rows': rows[offset:offset + limit], 'total': len(rows), 'offset': offset}

    def dispatch(self, command, args, request_id):
        commands = {'snapshot': self.snapshot, 'query': lambda: self.query(args),
            'save_account': lambda: self.save_account(args), 'save_policy': lambda: self.save_policy(args),
            'save_settings': lambda: self.save_settings(args), 'start_sync': lambda: self.start_sync(args, request_id),
            'pause': lambda: self.pause(True), 'resume': lambda: self.pause(False),
            'review': lambda: self.review(args), 'post_detail': lambda: self.post_detail(args),
            'delete_post': lambda: self.delete_post(args), 'recover_job': lambda: self.recover_job(args),
            'reconcile': lambda: self.reconcile(args), 'finalize_bill': lambda: self.finalize_bill(args),
            'queue_media': lambda: self.queue_media(args), 'process_media': lambda: self.process_media(args),
            'repair_media': lambda: self.repair_media(args), 'preview': lambda: self.preview(args),
            'reauthorize_media': lambda: self.reauthorize_media(args),
            'recover': self.recover, 'cleanup': self.cleanup, 'save_token': lambda: self.save_token(args),
            'load_demo': self.load_demo, 'export_diagnostics': self.export_diagnostics, 'stop_worker': self.stop,
            'register_backup': lambda: self.register_backup(args), 'create_backup': lambda: self.create_backup(args),
            'list_backups': self.list_backups, 'verify_backup': lambda: self.verify_backup(args),
            'restore_backup': lambda: self.restore_backup(args), 'enable_schedule': lambda: self.enable_schedule(args),
            'disable_schedule': self.disable_schedule, 'retry_schedule': self.retry_schedule,
            'export_csv': lambda: __import__('collector.migration', fromlist=['export_posts']).export_posts(self),
            'import_legacy': lambda: self.import_legacy(args), 'run_acceptance': self.run_acceptance, 'approve_stage': lambda: self.approve_stage(args)}
        if command not in commands or not isinstance(args, dict):
            raise ValueError('UNSUPPORTED_COMMAND')
        try:
            result = commands[command]()
            if command not in ('snapshot', 'query', 'post_detail', 'preview'):
                self.log(command)
            return result
        except Exception as exc:
            if command not in ('snapshot', 'query', 'post_detail', 'preview'):
                self.log(command, False, type(exc).__name__)
            raise

    def save_account(self, args):
        account = copy.deepcopy(args['account'])
        check('account', account)
        previous = self.accounts().get(account['account_id'])
        if args.get('revision') != (digest(previous) if previous else None):
            raise ValueError('CONFLICT_REFRESH_REQUIRED')
        if previous and previous['external_user_id'] and account['external_user_id'] != previous['external_user_id']:
            raise ValueError('REGISTERED_STABLE_ID_IS_IMMUTABLE')
        if account['verification_status'] == 'VERIFIED' and (not account['external_user_id'] or not account['verified_source_urls']):
            raise ValueError('IDENTITY_EVIDENCE_REQUIRED')
        for other in self.accounts().values():
            if other['account_id'] != account['account_id'] and account['external_user_id'] and other['external_user_id'] == account['external_user_id']:
                raise ValueError('STABLE_ID_ALREADY_REGISTERED')
        for job in self.store.jobs().values():
            if job['account_id'] == account['account_id'] and job['state'] not in TERMINAL:
                raise ValueError('FINISH_JOB_BEFORE_ACCOUNT_EDIT')
        self.store.save_admin('ACCOUNT_STATE', account, ADMIN)
        return {'account_id': account['account_id']}

    def save_policy(self, args):
        policy = copy.deepcopy(args['policy'])
        check('policy', policy)
        if policy['purpose'] != 'acg-research':
            raise ValueError('PURPOSE_MUST_BE_ACG_RESEARCH')
        group = []
        for old in self.policies():
            if (old['policy_id'], old['source_id']) == (policy['policy_id'], policy['source_id']):
                group.append(old)
        maximum = 0
        for old in group:
            maximum = max(maximum, old['version'])
        if policy['version'] != maximum + 1:
            raise ValueError('POLICY_VERSION_CONFLICT')
        narrowed = any(old['operation'] == 'retain' and
                       set(old['content_scope']) - set(policy['content_scope']) for old in group)
        if policy['operation'] == 'retain' and (policy['permission_state'] in ('DENIED', 'REVOKED') or narrowed):
            if args.get('confirmation') != policy['policy_id']:
                raise ValueError('CONFIRM_RETENTION_REVOCATION')
        if policy['permission_state'] == 'PERMITTED':
            if not policy['evidence_uri'] or parse_utc(policy['valid_until']) <= parse_utc(utc_now()):
                raise ValueError('VALID_EVIDENCE_REQUIRED')
            if self.settings['mode'] == 'LIVE' and policy['evidence_uri'].startswith('mock:'):
                raise ValueError('LIVE_REQUIRES_REAL_EVIDENCE')
        events = []
        for old in group:
            if old['operation'] != policy['operation']:
                old['version'] = policy['version']
                events.append(self.store._event('POLICY_STATE', old, ADMIN))
        events.append(self.store._event('POLICY_STATE', policy, ADMIN))
        self.store._commit(events)  # one version across all four dimensions
        # Revoke/expire retains no restricted data in current assets.
        contexts = []
        for account in self.accounts().values():
            if account['source_policy_id'] == policy['policy_id']:
                contexts.append(self.context(account))
        # 先把新许可同步至外盘，随后在当前根和历史快照执行相同清理。
        all_contexts = [self.context(item) for item in self.accounts().values() if item['external_user_id']]
        synced = self.backups.update_policies(self.gate(), all_contexts)
        DeletionManager(self.store).enforce_policy_expiry(self.gate(), contexts)
        if policy['operation'] == 'download_media' and (policy['permission_state'] != 'PERMITTED' or 'MEDIA' not in policy['content_scope']):
            for task in list(self.store.media_tasks().values()):
                if task['source_id'] == policy['source_id']:
                    original = self.context(self.accounts()[task['account_id']])
                    original['policy_version'] = policy['version'] - 1
                    DeletionManager(self.store).purge_media(task['media_id'], original)
        synced.extend(self.backups.propagate_erasure())
        if any(not row['completed'] for row in synced):
            self.backup_health = 'PERMISSION_SYNC_PENDING'
        return {'version': policy['version']}

    def save_settings(self, args):
        settings = copy.deepcopy(args['settings'])
        check('desktop_settings', settings)
        if settings['schedule_enabled'] != self.settings['schedule_enabled']:
            raise ValueError('USE_EXPLICIT_SCHEDULE_ENABLE_DISABLE')
        if args.get('revision') != digest(self.settings):
            raise ValueError('CONFLICT_REFRESH_REQUIRED')
        if (money(settings['per_run_usd']) > Decimal('0.50') or money(settings['pilot_usd']) > Decimal('10.00') or
                settings['quota_bytes'] > 50 * GiB or settings['min_free_bytes'] < 10 * GiB):
            raise ValueError('FROZEN_RESOURCE_LIMIT')
        if money(settings['per_run_usd']) <= 0 or money(settings['per_run_usd']).quantize(Decimal('0.01')) != money(settings['per_run_usd']):
            raise ValueError('RUN_LIMIT_MUST_BE_POSITIVE_CENTS')
        ZoneInfo(settings['timezone'])
        for job in self.store.jobs().values():
            if job['state'] not in TERMINAL and settings['mode'] != self.settings['mode']:
                raise ValueError('FINISH_JOB_BEFORE_MODE_CHANGE')
        schedule_disabled = self.settings['schedule_enabled'] and any(
            settings[key] != self.settings[key] for key in ('mode', 'actor_id', 'actor_build'))
        if schedule_disabled:
            # 模式或 Actor 变化使旧的手动验收失效；先持久化关闭，再触碰系统计划。
            settings['schedule_enabled'] = False
        if settings['schedule_enabled'] and (settings['schedule_local_time'],settings['timezone']) != (self.settings['schedule_local_time'],self.settings['timezone']):
            self.task_backend.register(settings)
        self.store.save_admin('SETTINGS_STATE', settings, ADMIN)
        self.settings = settings
        self.apply_quota()
        if schedule_disabled:
            self.task_backend.disable()
        return {'saved': True, 'schedule_disabled': bool(schedule_disabled)}

    def pause(self, paused):
        state = copy.deepcopy(self.settings)
        state['paused'] = paused
        return self.save_settings({'settings': state, 'revision': digest(self.settings)})

    def start_sync(self, args, request_id):
        for job in self.store.jobs().values():
            if job.get('command_id') == request_id:
                if job['account_id'] != args['account_id']:
                    raise ValueError('REQUEST_ID_REUSED_WITH_DIFFERENT_CONTENT')
                return {'job_id': job['job_id']}
        if args.get('scheduled') and not self.settings['schedule_enabled']:
            raise PermissionError('SCHEDULE_NOT_ENABLED')
        if self.settings['paused'] or self.stopping:
            raise ValueError('NEW_JOBS_PAUSED')
        account = self.accounts()[args['account_id']]
        if account['active_status'] != 'ACTIVE':
            raise ValueError('ACCOUNT_NOT_ACTIVE')
        if self.settings['mode'] == 'LIVE':
            if args.get('confirm_live') is not True:
                raise PermissionError('CONFIRM_LIVE_COST_REQUIRED')
            for policy in self.policies():
                if policy['policy_id'] == account['source_policy_id'] and policy['evidence_uri'].startswith('mock:'):
                    raise PermissionError('MOCK_EVIDENCE_CANNOT_AUTHORIZE_LIVE')
        if self.settings['mode'] == 'LIVE':
            observed_accounts = {account['account_id']}
            for old in self.store.jobs().values():
                if old['actor_id'] != 'offline-synthetic':
                    observed_accounts.add(old['account_id'])
            if len(observed_accounts) > self.approved_stage():
                raise PermissionError('SEEDING_STAGE_APPROVAL_REQUIRED')
        runtime = self.runtime(account)
        if args.get('scheduled') and not self.manual_sync_tested(account, runtime):
            raise ValueError('MANUAL_SYNC_TEST_REQUIRED')
        approval = {'actor_id': self.settings['actor_id'], 'build': self.settings['actor_build'],
            'model': 'PAY_PER_EVENT', 'evidence_uri': self.settings['price_evidence_uri'],
            'valid_until': self.settings['price_valid_until'], 'minimum_usd': self.settings['price_minimum_usd'],
            'cap_supported': self.settings['cap_supported']}
        jid = runtime.start(account, self.context(account), overlap_hours=self.settings['overlap_hours'],
                            live_approval=approval, command_id=request_id)
        self.runtimes[jid] = runtime
        return {'job_id': jid}

    def review(self, args):
        post = self.store.current_posts()['X:' + args['post_id']]
        account, context = self._permit_post(post)
        if (args.get('version_number', post['version_number']) != post['version_number'] or
                args.get('content_hash', post['content_hash']) != post['content_hash']):
            raise ValueError('REVIEW_CONTENT_CHANGED_REFRESH_REQUIRED')
        current = 'STAGING'
        for row in self.store.admin_events('REVIEW_STATE'):
            if (row['post_id'] == args['post_id'] and row.get('version_number') == post['version_number']
                    and row.get('content_hash') == post['content_hash']):
                current = row['status']
        status = args['status']
        if status == 'PUBLISHABLE':
            if args.get('category', post['content_rating']) == 'UNKNOWN':
                raise ValueError('CONTENT_RATING_REVIEW_REQUIRED')
            if current != 'REVIEWED':
                raise ValueError('REVIEW_BEFORE_PUBLISHABLE')
            self.gate().require(context['source_id'], 'redistribute', context['purpose'], context['policy_id'], context['policy_version'])
        row = {'post_id': args['post_id'], 'status': status, 'reviewer': args['reviewer'],
                'version_number': post['version_number'], 'content_hash': post['content_hash'],
                'category': args.get('category', post['content_rating']), 'notes': args.get('notes'), 'evidence_uri': args['evidence_uri'], 'reviewed_at': utc_now()}
        self.store.save_admin('REVIEW_STATE', row, context)
        return {'status': status}

    def post_detail(self, args):
        for row in self.posts():
            if row['post_id'] == args['post_id']:
                if row['rights_status'] != 'PERMITTED':
                    raise PermissionError('RETAIN_PERMISSION_REQUIRED')
                return row
        raise ValueError('POST_NOT_AVAILABLE')

    def delete_post(self, args):
        if args.get('confirmation') != args['post_id']:
            raise ValueError('TYPE_POST_ID_TO_CONFIRM')
        post = self.store.current_posts()['X:' + args['post_id']]
        account = self.accounts()[post['account_id']]
        return DeletionManager(self.store).purge(args['post_id'], self.context(account))

    def recover_job(self, args):
        jid = args['job_id']
        worker, account, context = self.coordinator(jid)
        if self.store.jobs()[jid]['state'] == 'NEEDS_REVIEW':
            worker.retry_review(jid, account, context)
        self.failed_ticks.discard(jid)
        return {'job_id': jid}

    def reconcile(self, args):
        # 人工核对已有 Run 也只需要账务身份和 Run API，不依赖旧 Dataset 映射。
        worker, account, context = self.coordinator(args['job_id'], accounting_only=True)
        if args.get('not_created') is True:
            if args.get('run_id'):
                raise ValueError('NOT_CREATED_MUST_NOT_HAVE_RUN_ID')
            worker.confirm_not_created(args['job_id'], account, context,
                                       args['evidence_uri'], args.get('confirmation'))
        else:
            if worker.transport is None:
                worker.transport = ApifyTransport(self.read_token(), enabled=True)
            worker.reconcile_run(args['job_id'], account, context, args['run_id'], args['evidence_uri'])
        self.failed_ticks.discard(args['job_id'])
        return {'reconciled': True}

    def finalize_bill(self, args):
        job = self.store.jobs()[args['job_id']]
        # 在途任务需查询原 Run 已结束；既有终态账单仍可无 Token 登记。
        remote_check = job['state'] in ('RUNNING', 'READING') and job['run_id'] is not None
        worker, account, context = self.coordinator(args['job_id'], accounting_only=True)
        if remote_check and worker.transport is None:
            # 核对负债只需要 Run API；即使旧内容适配器已不可用也可安全结算。
            worker.transport = ApifyTransport(self.read_token(), enabled=True)
        worker.finalize_bill(args['job_id'], account, context, args['actual_usd'], args['evidence_uri'])
        self.failed_ticks.discard(args['job_id'])
        return {'finalized': True}

    def media_manager(self):
        transport = FakeMediaHTTP(PNG) if self.settings['mode'] == 'OFFLINE' else MediaHTTP()
        return MediaManager(self.store, self.gate(), transport, self.settings['max_file_bytes'], self.settings['batch_bytes'])

    def media_context(self, task):
        return self.context(self.accounts()[task['account_id']])

    def queue_media(self, args):
        post = self.store.current_posts()['X:' + args['post_id']]
        account, context = self._permit_post(post)
        mid = self.media_manager().enqueue(args['post_id'], args['url'], context, args['rating'],
            args['evidence_uri'], args['expires_at'], restricted=args.get('restricted', False), allow_r18=args.get('allow_r18', False))
        return {'media_id': mid}

    def process_media(self, args):
        task = self.store.media_tasks()[args['media_id']]
        return self.media_manager().process(args['media_id'], self.media_context(task))

    def repair_media(self, args):
        task = self.store.media_tasks()[args['media_id']]
        self.media_manager().repair(args['media_id'], self.media_context(task), args['url'], args['rating'],
            args['evidence_uri'], args['expires_at'], args.get('allow_r18', False))
        return {'repaired': True}

    def preview(self, args):
        task = self.store.media_tasks().get(args['media_id'])
        if task is None:
            raise PermissionError('MEDIA_UNAVAILABLE_OR_ERASED')
        path = self.media_manager().preview_path(args['media_id'], self.media_context(task))
        return {'path': str(path), 'mime': task['mime'], 'media_id': task['media_id']}

    def reauthorize_media(self, args):
        task = self.store.media_tasks()[args['media_id']]
        self.media_manager().reauthorize(task['media_id'], self.media_context(task), args['rating'],
            args['evidence_uri'], args['expires_at'], args.get('allow_r18', False))
        return {'reauthorized': True}

    def recover(self):
        self.store.recover()
        return {'verified': True, 'status': self.store.status()}

    def cleanup(self):
        deletion = DeletionManager(self.store)
        progress = deletion.resume_cleanup()
        contexts = {}
        for account in self.accounts().values():
            context = self.context(account)
            contexts[digest(context)] = context
        reports = deletion.expire(contexts)
        reports.extend(deletion.enforce_policy_expiry(self.gate(), list(contexts.values())))
        progress = deletion.resume_cleanup()
        synced = self.backups.update_policies(self.gate(), list(contexts.values())) if self.backups.registry() else []
        return {'reports': reports, 'active_cleanup_completed': progress['active_cleanup_completed'],
                'pending': progress['pending'], 'backup_policy_sync': synced}

    def save_token(self, args):
        token = args['token']
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(c.isspace() for c in token):
            raise ValueError('INVALID_TOKEN')
        atomic_bytes(self.private_dir / '.env', ('APIFY_TOKEN=' + token + '\n').encode('utf-8'))
        secure_path(self.private_dir / '.env')
        return {'token_configured': True}

    def load_demo(self):
        if self.settings['mode'] != 'OFFLINE':
            raise ValueError('DEMO_OFFLINE_ONLY')
        fixture = strict_loads((Path(__file__).resolve().parents[1] / 'fixtures/offline.json').read_bytes())
        account = fixture['account']
        if account['account_id'] in self.accounts():
            return {'already_loaded': True}
        self.save_account({'account': account, 'revision': None})
        for policy in fixture['policies']:
            policy['purpose'] = 'acg-research'
            if policy['operation'] != 'redistribute':
                policy['permission_state'] = 'PERMITTED'
                policy['content_scope'] = ['METADATA', 'TEXT', 'MEDIA']
            self.store.save_admin('POLICY_STATE', policy, ADMIN)
        return {'loaded': True}

    def stop(self):
        self.stopping = True
        return {'stopping': True}

    def export_diagnostics(self):
        state = self.snapshot()
        safe = {'schema_version': '1.0', 'at': utc_now(), 'state': state['state'],
                'mode': self.settings['mode'], 'accounts': state['accounts'], 'posts': state['posts'],
                'jobs': state['jobs'], 'active_jobs': state['active'], 'media': state['media'],
                'finalized_usd': state['finalized_usd'], 'reserved_usd': state['reserved_usd'],
                'quota': state['quota'], 'token_configured': state['token_configured'],
                'operations': self.store.admin_events('OPERATION_LOG')[-100:]}
        path = self.store._path('exports/diagnostic-' + uuid.uuid4().hex + '.json')
        from collector.validation import canonical
        data = canonical(safe)
        self.store.quota.require(len(data) + 65536)
        atomic_bytes(path, data)
        DeletionManager(self.store).track_artifact([], path.relative_to(self.store.root).as_posix())
        return {'path': str(path), 'filename': path.name}

    def backup_row(self, args):
        for row in self.backups.registry():
            if row['vault_id'] == args['vault_id']:
                return row
        raise ValueError('BACKUP_VAULT_NOT_REGISTERED')

    def register_backup(self, args):
        if args.get('confirmation') != args.get('path'):
            raise ValueError('CONFIRM_BACKUP_PATH')
        row = self.backups.register(args['path'])
        self.backup_health = 'REGISTERED'
        return row

    def create_backup(self, args):
        contexts = []
        for account in self.accounts().values():
            if account['external_user_id']:
                contexts.append(self.context(account))
        result = self.backups.create(self.backup_row(args), self.gate(), contexts)
        self.backup_health = 'VERIFIED'
        return {'snapshot_id': result['snapshot_id'], 'created_at': result['created_at'], 'total_bytes': result['total_bytes']}

    def list_backups(self):
        result = []
        for row in self.backups.registry():
            try:
                root = self.backups._vault(row)
                ids = self.backups.list_ids(row)
                if not ids:
                    result.append({'vault_id': row['vault_id'], 'path': row['path'], 'snapshot_id': None,
                                   'status': 'REGISTERED', 'verification_label': '已注册'})
                for snapshot_id in ids:
                    info = self.backups.describe(row, snapshot_id)
                    result.append({'vault_id': row['vault_id'], 'path': row['path'], 'snapshot_id': snapshot_id,
                                   'created_at': info['created_at'], 'total_bytes': info['total_bytes'],
                                   'status': info['verification_status'], 'last_verified_at': info['last_verified_at'],
                                   'verification_label': {'NOT_VERIFIED':'尚未校验', 'VERIFIED_PREVIOUSLY':'上次通过',
                                                          'VERIFICATION_FAILED':'上次失败'}[info['verification_status']]})
            except Exception as exc:
                result.append({'vault_id': row['vault_id'], 'path': row['path'], 'snapshot_id': None,
                               'status': type(exc).__name__, 'verification_label': '无法读取'})
        return result

    def verify_backup(self, args):
        result = self.backups.verify(self.backup_row(args), args['snapshot_id'])
        self.backup_health = 'VERIFIED'
        return {'snapshot_id': result['snapshot_id'], 'verified': True}

    def restore_backup(self, args):
        if args.get('confirmation') != args.get('target'):
            raise ValueError('CONFIRM_RESTORE_TARGET')
        return self.backups.restore(self.backup_row(args), args['snapshot_id'], args['target'])

    def manual_sync_tested(self, account, runtime):
        scheduled_requests = {entry['request_id'] for state in self.store.admin_events('SCHEDULE_STATE')
                              for entry in state['entries']}
        # 每次计划创建前也复核，避免启用后新增账号或改变映射绕过手动验收。
        for job in self.store.jobs().values():
            if (job['account_id'] == account['account_id'] and job['state'] == 'DONE' and
                    job['account_hash'] == digest(account) and job['context_hash'] == digest(self.context(account)) and
                    job.get('mode') == self.settings['mode'] and
                    job['actor_id'] == runtime.adapter.actor_id and job['actor_build'] == runtime.adapter.build and
                    job.get('adapter_version') == runtime.adapter.adapter_version and
                    job.get('command_id') not in scheduled_requests):
                return True
        return False

    def enable_schedule(self, args):
        if args.get('confirmation') != 'ENABLE':
            raise ValueError('EXPLICIT_SCHEDULE_CONFIRMATION_REQUIRED')
        accounts = []
        for account in self.accounts().values():
            if account['active_status'] == 'ACTIVE':
                accounts.append(account)
                runtime = self.runtime(account)
                runtime._permit(account, self.context(account))
                if not self.manual_sync_tested(account, runtime):
                    raise ValueError('MANUAL_SYNC_TEST_REQUIRED')
        if not accounts or len(accounts) > 150:
            raise ValueError('SCHEDULE_REQUIRES_1_TO_150_ACTIVE_ACCOUNTS')
        if not self.backups.registry():
            raise ValueError('OTHER_DISK_BACKUP_REQUIRED')
        for row in self.backups.registry():
            self.backups._vault(row)
        balance = self.runtime(accounts[0]).budget()
        if balance['blocked'] or balance['finalized'] + balance['reserved'] + money(self.settings['per_run_usd']) > money(self.settings['pilot_usd']):
            raise ValueError('SCHEDULE_BUDGET_BLOCKED')
        settings = copy.deepcopy(self.settings)
        settings['schedule_enabled'] = True
        result = self.task_backend.register(settings)
        # Registering a task alone cannot start a paid run: persisted flag is last.
        self.store.save_admin('SETTINGS_STATE', settings, ADMIN)
        self.settings = settings
        if self.schedule.current()['claimed_day'] is None:
            from datetime import timedelta
            from zoneinfo import ZoneInfo
            yesterday = parse_utc(utc_now()).astimezone(ZoneInfo(settings['timezone'])).date() - timedelta(days=1)
            self.schedule.save({'schedule_key': None, 'claimed_day': yesterday.isoformat(), 'missed_days': 0, 'entries': []})
        return result

    def disable_schedule(self):
        # Disable the local gate even if Windows task removal fails. No new paid work can start.
        settings = copy.deepcopy(self.settings)
        settings['schedule_enabled'] = False
        self.store.save_admin('SETTINGS_STATE', settings, ADMIN)
        self.settings = settings
        return self.task_backend.disable()

    def retry_schedule(self):
        state = self.schedule.current()
        for entry in state['entries']:
            if entry['status'] == 'BLOCKED':
                entry['status'] = 'PENDING'
        self.schedule.save(state)
        self.failed_ticks.discard('SCHEDULE_BLOCKED')
        return {'retry_requested': True}

    def import_legacy(self, args):
        from collector.migration import import_legacy
        account = self.accounts()[args['account_id']]
        execute = args.get('execute', False)
        if execute and args.get('confirmation') != args['account_id']:
            raise ValueError('CONFIRM_LEGACY_ACCOUNT')
        return import_legacy(self.store, args['source'], account, self.gate(), self.context(account), execute)

    def run_acceptance(self):
        from collector.acceptance import offline_trial, classification_sample
        from collector.validation import canonical
        report = offline_trial(5)
        report['classification_sample'] = classification_sample()
        path = self.store._path('exports/acceptance-' + uuid.uuid4().hex + '.json')
        atomic_bytes(path, canonical(report))
        DeletionManager(self.store).track_artifact([], path.relative_to(self.store.root).as_posix())
        return {'path': str(path), 'passed': report['passed'], 'mode': 'MOCK'}

    def approved_stage(self):
        records = self.store.admin_events('SCALE_APPROVAL')
        return records[-1]['stage'] if records else 5

    def approve_stage(self, args):
        stages = [5,20,50,150]
        current = self.approved_stage()
        if current == 150 or args.get('stage') != stages[stages.index(current)+1] or args.get('confirmation') != 'APPROVE':
            raise ValueError('NEXT_STAGE_EXPLICIT_APPROVAL_REQUIRED')
        if not args.get('reviewer') or not args.get('evidence_uri') or args['evidence_uri'].startswith('mock:'):
            raise ValueError('REAL_STAGE_REVIEW_EVIDENCE_REQUIRED')
        successful = set()
        for job in self.store.jobs().values():
            if job['actor_id'] != 'offline-synthetic' and job['state'] == 'DONE' and job['finalized_usd'] is not None:
                successful.add(job['account_id'])
        if len(successful) < int(current * .8):
            raise ValueError('PREVIOUS_REAL_STAGE_ACCEPTANCE_REQUIRED')
        for account in self.accounts().values():
            if account['active_status'] == 'ACTIVE':
                self.runtime(account)._permit(account, self.context(account))
        payload = {'stage':args['stage'],'reviewer':args['reviewer'],'evidence_uri':args['evidence_uri'],'approved_at':utc_now()}
        self.store.save_admin('SCALE_APPROVAL',payload,ADMIN)
        return {'approved_stage':args['stage'],'budget_unchanged':True}
