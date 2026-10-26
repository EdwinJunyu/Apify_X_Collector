"""Manifest-reachable JSONL segments are the only committed source of truth.

Every write is single-threaded under an OS process lock. Failures poison the
instance: close and reopen to resolve whether the pointer commit succeeded.
"""
import copy
import hashlib
import os
import re
import uuid
from pathlib import Path

from .quota import Quota
from .locking import WriterLock
from .validation import ValidationError, canonical, check, digest, strict_loads, utc_now, parse_utc, validate, load_schema


class CorruptStore(RuntimeError):
    pass


DIRECTORIES = ('accounts', 'policies', 'posts', 'raw', 'media/blobs', 'runs',
               'sync', 'ledger', 'reviews', 'deletions', 'quarantine/orphans',
               'journal/segments', 'journal/manifests', 'indexes', 'exports',
               'snapshots', 'logs')
CONTENT_FIELDS = ('platform', 'post_id', 'account_id', 'author_external_id',
                  'published_at', 'post_type', 'quoted_post_id', 'in_reply_to_id',
                  'reposted_post_id', 'language', 'text', 'media_refs', 'hashtags',
                  'mentions', 'content_rating')
MAX_SEGMENT = 16 * 1024 * 1024
OBSERVATION_FIELDS = ('platform', 'post_id', 'account_id', 'author_external_id',
                      'author_handle_at_observation', 'source_url', 'version_number',
                      'observed_at', 'indexed_at', 'metrics')


def content_hash(post):
    content = {}
    for field in CONTENT_FIELDS:
        content[field] = post[field]
    return digest(content)


def sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def replace_file(source, target):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH, same volume.
        if not move(str(source), str(target), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)
    sync_directory(target.parent)


