"""Durable Actor lifecycle, conservative budget ledger, resumable account sync."""
import copy
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from .apify import CreateRejected, remote_id
from .validation import check, digest, parse_utc, utc_now


def money(value):
    if not isinstance(value, str):
        raise ValueError('DECIMAL_STRING_REQUIRED')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError('INVALID_MONEY') from None
    if not result.is_finite() or result < 0 or result.as_tuple().exponent < -6:
        raise ValueError('INVALID_MONEY')
    return result


class SyncCoordinator:
    def __init__(self, store, transport, adapter, gate, per_run='0.50', total='10.00', page_size=100):
        self.store, self.transport, self.adapter, self.gate = store, transport, adapter, gate
        self.per_run, self.total = money(per_run), money(total)
        if self.per_run > Decimal('0.50') or self.total > Decimal('10.00') or self.per_run <= 0 or self.per_run.quantize(Decimal('0.01')) != self.per_run:
            raise ValueError('FROZEN_BUDGET_LIMIT')
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError('PAGE_SIZE')
        self.page_size = page_size

    def _permit(self, account, context):
        check('account', account)
        check('context', context)
        if (account['verification_status'] != 'VERIFIED' or not account['verified_source_urls'] or
                context['source_id'] != 'x:' + str(account['external_user_id']) or
                context['policy_id'] != account['source_policy_id']):
            raise PermissionError('ACCOUNT_POLICY_BINDING')
        for operation in ('access', 'retain'):
            policy = self.gate.require(context['source_id'], operation, context['purpose'],
                                       context['policy_id'], context['policy_version'])
        if 'METADATA' not in policy['content_scope']:
            raise PermissionError('METADATA_RETENTION_DENIED')
        return policy

    def _save(self, job, context):
        job['updated_at'] = utc_now()
        self.store.save_job(job, context)

    def budget(self):
        finalized = Decimal('0')
        reserved = Decimal('0')
        blocked = False
        for job in self.store.jobs().values():
            if job['finalized_usd'] is None:
                reserved += money(job['reserved_usd'])
                blocked = True
            else:
                cost = money(job['finalized_usd'])
                finalized += cost
                # 新任务限额可以降低；历史任务是否超额按创建时持久化的上限判断。
                if cost > money(job['reserved_usd']):
                    blocked = True
        return {'finalized': finalized, 'reserved': reserved, 'blocked': blocked}

    def start(self, account, context, now=None, days=30, overlap_hours=48, live_approval=None, command_id=None):
        self._permit(account, context)
        end = parse_utc(now or utc_now())
        if type(days) is not int or not 1 <= days <= 30 or type(overlap_hours) is not int or overlap_hours < 1:
            raise ValueError('WINDOW_LIMIT')
        for old in self.store.jobs().values():
            if old['account_id'] == account['account_id'] and old['state'] not in ('DONE', 'FAILED'):
                raise RuntimeError('ACCOUNT_HAS_UNFINISHED_JOB')
        balance = self.budget()
        if balance['blocked'] or balance['finalized'] + balance['reserved'] + self.per_run > self.total:
            raise RuntimeError('BUDGET_BLOCKED')
        if not self.transport.is_mock:
            if (not isinstance(live_approval, dict) or
                    live_approval.get('actor_id') != self.adapter.actor_id or
                    live_approval.get('build') != self.adapter.build or self.adapter.build == 'unverified' or
                    live_approval.get('model') != 'PAY_PER_EVENT' or
                    not live_approval.get('evidence_uri') or
                    parse_utc(live_approval['valid_until']) <= parse_utc(utc_now()) or
                    money(live_approval['minimum_usd']) > self.per_run or
                    live_approval.get('cap_supported') is not True):
                raise PermissionError('LIVE_ACTOR_PRICE_REVIEW_REQUIRED')
        checkpoint = self.store.checkpoint(account['account_id'])
        start = end - timedelta(days=days)
        previous = None
        if checkpoint:
            start = max(start, parse_utc(checkpoint['last_success_at']) - timedelta(hours=overlap_hours))
            previous = checkpoint['committed_cursor']
        if start > end:
            raise ValueError('CLOCK_BEFORE_LAST_SYNC')
        start_text = start.isoformat().replace('+00:00', 'Z')
        end_text = end.isoformat().replace('+00:00', 'Z')
        inputs = self.adapter.inputs(account, start_text, end_text, previous)
        job = {'job_id': uuid.uuid4().hex, 'account_id': account['account_id'],
               'source_id': context['source_id'], 'context_hash': digest(context), 'account_hash': digest(account),
               'actor_id': self.adapter.actor_id, 'actor_build': self.adapter.build,
               'input_fingerprint': digest(inputs), 'window_start': start_text, 'window_end': end_text,
               'previous_cursor': previous, 'run_id': None, 'dataset_id': None, 'state': 'RUN_INTENT',
               'reserved_usd': format(self.per_run, '.2f'), 'observed_usd': None, 'finalized_usd': None,
               'offset': 0, 'total': None, 'candidate_cursor': previous, 'rejected': 0, 'accepted': 0,
               'oldest': None, 'newest': None, 'pages': 0, 'coverage_status': 'UNKNOWN',
               'coverage_reason': 'NO_EXTERNAL_COVERAGE_EVIDENCE', 'error_code': None, 'updated_at': utc_now(), 'run_evidence_uri': None, 'bill_evidence_uri': None}
        job['command_id'] = command_id
        # 历史任务固定执行环境，后续默认设置只影响新任务。
        job['mode'] = 'OFFLINE' if self.transport.is_mock else 'LIVE'
        job['adapter_version'] = self.adapter.adapter_version
        try:
            self._save(job, context)  # includes budget reservation, before any POST
        except Exception:
            # 此处尚未调用 create。若本地 intent 已提交而外盘同步失败，
            # 追加零费用终态；不能为了记录失败再次依赖离线外盘。
            # 本地提交本身损坏时不尝试越过 poisoned 保护，重启后仍需核对。
            if not self.store.poisoned and job['job_id'] in self.store.jobs():
                self._not_created(job, 'local://create-not-sent/accounting-mirror-failed')
                self.store.save_job(job, context, mirror_accounting=False)
            raise
        try:
            result = self.transport.create(self.adapter.actor_id, self.adapter.build, inputs, job['reserved_usd'])
            job['run_id'] = remote_id(result['id'])
            job['state'] = 'RUNNING'
        except CreateRejected as exc:
            self._not_created(job, 'apify://create-rejected/http/' + str(exc.status))
        except Exception:
            job['run_id'] = None
            job['state'] = 'REMOTE_OUTCOME_UNKNOWN'
            job['error_code'] = 'CREATE_RESULT_UNKNOWN'
        self._save(job, context)
        return job['job_id']

    def _not_created(self, job, evidence_uri):
        # 复用已有 FAILED 终态，保留原预留金额作为审计记录；零结算释放占用。
        job.update(state='FAILED', error_code='CONFIRMED_NOT_CREATED',
                   run_id=None, finalized_usd='0.00', run_evidence_uri=evidence_uri,
                   bill_evidence_uri=evidence_uri, updated_at=utc_now())

    def _job(self, job_id, account, context):
        policy = self._permit(account, context)
        job = self.store.jobs()[job_id]
        if (job['context_hash'] != digest(context) or job['account_hash'] != digest(account) or
                job['actor_id'] != self.adapter.actor_id or job['actor_build'] != self.adapter.build):
            raise PermissionError('JOB_CONTEXT_CHANGED')
        return job, policy

    def _accounting_job(self, job_id, account, context):
        # Recording an existing liability does not authorize access to source content.
        job = self.store.jobs()[job_id]
        if (job['account_id'] != account['account_id'] or
                job['source_id'] != 'x:' + str(account['external_user_id']) or
                job['source_id'] != context['source_id'] or
                job['actor_id'] != self.adapter.actor_id or job['actor_build'] != self.adapter.build):
            raise PermissionError('ACCOUNTING_IDENTITY_MISMATCH')
        return job

    def reconcile_run(self, job_id, account, context, run_id, evidence_uri):
        job = self._accounting_job(job_id, account, context)
        if job['state'] not in ('RUN_INTENT', 'REMOTE_OUTCOME_UNKNOWN') or not evidence_uri:
            raise ValueError('RECONCILIATION_REQUIRES_EVIDENCE')
        state = self.transport.run(remote_id(run_id))
        if state['id'] != run_id:
            raise ValueError('RUN_ID_MISMATCH')
        # Caller has manually matched Actor/input fingerprint/time in Console.
        job['run_id'] = run_id
        job['run_evidence_uri'] = evidence_uri
        job['state'] = 'RUNNING'
        job['error_code'] = None
        self._save(job, context)

    def confirm_not_created(self, job_id, account, context, evidence_uri, confirmation):
        job = self._accounting_job(job_id, account, context)
        # 超时不是未创建的证据。此入口仅接受人工核对后的明确确认，绝不自动重发。
        if (confirmation != job_id or not isinstance(evidence_uri, str) or
                not evidence_uri.strip()):
            raise ValueError('NOT_CREATED_REQUIRES_EVIDENCE_AND_JOB_CONFIRMATION')
        if (job['state'] not in ('RUN_INTENT', 'REMOTE_OUTCOME_UNKNOWN') or
                job['run_id'] is not None or job['dataset_id'] is not None or
                job['observed_usd'] is not None or job['finalized_usd'] is not None):
            raise ValueError('NOT_CREATED_REQUIRES_UNRESOLVED_CREATE')
        self._not_created(job, evidence_uri.strip())
        self._save(job, context)

    def finalize_bill(self, job_id, account, context, actual_usd, evidence_uri):
        job = self._accounting_job(job_id, account, context)
        if not isinstance(evidence_uri, str) or not evidence_uri.strip():
            raise ValueError('BILL_NOT_READY')
        if job['finalized_usd'] is not None:
            raise ValueError('BILL_ALREADY_FINALIZED')
        amount = format(money(actual_usd), 'f')
        if job['state'] in ('RUNNING', 'READING') and job['run_id'] is not None:
            # 许可撤销或账号配置变化不能冻结已发生的负债。仅核对既有 Run
            # 的终态，不读取 Dataset、不授权内容导入，也不推断零费用。
            if self.transport is None:
                raise ValueError('BILL_REQUIRES_REMOTE_TERMINAL_CHECK')
            result = self.transport.run(remote_id(job['run_id']))
            if result['id'] != job['run_id']:
                raise ValueError('RUN_ID_MISMATCH')
            if result['status'] not in ('SUCCEEDED', 'FAILED', 'ABORTED', 'TIMED-OUT'):
                raise ValueError('BILL_REQUIRES_REMOTE_TERMINAL_CHECK')
            job['state'] = 'FAILED'
            job['error_code'] = 'CONTENT_IMPORT_STOPPED_AFTER_BILLING'
        elif job['state'] not in ('DONE', 'FAILED', 'NEEDS_REVIEW'):
            raise ValueError('BILL_NOT_READY')
        job['bill_evidence_uri'] = evidence_uri.strip()
        job['finalized_usd'] = amount
        self._save(job, context)

    def advance(self, job_id, account, context):
        job, policy = self._job(job_id, account, context)
        if job['state'] in ('DONE', 'FAILED', 'NEEDS_REVIEW'):
            return job
        if job['state'] in ('RUN_INTENT', 'REMOTE_OUTCOME_UNKNOWN'):
            raise RuntimeError('REMOTE_OUTCOME_REQUIRES_RECONCILIATION')
        # Bounded step: one poll or one Dataset page. Caller controls polling interval.
        if job['state'] == 'RUNNING':
            result = self.transport.run(job['run_id'])
            if result['id'] != job['run_id']:
                raise ValueError('RUN_ID_MISMATCH')
            if result['status'] in ('FAILED', 'ABORTED', 'TIMED-OUT'):
                job['state'] = 'FAILED'
                job['error_code'] = 'REMOTE_RUN_FAILED'
            elif result['status'] == 'SUCCEEDED':
                job['dataset_id'] = remote_id(result['defaultDatasetId'])
                total = self.transport.dataset(job['dataset_id'])['itemCount']
                if type(total) is not int or not 0 <= total <= 100000:
                    raise ValueError('DATASET_COUNT_LIMIT')
                job['total'] = total
                job['state'] = 'READING'
            if result.get('usageTotalUsd') is not None:
                job['observed_usd'] = format(money(str(result['usageTotalUsd'])), 'f')
            if self.transport.is_mock and job['state'] == 'FAILED':
                job['finalized_usd'] = job['observed_usd']
                job['bill_evidence_uri'] = 'mock://synthetic-final-bill'
            self._save(job, context)
            return job
        if job['offset'] == job['total']:
            current_total = self.transport.dataset(job['dataset_id'])['itemCount']
            if current_total != job['total']:
                raise RuntimeError('DATASET_CHANGED')
            job['state'] = 'NEEDS_REVIEW' if job['rejected'] else 'DONE'
            job['coverage_status'] = 'PARTIAL' if job['accepted'] else 'UNKNOWN'
            job['coverage_reason'] = 'NORMALIZATION_REJECTED' if job['rejected'] else 'NO_EXTERNAL_COVERAGE_EVIDENCE'
            if self.transport.is_mock:
                job['finalized_usd'] = job['observed_usd']
                job['bill_evidence_uri'] = 'mock://synthetic-final-bill'
            if job['rejected']:
                self._save(job, context)
            else:
                checkpoint = {'account_id': account['account_id'], 'previous_cursor': job['previous_cursor'],
                    'requested_window': job['window_start'] + '/' + job['window_end'],
                    'overlap_start': job['window_start'], 'candidate_cursor': job['candidate_cursor'],
                    'committed_cursor': job['candidate_cursor'], 'coverage_status': job['coverage_status'],
                    'coverage_reason': job['coverage_reason'], 'last_success_at': job['window_end']}
                # Final job state and checkpoint share one durable commit.
                self.store.ingest([], account, self.gate, context, checkpoint, job)
            return job
        limit = min(self.page_size, job['total'] - job['offset'])
        rows = self.transport.items(job['dataset_id'], job['offset'], limit)
        if not isinstance(rows, list) or len(rows) != limit:
            raise RuntimeError('DATASET_PAGE_INCOMPLETE')
        posts = []
        for raw in rows:
            try:
                post = self.adapter.normalize(raw, account, 'TEXT' in policy['content_scope'],
                                              'MEDIA' in policy['content_scope'])
                if post is None:
                    continue
                if not parse_utc(job['window_start']) <= parse_utc(post['published_at']) <= parse_utc(job['window_end']):
                    continue
                if 'MEDIA' not in policy['content_scope']:
                    post['media_refs'] = []
                posts.append(post)
            except (ValueError, TypeError, KeyError):
                posts.append({})  # sanitized Quarantine, never retain invalid raw response
        result = self.store.ingest(posts, account, self.gate, context)
        job['rejected'] += result['quarantined']
        job['accepted'] += (len(posts) - result['quarantined'] - result['filtered_deleted']
                            - result['filtered_stale'])
        # Candidate is advisory until all pages finish. Do not include rejected records.
        for post in posts:
            if post and 'post_id' in post:
                # 删除屏障是已知的安全过滤，不纳入内容覆盖或候选游标。
                if ('X:' + post['post_id'] in self.store._tombstones or
                        post['post_id'] in self.store._erased):
                    continue
                current = self.store._posts.get('X:' + post['post_id'])
                if (current is not None and current['content_hash'] != post['content_hash']
                        and parse_utc(post['observed_at']) < parse_utc(current['observed_at'])):
                    # 已知过时正文不贡献本批覆盖或候选游标，也不阻塞下一次同步。
                    continue
                candidate = job['candidate_cursor']
                if candidate is None or int(post['post_id']) > int(candidate):
                    job['candidate_cursor'] = post['post_id']
                stamp = post['published_at']
                job['oldest'] = min(job['oldest'] or stamp, stamp, key=parse_utc)
                job['newest'] = max(job['newest'] or stamp, stamp, key=parse_utc)
        job['offset'] += len(rows)
        job['pages'] += 1
        self._save(job, context)
        return job

    def retry_review(self, job_id, account, context):
        job, _ = self._job(job_id, account, context)
        if job['state'] != 'NEEDS_REVIEW':
            raise ValueError('JOB_NOT_REVIEWABLE')
        job.update({'state': 'READING', 'offset': 0, 'pages': 0, 'accepted': 0,
                    'rejected': 0, 'candidate_cursor': job['previous_cursor'],
                    'oldest': None, 'newest': None, 'error_code': None})
        self._save(job, context)

    def coverage_report(self):
        report = []
        for job in self.store.jobs().values():
            report.append({'job_id': job['job_id'], 'account_id': job['account_id'],
                'window_start': job['window_start'], 'window_end': job['window_end'],
                'oldest': job['oldest'], 'newest': job['newest'], 'pages': job['pages'],
                'accepted_observations': job['accepted'], 'rejected': job['rejected'],
                'state': job['state'], 'coverage_status': job['coverage_status'],
                'coverage_reason': job['coverage_reason']})
        return report
