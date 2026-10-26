"""Durable anti-resurrection barrier + atomic chain compaction + tracked cleanup.

This is logical filesystem erasure, not SSD secure erase. M6 must preserve and
merge the barrier before restoring into a new data root.
"""
import copy
import hashlib
import re
import uuid
from .storage import atomic_bytes, replace_file, sync_directory, MAX_SEGMENT
from .validation import canonical, strict_loads, utc_now


class DeletionManager:
    def __init__(self, store):
        self.store = store

    def _catalog(self):
        path = self.store._path('deletions/artifacts.json')
        value = strict_loads(path.read_bytes()) if path.exists() else []
        if not isinstance(value, list):
            raise ValueError('INVALID_ARTIFACT_CATALOG')
        return value

    def track_artifact(self, post_ids, relative):
        # Only derived artifacts inside known local application directories.
        self.store._ready()
        path = self.store._path(relative)
        if relative.split('/')[0] not in ('raw', 'exports', 'snapshots') or not path.is_file():
            raise ValueError('INVALID_ARTIFACT')
        for post_id in post_ids:
            if not isinstance(post_id, str) or not re.fullmatch(r'[0-9]{1,32}', post_id):
                raise ValueError('INVALID_POST_ID')
        record = {'post_ids': post_ids, 'path': relative,
                  'hash': hashlib.sha256(path.read_bytes()).hexdigest()}
        catalog = self._catalog()
        progress = self._progress()
        if relative in progress['artifact_paths'] and not any(item['path'] == relative for item in catalog):
            # 旧文件及目录项已清理，但最终凭据可能写入中断。登记同路径新文件前，
            # 先耐久消费旧目标；否则下一次恢复会把新文件误认为旧删除任务。
            progress['artifact_paths'].remove(relative)
            try:
                self._save_progress(progress)
            except BaseException:
                self.store.poisoned = True
                raise
        catalog.append(record)
        atomic_bytes(self.store._path('deletions/artifacts.json'), canonical(catalog))

    def _progress(self):
        path = self.store._path('deletions/cleanup-progress.json')
        value = strict_loads(path.read_bytes()) if path.exists() else {
            'subjects': [], 'media_ids': [], 'post_contexts': {}, 'artifact_paths': [],
            'barriers': None, 'compacted': False, 'pending': []}
        if not isinstance(value, dict) or set(value) != {
                'subjects', 'media_ids', 'post_contexts', 'artifact_paths', 'barriers', 'compacted', 'pending'}:
            raise ValueError('INVALID_CLEANUP_PROGRESS')
        for key, pattern in (('subjects', r'[0-9]{1,32}'), ('media_ids', r'[a-f0-9]{32}')):
            if not isinstance(value[key], list) or any(
                    not isinstance(item, str) or not re.fullmatch(pattern, item) for item in value[key]):
                raise ValueError('INVALID_CLEANUP_PROGRESS')
        if (not isinstance(value['post_contexts'], dict) or type(value['compacted']) is not bool or
                not isinstance(value['pending'], list) or not isinstance(value['artifact_paths'], list) or
                any(not isinstance(path, str) for path in value['artifact_paths']) or
                (value['barriers'] is not None and (not isinstance(value['barriers'], str) or
                 not re.fullmatch(r'[a-f0-9]{64}', value['barriers'])))):
            raise ValueError('INVALID_CLEANUP_PROGRESS')
        from .validation import check
        for post_id, context in value['post_contexts'].items():
            if not re.fullmatch(r'[0-9]{1,32}', post_id):
                raise ValueError('INVALID_CLEANUP_PROGRESS')
            check('context', context)
        return value

    def _save_progress(self, value):
        path = self.store._path('deletions/cleanup-progress.json')
        raw = canonical(value)
        if not path.exists() or path.read_bytes() != raw:
            atomic_bytes(path, raw)

    def _queue_cleanup(self, subjects, media_ids=(), post_contexts=None):
        # 先持久化清理对象，再写屏障；恢复不能依赖已被压缩掉的 MEDIA_STATE。
        value = self._progress()
        value['subjects'] = sorted(set(value['subjects']) | set(subjects))
        value['media_ids'] = sorted(set(value['media_ids']) | set(media_ids))
        value['post_contexts'].update(post_contexts or {})
        # 只登记本次权限变更前的派生物；以后合法导出的元数据不应被反复删除。
        targets = {item['path'] for item in self._catalog() if set(item['post_ids']) & set(subjects)}
        value['artifact_paths'] = sorted(set(value['artifact_paths']) | targets)
        self._save_progress(value)

    def purge(self, post_id, context):
        store = self.store
        store._ready()
        from .validation import check
        check('context', context)
        if not isinstance(post_id, str) or not re.fullmatch(r'[0-9]{1,32}', post_id):
            raise ValueError('INVALID_POST_ID')
        post = store.current_posts().get('X:' + post_id)
        if post and context['source_id'] != 'x:' + post['author_external_id']:
            raise PermissionError('SOURCE_ACCOUNT_MISMATCH')
        media_ids = [task['media_id'] for task in store.media_tasks().values() if task['post_id'] == post_id]
        self._queue_cleanup([post_id], media_ids, {post_id: context})
        report = dict(self.resume_cleanup(), post_id=post_id, external_backup_cleanup=self._external_cleanup())
        atomic_bytes(store._path('deletions/cleanup-' + post_id + '.json'), canonical(report))
        return report

    def _clean_artifacts(self, erased, targets=()):
        remaining = []
        pending = []
        for item in self._catalog():
            if item['path'] in targets or any(post_id in erased for post_id in item['post_ids']):
                path = self.store._path(item['path'])
                if item['path'].split('/')[0] not in ('raw', 'exports', 'snapshots'):
                    raise ValueError('INVALID_ARTIFACT_CATALOG_PATH')
                if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != item['hash']:
                    pending.append('ARTIFACT_CHANGED')
                    remaining.append(item)
                else:
                    path.unlink(missing_ok=True)
            else:
                remaining.append(item)
        path = self.store._path('deletions/artifacts.json')
        raw = canonical(remaining)
        if not path.exists() or path.read_bytes() != raw:
            atomic_bytes(path, raw)
        return pending

    def _compact(self, events):
        # Build a separate complete chain first. Publish exactly one final pointer.
        store = self.store
        head = None
        batch = []
        size = 0
        batches = []
        for event in events:
            length = len(canonical(event)) + 1
            if size + length > MAX_SEGMENT and batch:
                batches.append(batch)
                batch, size = [], 0
            batch.append(event)
            size += length
        if batch:
            batches.append(batch)
        for batch in batches:
            cid = uuid.uuid4().hex
            data = bytearray()
            for event in batch:
                event['commit_id'] = cid
                store._validate_event(event)
                data.extend(canonical(event) + b'\n')
            store.quota.require(len(data) + 65536, essential=True)
            atomic_bytes(store._path('journal/segments/' + cid + '.jsonl'), data)
            manifest = {'schema_version': '1.0', 'commit_id': cid, 'previous': head,
                        'segment_hash': hashlib.sha256(data).hexdigest(),
                        'segment_bytes': len(data), 'event_count': len(batch)}
            raw = canonical(manifest)
            atomic_bytes(store._path('journal/manifests/' + cid + '.json'), raw)
            head = {'schema_version': '1.0', 'commit_id': cid,
                    'manifest_hash': hashlib.sha256(raw).hexdigest()}
        if head is None:
            raise RuntimeError('ERASURE_REQUIRES_TOMBSTONE')
        store._hit('BEFORE_ERASURE_POINTER')
        atomic_bytes(store._path('journal/CURRENT.json'), canonical(head))

    def purge_media(self, media_id, context):
        store = self.store
        store._ready()
        task = store.media_tasks()[media_id]
        if task['source_id'] != context['source_id']:
            raise PermissionError('CONTEXT_MISMATCH')
        self._queue_cleanup([task['post_id']], [media_id])
        result = self.resume_cleanup()
        return {'media_id': media_id, 'active_media_cleanup_completed': result['active_cleanup_completed'],
                'pending': result['pending'], 'external_backup_cleanup': self._external_cleanup()}

    def _external_cleanup(self):
        registry = self.store._path('deletions/backup-registry.json')
        if not registry.exists():
            return {'configured': False, 'completed': True, 'vaults': []}
        from .backup import BackupManager
        rows = BackupManager(self.store).propagate_erasure()
        completed = True
        for row in rows:
            completed = completed and row['completed']
        return {'configured': True, 'completed': completed, 'vaults': rows}

    def expire(self, contexts, now=None):
        from .validation import parse_utc, utc_now
        when = parse_utc(now or utc_now())
        results = []
        for task in list(self.store.media_tasks().values()):
            if parse_utc(task['expires_at']) <= when:
                context = contexts.get(task['context_hash'])
                if context is None:
                    for candidate in contexts.values():
                        if candidate['source_id'] == task['source_id']:
                            context = candidate
                            break
                if context is None:
                    raise PermissionError('EXPIRY_CONTEXT_REQUIRED')
                results.append(self.purge_media(task['media_id'], context))
        return results

    def resume_cleanup(self):
        """正常删除和崩溃恢复共用：屏障、压缩、派生物清理、完成凭据。"""
        from .validation import digest
        store = self.store
        store._ready()
        progress = self._progress()
        erased = store._erased | set(progress['post_contexts'])
        media_ids = store._erased_media | set(progress['media_ids'])
        progress['subjects'] = sorted(set(progress['subjects']) | erased)
        progress['media_ids'] = sorted(media_ids)
        prior = store.essential
        store.essential = True
        try:
            changed = erased != store._erased or media_ids != store._erased_media
            for values, original, name in ((erased, store._erased, 'erasure-barrier.json'),
                                           (media_ids, store._erased_media, 'media-erasure-barrier.json')):
                if values != original:
                    store.quota.require(65536, essential=True)
                    atomic_bytes(store._path('deletions/' + name), canonical(sorted(values)))
            if changed:
                store._hit('AFTER_ERASURE_BARRIER')
                store.recover()
            signature = digest({'posts': sorted(store._erased), 'media': sorted(store._erased_media),
                                'text': sorted(store._redacted_text), 'refs': sorted(store._redacted_media_refs)})
            has_barriers = bool(erased or media_ids or store._redacted_text or store._redacted_media_refs)
            if has_barriers and (not progress['compacted'] or progress['barriers'] != signature
                                 or store.erasure_replay_filtered):
                # 屏障后、墓碑前中断时，空事件链也必须有可提交的删除凭据。
                admin = {'source_id':'x:0', 'policy_id':'local-admin', 'policy_version':1, 'purpose':'local-management'}
                for post_id in sorted(erased):
                    if 'X:' + post_id not in store._tombstones:
                        store.tombstone(post_id, progress['post_contexts'].get(post_id, admin))
                if not store._events:
                    store.save_admin('OPERATION_LOG', {'operation': 'ERASURE_CLEANUP', 'outcome': 'SUCCESS',
                        'error_code': None, 'at': utc_now()}, admin)
                self._compact(copy.deepcopy(store._events))
                store._hit('AFTER_ERASURE_POINTER')
                store.recover()
                progress.update(compacted=True, barriers=signature)
                self._save_progress(progress)
            if not has_barriers and not progress['subjects']:
                return {'active_cleanup_completed': True, 'pending': []}
            keep = {task['local_path'] for task in store.media_tasks().values() if task['local_path']}
            for path in (store.root / 'media/blobs').rglob('*'):
                if path.is_file() and re.fullmatch(r'[a-f0-9]{64}', path.name):
                    relative = path.relative_to(store.root).as_posix()
                    if relative not in keep:
                        store._path(relative).unlink()
            for media_id in progress['media_ids']:
                store._path('media/' + media_id + '.part').unlink(missing_ok=True)
            for path in (store.root / 'quarantine/orphans').iterdir():
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            for path in (store.root / 'journal').glob('*.pending'):
                if re.fullmatch(r'[a-f0-9]{32}\.pending', path.name):
                    store._path(path.relative_to(store.root).as_posix()).unlink()
            pending = self._clean_artifacts(erased, set(progress['artifact_paths']))
            known = {item['path'] for item in self._catalog()}
            # 已清理的路径不再是永久删除目标；后续可在同一路径登记获准的新导出。
            # 仍未删成（例如 hash 改变）的文件保留目标，下一轮继续报告 pending。
            progress['artifact_paths'] = sorted(set(progress['artifact_paths']) & known)
            for folder in ('raw', 'exports', 'snapshots'):
                for path in (store.root / folder).rglob('*'):
                    if path.is_file() and path.relative_to(store.root).as_posix() not in known:
                        pending.append('UNTRACKED_DERIVED_ARTIFACT')
            progress['pending'] = sorted(set(pending))
            self._save_progress(progress)
            return {'active_cleanup_completed': not pending, 'pending': progress['pending']}
        except BaseException:
            store.poisoned = True
            raise
        finally:
            store.quota.invalidate()
            store.essential = prior

    def restrict_scope(self, context, scope):
        store = self.store
        text_ids, media_ids = set(store._redacted_text), set(store._redacted_media_refs)
        subjects = set()
        for event in store._events:
            if event['source_id'] != context['source_id']:
                continue
            payload = event['payload']
            if ('TEXT' not in scope and event['event_type'] in ('POST_UPSERT', 'REVIEW_STATE')
                    and (payload.get('text') is not None or payload.get('notes') is not None)):
                text_ids.add(event['event_id'])
                subjects.add(payload['post_id'])
            if 'MEDIA' not in scope and event['event_type'] == 'POST_UPSERT' and payload['media_refs']:
                media_ids.add(event['event_id'])
                subjects.add(payload['post_id'])
        tasks = [task for task in store.media_tasks().values()
                 if task['source_id'] == context['source_id'] and 'MEDIA' not in scope]
        subjects.update(task['post_id'] for task in tasks)
        if not subjects:
            return []
        self._queue_cleanup(subjects, [task['media_id'] for task in tasks])
        # 事件 ID 屏障不会误删以后重新获准采集的新版本；旧快照仍必须合并它。
        try:
            atomic_bytes(store._path('deletions/scope-erasure-barrier.json'),
                         canonical({'text': sorted(text_ids), 'media': sorted(media_ids)}))
            store._hit('AFTER_SCOPE_BARRIER')
            store.recover()
            result = self.resume_cleanup()
        except BaseException:
            store.poisoned = True
            raise
        result['external_backup_cleanup'] = self._external_cleanup()
        return [result]

    def revoke_source(self, context):
        subjects = []
        for post in self.store.current_posts().values():
            if context['source_id'] == 'x:' + post['author_external_id']:
                subjects.append(post['post_id'])
        reports = []
        for post_id in subjects:
            reports.append(self.purge(post_id, context))
        return reports

    def enforce_policy_expiry(self, gate, contexts):
        reports = []
        for context in contexts:
            try:
                policy = gate.require(context['source_id'], 'retain', context['purpose'],
                                      context['policy_id'], context['policy_version'])
            except PermissionError:
                reports.extend(self.revoke_source(context))
                continue
            if 'METADATA' not in policy['content_scope']:
                reports.extend(self.revoke_source(context))
            else:
                reports.extend(self.restrict_scope(context, policy['content_scope']))
        return reports