def atomic_bytes(path, data):
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    with temporary.open('xb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    replace_file(temporary, path)


class StorageEngine:
    def __init__(self, root, fault=None, restore_token=None):
        from .security import checked_path, secure_path
        try:
            self.root = checked_path(root)
        except PermissionError:
            raise ValueError('SYMLINK_ROOT') from None
        secure_path(self.root, directory=True)
        restoring = self.root / '.restore-in-progress.json'
        if restoring.exists():
            state = strict_loads(restoring.read_bytes())
            if restore_token is None or state.get('token') != restore_token:
                raise ValueError('RESTORE_INCOMPLETE_ROOT_BLOCKED')
        # Private data root only; reject links before creating children.
        for part in (self.root, *self.root.parents):
            if part.is_symlink():
                raise ValueError('SYMLINK_ROOT')
        for directory in DIRECTORIES:
            path = self.root / directory
            for part in (path, *path.parents):
                if part == self.root.parent:
                    break
                if part.is_symlink():
                    raise ValueError('SYMLINK_DIRECTORY')
            path.mkdir(parents=True, exist_ok=True)
        self.lock = WriterLock(self.root)
        self.lock.acquire()
        self.quota = Quota(self.root)
        self.essential = False
        self._erased = set()
        self._erased_media = set()
        self.fault = fault
        self.poisoned = False
        self._posts = {}
        self._versions = {}
        self._tombstones = {}
        self._checkpoints = {}
        self._quarantine = []
        self._events = []
        self.head = None
        try:
            floor = self._path('deletions/format-floor.json')
            if floor.exists() and strict_loads(floor.read_bytes()).get('min_reader_level', 0) > 10:
                raise ValueError('STORE_REQUIRES_NEWER_READER')
            atomic_bytes(floor, canonical({'min_reader_level': 10, 'schema_version': '1.0'}))
            self.recover()
        except BaseException:
            self.lock.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.lock.close()

    def _ready(self):
        self.lock.require()
        if self.poisoned:
            raise CorruptStore('REOPEN_REQUIRED')

    def _path(self, relative):
        path = self.root / relative
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()) or resolved == self.root.resolve():
            raise CorruptStore('PATH_OUTSIDE_ROOT')
        if path.is_symlink():
            raise CorruptStore('SYMLINK_FILE')
        return path

    def _load(self, relative, limit=MAX_SEGMENT):
        path = self._path(relative)
        if path.stat().st_size > limit:
            raise CorruptStore('FILE_LIMIT')
        return path.read_bytes()

    def _hit(self, stage):
        if self.fault:
            self.fault(stage)

    def _apply(self, event):
        payload = event['payload']
        if event['event_type'] == 'MEDIA_STATE' and payload['media_id'] in self._erased_media:
            self.erasure_replay_filtered = True
            return
        if event['event_type'] == 'REVIEW_STATE' and payload['post_id'] in self._erased:
            self.erasure_replay_filtered = True
            return
        if event['event_type'] in ('POST_UPSERT', 'POST_OBSERVATION', 'MEDIA_STATE') and payload['post_id'] in self._erased:
            self.erasure_replay_filtered = True
            return
        kind = event['event_type']
        # 先验证原事件完整性，再应用持久化字段屏障；中断后重开也不暴露旧正文。
        text_erased = event['event_id'] in self._redacted_text
        media_erased = event['event_id'] in self._redacted_media_refs
        if (text_erased or media_erased) and kind in ('POST_UPSERT', 'REVIEW_STATE'):
            event = copy.deepcopy(event)
            payload = event['payload']
            before = digest(payload)
            if kind == 'POST_UPSERT':
                fields = set(payload.get('redacted_fields', []))
                if text_erased:
                    payload['text'] = None
                    fields.add('TEXT')
                if media_erased:
                    payload['media_refs'] = []
                    fields.add('MEDIA')
                payload['raw_ref'] = None
                payload['redacted_fields'] = sorted(fields)
                payload['content_hash'] = content_hash(payload)
            elif text_erased:
                payload['notes'] = None
            event['payload_hash'] = digest(payload)
            event['redaction_state'] = 'SANITIZED'
            if before != event['payload_hash']:
                self.erasure_replay_filtered = True
        if kind == 'POST_UPSERT':
            key = payload['platform'] + ':' + payload['post_id']
            old = self._posts.get(key)
            expected = 1 if old is None else old['version_number'] + 1
            if (key in self._tombstones or payload['version_number'] != expected or
                    content_hash(payload) != payload['content_hash']):
                raise CorruptStore('INVALID_POST_HISTORY')
            if old and (old['author_external_id'] != payload['author_external_id'] or
                        old['account_id'] != payload['account_id'] or
                        old['published_at'] != payload['published_at'] or
                        old['first_seen_at'] != payload['first_seen_at'] or
                        (old['content_hash'] == payload['content_hash'] and not
                         (old.get('redacted_fields') or payload.get('redacted_fields')))):
                raise CorruptStore('POST_IDENTITY_OR_VERSION_CONFLICT')
            self._posts[key] = copy.deepcopy(payload)
            self._versions.setdefault(key, []).append(copy.deepcopy(payload))
        elif kind == 'POST_OBSERVATION':
            key = payload['platform'] + ':' + payload['post_id']
            current = self._posts.get(key)
            if (current is None or key in self._tombstones or any(
                    payload[name] != current[name] for name in ('account_id', 'author_external_id', 'version_number'))
                    or event['source_id'] != 'x:' + current['author_external_id']
                    or parse_utc(payload['observed_at']) < parse_utc(current['observed_at'])):
                raise CorruptStore('INVALID_POST_OBSERVATION')
            # 版本自身和审核 hash 不变；当前视图及该版本的最近观测一起更新。
            current.update(copy.deepcopy(payload))
            self._versions[key][-1].update(copy.deepcopy(payload))
        elif kind == 'TOMBSTONE':
            key = payload['platform'] + ':' + payload['subject_id']
            if key in self._tombstones:
                raise CorruptStore('DUPLICATE_TOMBSTONE')
            self._tombstones[key] = copy.deepcopy(payload)
            self._posts.pop(key, None)
        elif kind == 'CHECKPOINT':
            self._checkpoints[payload['account_id']] = copy.deepcopy(payload)
        elif kind == 'QUARANTINE':
            self._quarantine.append(copy.deepcopy(payload))
        self._events.append(copy.deepcopy(event))

    def recover(self):
        self.lock.require()
        self.erasure_replay_filtered = False
        self._redacted_text, self._redacted_media_refs = set(), set()
        scope_file = self._path('deletions/scope-erasure-barrier.json')
        if scope_file.exists():
            scopes = strict_loads(self._load('deletions/scope-erasure-barrier.json'))
            if not isinstance(scopes, dict) or set(scopes) != {'text', 'media'}:
                raise CorruptStore('INVALID_SCOPE_BARRIER')
            for field, target in (('text', self._redacted_text), ('media', self._redacted_media_refs)):
                if not isinstance(scopes[field], list):
                    raise CorruptStore('INVALID_SCOPE_BARRIER')
                for event_id in scopes[field]:
                    if not isinstance(event_id, str) or not re.fullmatch(r'[a-f0-9]{32}', event_id):
                        raise CorruptStore('INVALID_SCOPE_BARRIER')
                    target.add(event_id)
        barrier = self._path('deletions/erasure-barrier.json')
        self._erased = set()
        if barrier.exists():
            erased = strict_loads(self._load('deletions/erasure-barrier.json'))
            if not isinstance(erased, list):
                raise CorruptStore('INVALID_ERASURE_BARRIER')
            for post_id in erased:
                if not isinstance(post_id, str) or not re.fullmatch(r'[0-9]{1,32}', post_id):
                    raise CorruptStore('INVALID_ERASURE_BARRIER')
                self._erased.add(post_id)
        media_barrier = self._path('deletions/media-erasure-barrier.json')
        self._erased_media = set()
        if media_barrier.exists():
            entries = strict_loads(self._load('deletions/media-erasure-barrier.json'))
            if not isinstance(entries, list):
                raise CorruptStore('INVALID_MEDIA_BARRIER')
            for media_id in entries:
                if not isinstance(media_id, str) or not re.fullmatch(r'[a-f0-9]{32}', media_id):
                    raise CorruptStore('INVALID_MEDIA_BARRIER')
                self._erased_media.add(media_id)
        self._posts.clear()
        self._versions.clear()
        self._tombstones.clear()
        self._checkpoints.clear()
        self._quarantine.clear()
        self._events.clear()
        self.head = None
        chain = []
        referenced = set()
        seen = set()
        pointer = self._path('journal/CURRENT.json')
        try:
            if pointer.exists():
                self.head = strict_loads(self._load('journal/CURRENT.json', 4096))
                check('pointer', self.head)
            current = self.head
            while current:
                cid = current['commit_id']
                if cid in seen:
                    raise CorruptStore('MANIFEST_CYCLE')
                seen.add(cid)
                manifest_name = 'journal/manifests/' + cid + '.json'
                data = self._load(manifest_name, 65536)
                if hashlib.sha256(data).hexdigest() != current['manifest_hash']:
                    raise CorruptStore('MANIFEST_HASH')
                manifest = strict_loads(data)
                check('manifest', manifest)
                if manifest['commit_id'] != cid:
                    raise CorruptStore('COMMIT_ID_MISMATCH')
                segment_name = 'journal/segments/' + cid + '.jsonl'
                raw = self._load(segment_name)
                if (len(raw) != manifest['segment_bytes'] or
                        hashlib.sha256(raw).hexdigest() != manifest['segment_hash'] or
                        not raw.endswith(b'\n')):
                    raise CorruptStore('SEGMENT_INTEGRITY')
                events = []
                for line in raw.splitlines():
                    event = strict_loads(line)
                    self._validate_event(event)
                    if event['commit_id'] != cid or event['event_id'] in seen:
                        raise CorruptStore('EVENT_ID_MISMATCH')
                    seen.add(event['event_id'])
                    events.append(event)
                if len(events) != manifest['event_count']:
                    raise CorruptStore('EVENT_COUNT')
                chain.append(events)
                referenced.update((manifest_name, segment_name))
                current = manifest['previous']
            for events in reversed(chain):
                for event in events:
                    self._apply(event)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.poisoned = True
            raise CorruptStore('RECOVERY_INTEGRITY_FAILURE') from None
        except CorruptStore:
            self.poisoned = True
            raise
        # Only implementation-owned names; never guess that an orphan committed.
        for directory, pattern in (('journal/segments', r'[a-f0-9]{32}\.jsonl'),
                                   ('journal/manifests', r'[a-f0-9]{32}\.json'),
                                   ('journal', r'[a-f0-9]{32}\.pending')):
            for path in (self.root / directory).iterdir():
                relative = path.relative_to(self.root).as_posix()
                if re.fullmatch(pattern, path.name) and relative not in referenced:
                    source = self._path(relative)
                    target = self._path('quarantine/orphans/' + uuid.uuid4().hex + '-' + path.name)
                    replace_file(source, target)
        # Pending files never participate in replay. Index is disposable.
        self._write_index()
        self.poisoned = False
        return self.status()

    def _validate_event(self, event):
        check('event', event)
        if event['payload_hash'] != digest(event['payload']):
            raise ValidationError('PAYLOAD_HASH')
        mapping = {'POST_UPSERT': 'post', 'QUARANTINE': 'quarantine',
                   'TOMBSTONE': 'tombstone', 'CHECKPOINT': 'checkpoint', 'JOB_STATE': 'job', 'MEDIA_STATE': 'media_task', 'ACCOUNT_STATE': 'account', 'POLICY_STATE': 'policy', 'REVIEW_STATE': 'review_action', 'SETTINGS_STATE': 'desktop_settings', 'OPERATION_LOG': 'operation_log', 'SCHEDULE_STATE': 'schedule_state', 'LEGACY_IMPORT': 'legacy_import', 'SCALE_APPROVAL': 'scale_approval'}
        if event['event_type'] == 'POST_OBSERVATION':
            payload = event['payload']
            validate(payload, load_schema('event')['$defs']['post_observation'])
            if payload['source_url'] != 'https://x.com/' + payload['author_handle_at_observation'] + '/status/' + payload['post_id']:
                raise ValidationError('SOURCE_URL_MISMATCH')
        else:
            check(mapping[event['event_type']], event['payload'])
        if event['event_type'] == 'POST_UPSERT':
            validate_post(event['payload'])

    def _event(self, kind, payload, context):
        return {'schema_version': '1.0', 'event_id': uuid.uuid4().hex,
                'event_type': kind, 'recorded_at': utc_now(), 'commit_id': '0' * 32,
                'source_id': context['source_id'], 'actor_run_id': context.get('actor_run_id'),
                'rights_policy_id': context['policy_id'],
                'rights_policy_version': context['policy_version'], 'payload': payload,
                'payload_hash': digest(payload), 'retention_class': 'policy-bound',
                'redaction_state': 'SANITIZED' if kind == 'QUARANTINE' else 'NONE'}

    def _commit(self, events):
        self._ready()
        if not events:
            return None
        cid = uuid.uuid4().hex
        data = bytearray()
        for event in events:
            event['commit_id'] = cid
            self._validate_event(event)
            data.extend(canonical(event) + b'\n')
        if len(data) > MAX_SEGMENT:
            raise ValueError('SEGMENT_LIMIT')
        self.quota.require(len(data) * 2 + 65536, essential=self.essential)
        manifest = {'schema_version': '1.0', 'commit_id': cid, 'previous': self.head,
                    'segment_hash': hashlib.sha256(data).hexdigest(),
                    'segment_bytes': len(data), 'event_count': len(events)}
        check('manifest', manifest)
        mdata = canonical(manifest)
        head = {'schema_version': '1.0', 'commit_id': cid,
                'manifest_hash': hashlib.sha256(mdata).hexdigest()}
        try:
            pending = self._path('journal/' + cid + '.pending')
            atomic_bytes(pending, data)
            self._hit('AFTER_PENDING')
            replace_file(pending, self._path('journal/segments/' + cid + '.jsonl'))
            self._hit('AFTER_SEGMENT')
            atomic_bytes(self._path('journal/manifests/' + cid + '.json'), mdata)
            self._hit('AFTER_MANIFEST')
            atomic_bytes(self._path('journal/CURRENT.json'), canonical(head))
            self._hit('AFTER_POINTER')
            self.head = head
            for event in events:
                self._apply(event)
            self._write_index()
            return cid
        except BaseException:
            self.poisoned = True
            raise

    def _write_index(self):
        # Metadata only: full content stays solely in committed segments.
        posts = {}
        for key, post in self._posts.items():
            posts[key] = {'version_number': post['version_number'], 'content_hash': post['content_hash']}
        atomic_bytes(self._path('indexes/current.json'), canonical({'head': self.head, 'posts': posts}))

    def ingest(self, records, account, gate, context, checkpoint=None, job=None):
        self._ready()
        check('account', account)
        check('context', context)
        if (account['verification_status'] != 'VERIFIED' or
                account['external_user_id'] is None or not account['verified_source_urls']):
            raise PermissionError('VERIFIED_ACCOUNT_REQUIRED')
        policy = gate.require(context['source_id'], 'retain', context['purpose'],
                              context['policy_id'], context['policy_version'])
        if 'METADATA' not in policy['content_scope']:
            raise PermissionError('METADATA_RETENTION_DENIED')
        if context['source_id'] != 'x:' + account['external_user_id']:
            raise PermissionError('SOURCE_ACCOUNT_MISMATCH')
        if context['policy_id'] != account['source_policy_id']:
            raise PermissionError('SOURCE_POLICY_MISMATCH')
        if not isinstance(records, list) or len(records) > 1000:
            raise ValueError('BATCH_LIMIT')
        events = []
        working = copy.deepcopy(self._posts)
        counts = {'inserted': 0, 'revised': 0, 'duplicate': 0, 'quarantined': 0,
                  'filtered_deleted': 0}
        for row in records:
            try:
                if isinstance(row, dict) and row.get('text') is not None and 'TEXT' not in policy['content_scope']:
                    raise ValidationError('TEXT_RETENTION_DENIED')
                if isinstance(row, dict) and row.get('media_refs') and 'MEDIA' not in policy['content_scope']:
                    raise ValidationError('MEDIA_RETENTION_DENIED')
                post = prepare_post(row, account)
                key = post['platform'] + ':' + post['post_id']
                if key in self._tombstones or post['post_id'] in self._erased:
                    # 完成字段和账号校验后再过滤，不让损坏或伪造身份绕过隔离。
                    counts['filtered_deleted'] += 1
                    continue
                previous = working.get(key)
                if previous:
                    if (post['author_external_id'] != previous['author_external_id'] or
                            post['account_id'] != previous['account_id'] or
                            post['published_at'] != previous['published_at']):
                        raise ValidationError('POST_IDENTITY_CONFLICT')
                    if post['content_hash'] == previous['content_hash']:
                        counts['duplicate'] += 1
                        if parse_utc(post['observed_at']) >= parse_utc(previous['observed_at']):
                            observed = {name: copy.deepcopy(post[name]) for name in OBSERVATION_FIELDS}
                            observed['version_number'] = previous['version_number']
                            # 本次未返回的计数保留最近已知值，0 则是有效的新观测。
                            observed['metrics'] = dict(previous['metrics'], **post['metrics'])
                            compared = ('observed_at', 'metrics', 'author_handle_at_observation', 'source_url')
                            if any(observed[name] != previous[name] for name in compared):
                                events.append(self._event('POST_OBSERVATION', observed, context))
                                # 同批前一个 POST_UPSERT 可能仍引用 previous，不能改写其已计算的 hash。
                                latest = copy.deepcopy(previous)
                                latest.update(copy.deepcopy(observed))
                                working[key] = latest
                        continue
                    post['version_number'] = previous['version_number'] + 1
                    post['first_seen_at'] = previous['first_seen_at']
                    counts['revised'] += 1
                else:
                    counts['inserted'] += 1
                working[key] = post
                events.append(self._event('POST_UPSERT', post, context))
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
                # Never copy untrusted text or exception messages into diagnostics.
                reason = 'INVALID_POST_OR_TOMBSTONED'
                if isinstance(exc, ValidationError):
                    reason = str(exc)
                diagnostic = {'reason': reason, 'item_index': sum(counts.values()),
                              'quarantine_id': uuid.uuid4().hex}
                events.append(self._event('QUARANTINE', diagnostic, context))
                counts['quarantined'] += 1
        if checkpoint is not None:
            check('checkpoint', checkpoint)
            if checkpoint['account_id'] != account['account_id']:
                raise ValueError('CHECKPOINT_ACCOUNT_MISMATCH')
            if counts['quarantined']:
                raise ValueError('CHECKPOINT_REQUIRES_CLEAN_BATCH')
            events.append(self._event('CHECKPOINT', checkpoint, context))
        if job is not None:
            check('job', job)
            events.append(self._event('JOB_STATE', job, context))
        counts['commit_id'] = self._commit(events)
        return counts

    def tombstone(self, post_id, context, reason='WITHDRAWN'):
        self._ready()
        check('context', context)
        if not isinstance(post_id, str) or not re.fullmatch(r'[0-9]{1,32}', post_id):
            raise ValueError('INVALID_POST_ID')
        key = 'X:' + post_id
        if key in self._tombstones:
            return None
        post = self._posts.get(key)
        if post and context['source_id'] != 'x:' + post['author_external_id']:
            raise PermissionError('SOURCE_ACCOUNT_MISMATCH')
        payload = {'platform': 'X', 'subject_type': 'POST', 'subject_id': post_id,
                   'request_reason': reason, 'requested_at': utc_now(),
                   'cleanup_scope': ['current_index'], 'cleaned_at': None,
                   'verification_status': 'PENDING_PHYSICAL_CLEANUP'}
        return self._commit([self._event('TOMBSTONE', payload, context)])

    def current_posts(self):
        self._ready()
        result = {}
        for key, post in self._posts.items():
            if post['post_id'] not in self._erased:
                result[key] = copy.deepcopy(post)
        return result

    def history(self, post_id):
        self._ready()
        if post_id in self._erased:
            return []
        key = 'X:' + post_id
        versions = copy.deepcopy(self._versions.get(key, []))
        for post in versions:
            post['is_current'] = key in self._posts and post['version_number'] == self._posts[key]['version_number']
        return versions

    def quarantine_records(self):
        self._ready()
        return copy.deepcopy(self._quarantine)

    def checkpoint(self, account_id):
        self._ready()
        return copy.deepcopy(self._checkpoints.get(account_id))

    def save_job(self, job, context, mirror_accounting=True):
        self._ready()
        check('context', context)
        check('job', job)
        # 不允许借由本地补记入口绕过收费 intent 的外盘预留。
        if not mirror_accounting and not (
                job['state'] == 'FAILED' and job['error_code'] == 'CONFIRMED_NOT_CREATED'
                and job['run_id'] is None and job['dataset_id'] is None
                and job['finalized_usd'] == '0.00'
                and job['run_evidence_uri'] == 'local://create-not-sent/accounting-mirror-failed'):
            raise ValueError('ACCOUNTING_MIRROR_REQUIRED')
        commit_id = self._commit([self._event('JOB_STATE', job, context)])
        # A RUN_INTENT is mirrored before ActorRunner can send its paid POST.
        # If the registered vault is unavailable, creation fails closed.
        # 唯一跳过镜像的调用是 POST 尚未发生时追加本地失败凭据。
        if mirror_accounting and self._path('deletions/backup-registry.json').exists():
            from .backup import BackupManager
            BackupManager(self).mirror_accounting()
        return commit_id

    def jobs(self):
        self._ready()
        jobs = {}
        for event in self._events:
            if event['event_type'] == 'JOB_STATE':
                jobs[event['payload']['job_id']] = copy.deepcopy(event['payload'])
        return jobs

    def save_media(self, task, context):
        self._ready()
        check('context', context)
        check('media_task', task)
        return self._commit([self._event('MEDIA_STATE', task, context)])

    def media_tasks(self):
        self._ready()
        result = {}
        for event in self._events:
            if event['event_type'] == 'MEDIA_STATE' and event['payload']['post_id'] not in self._erased and event['payload']['media_id'] not in self._erased_media:
                result[event['payload']['media_id']] = copy.deepcopy(event['payload'])
        return result

    def admin_events(self, event_type):
        self._ready()
        result = []
        for event in self._events:
            if event['event_type'] == event_type:
                result.append(copy.deepcopy(event['payload']))
        return result

    def save_admin(self, kind, payload, context):
        if kind not in ('ACCOUNT_STATE', 'POLICY_STATE', 'REVIEW_STATE', 'SETTINGS_STATE', 'OPERATION_LOG', 'SCHEDULE_STATE', 'LEGACY_IMPORT', 'SCALE_APPROVAL'):
            raise ValueError('UNSUPPORTED_ADMIN_EVENT')
        self._ready()
        check('context', context)
        return self._commit([self._event(kind, payload, context)])

    def status(self):
        return {'schema_version': '1.0', 'posts': len(self._posts),
                'events': len(self._events), 'tombstones': len(self._tombstones),
                'quarantined': len(self._quarantine), 'head': copy.deepcopy(self.head)}


def validate_post(post):
    check('post', post)
    expected = 'https://x.com/' + post['author_handle_at_observation'] + '/status/' + post['post_id']
    if post['source_url'] != expected:
        raise ValidationError('SOURCE_URL_MISMATCH')
    pairs = (('QUOTE', 'quoted_post_id'), ('REPLY', 'in_reply_to_id'), ('REPOST', 'reposted_post_id'))
    for kind, field in pairs:
        if post['post_type'] == kind and post[field] is None:
            raise ValidationError('MISSING_RELATION_ID')


def prepare_post(row, account):
    if not isinstance(row, dict):
        raise ValidationError('POST_OBJECT_REQUIRED')
    post = copy.deepcopy(row)
    post.pop('redacted_fields', None)  # 外部输入不能伪造本地清理凭据。
    now = utc_now()
    for field in ('first_seen_at', 'observed_at', 'indexed_at'):
        post.setdefault(field, now)
    post.update({'version_number': 1, 'content_hash': '0' * 64, 'is_current': True,
                 'review_status': 'STAGING', 'rights_status': 'PERMITTED', 'deletion_state': 'ACTIVE'})
    validate_post(post)
    handles = [account['current_handle']]
    handles.extend(account['handle_history'])
    valid_handles = []
    for handle in handles:
        valid_handles.append(handle.lower())
    if (account['verification_status'] != 'VERIFIED' or
            post['account_id'] != account['account_id'] or
            post['author_external_id'] != account['external_user_id'] or
            post['author_handle_at_observation'].lower() not in valid_handles):
        raise ValidationError('AUTHOR_MISMATCH')
    post['content_hash'] = content_hash(post)
    return post
