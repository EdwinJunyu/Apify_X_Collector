"""Durable anti-resurrection barrier + atomic chain compaction + tracked cleanup.

This is logical filesystem erasure, not SSD secure erase. M6 must preserve and
merge the barrier before restoring into a new data root.
"""
import copy
import hashlib
import re
import uuid
from .storage import atomic_bytes, replace_file, sync_directory, MAX_SEGMENT
from .validation import canonical, strict_loads


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
        path = self.store._path(relative)
        if relative.split('/')[0] not in ('raw', 'exports', 'snapshots') or not path.is_file():
            raise ValueError('INVALID_ARTIFACT')
        for post_id in post_ids:
            if not isinstance(post_id, str) or not re.fullmatch(r'[0-9]{1,32}', post_id):
                raise ValueError('INVALID_POST_ID')
        record = {'post_ids': post_ids, 'path': relative,
                  'hash': hashlib.sha256(path.read_bytes()).hexdigest()}
        catalog = self._catalog()
        catalog.append(record)
        atomic_bytes(self.store._path('deletions/artifacts.json'), canonical(catalog))

    def purge(self, post_id, context):
        store = self.store
        store._ready()
        if not isinstance(post_id, str) or not re.fullmatch(r'[0-9]{1,32}', post_id):
            raise ValueError('INVALID_POST_ID')
        post = store.current_posts().get('X:' + post_id)
        if post and context['source_id'] != 'x:' + post['author_external_id']:
            raise PermissionError('SOURCE_ACCOUNT_MISMATCH')
        media = store.media_tasks()
        erased = set(store._erased)
        erased.add(post_id)
        store.quota.require(65536, essential=True)
        atomic_bytes(store._path('deletions/erasure-barrier.json'), canonical(sorted(erased)))
        store._erased = erased
        store._hit('AFTER_ERASURE_BARRIER')
        prior = store.essential
        store.essential = True
        try:
            store.tombstone(post_id, context)
            # Filter in-memory history before touching old physical segments.
            retained = []
            for event in store._events:
                if event['event_type'] in ('POST_UPSERT', 'MEDIA_STATE', 'REVIEW_STATE') and event['payload']['post_id'] in erased:
                    continue
                retained.append(copy.deepcopy(event))
            self._compact(retained)
            store._hit('AFTER_ERASURE_POINTER')
            store.recover()
            # Every unreferenced segment is now discarded, including old chain copies.
            for path in (store.root / 'quarantine/orphans').iterdir():
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            for path in (store.root / 'journal').glob('*.pending'):
                if re.fullmatch(r'[a-f0-9]{32}\.pending', path.name):
                    store._path(path.relative_to(store.root).as_posix()).unlink()
            # Content-addressed blob ownership is still per authorized source.
            keep = set()
            for task in media.values():
                if task['post_id'] not in erased and task['local_path']:
                    keep.add(task['local_path'])
            for task in media.values():
                if task['post_id'] in erased:
                    if task['local_path'] and task['local_path'] not in keep:
                        store._path(task['local_path']).unlink(missing_ok=True)
                    store._path('media/' + task['media_id'] + '.part').unlink(missing_ok=True)
            active_blobs = set()
            for task in store.media_tasks().values():
                if task['local_path']:
                    active_blobs.add(task['local_path'])
            for path in (store.root / 'media/blobs').rglob('*'):
                if path.is_file() and re.fullmatch(r'[a-f0-9]{64}', path.name):
                    relative = path.relative_to(store.root).as_posix()
                    if relative not in active_blobs:
                        store._path(relative).unlink()
            pending = self._clean_artifacts(erased)
            # No false completion while unknown snapshot/export/raw copies exist.
            catalog_paths = set()
            for item in self._catalog():
                catalog_paths.add(item['path'])
            for folder in ('raw', 'exports', 'snapshots'):
                for path in (store.root / folder).rglob('*'):
                    if path.is_file() and path.relative_to(store.root).as_posix() not in catalog_paths:
                        pending.append('UNTRACKED_DERIVED_ARTIFACT')
            report = {'post_id': post_id, 'active_cleanup_completed': not pending,
                      'pending': pending, 'external_backup_cleanup': self._external_cleanup()}
            atomic_bytes(store._path('deletions/cleanup-' + post_id + '.json'), canonical(report))
            return report
        except BaseException:
            store.poisoned = True
            raise
        finally:
            store.essential = prior

    def _clean_artifacts(self, erased):
        remaining = []
        pending = []
        for item in self._catalog():
            if any(post_id in erased for post_id in item['post_ids']):
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
        atomic_bytes(self.store._path('deletions/artifacts.json'), canonical(remaining))
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
        from .validation import digest
        # Revocation advances the policy version. Erasure must remain possible with
        # the current source-bound context even though acquisition permits are stale.
        if task['source_id'] != context['source_id']:
            raise PermissionError('CONTEXT_MISMATCH')
        erased = set(store._erased_media)
        erased.add(media_id)
        atomic_bytes(store._path('deletions/media-erasure-barrier.json'), canonical(sorted(erased)))
        store._erased_media = erased
        retained = []
        for event in store._events:
            if event['event_type'] == 'MEDIA_STATE' and event['payload']['media_id'] in erased:
                continue
            if event['event_type'] in ('POST_UPSERT', 'MEDIA_STATE', 'REVIEW_STATE') and event['payload']['post_id'] in store._erased:
                continue
            retained.append(copy.deepcopy(event))
        prior = store.essential
        store.essential = True
        try:
            self._compact(retained)
            store.recover()
            for path in (store.root / 'quarantine/orphans').iterdir():
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            referenced = set()
            for other in store.media_tasks().values():
                if other['local_path']:
                    referenced.add(other['local_path'])
            if task['local_path'] and task['local_path'] not in referenced:
                store._path(task['local_path']).unlink(missing_ok=True)
            store._path('media/' + media_id + '.part').unlink(missing_ok=True)
            pending = self._clean_artifacts({task['post_id']})
            known = set()
            for item in self._catalog():
                known.add(item['path'])
            for folder in ('raw', 'exports', 'snapshots'):
                for path in (store.root / folder).rglob('*'):
                    if path.is_file() and path.relative_to(store.root).as_posix() not in known:
                        pending.append('UNTRACKED_DERIVED_ARTIFACT')
            return {'media_id': media_id, 'active_media_cleanup_completed': not pending, 'pending': pending, 'external_backup_cleanup': self._external_cleanup()}
        except BaseException:
            store.poisoned = True
            raise
        finally:
            store.essential = prior

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
        """Finish physical orphan cleanup after an interrupted erasure operation."""
        store = self.store
        store._ready()
        prior = store.essential
        store.essential = True
        try:
            self._compact(copy.deepcopy(store._events))
            store.recover()
            keep = set()
            for task in store.media_tasks().values():
                if task['local_path']:
                    keep.add(task['local_path'])
            for path in (store.root / 'media/blobs').rglob('*'):
                if path.is_file() and re.fullmatch(r'[a-f0-9]{64}', path.name):
                    relative = path.relative_to(store.root).as_posix()
                    if relative not in keep:
                        store._path(relative).unlink()
            for media_id in store._erased_media:
                store._path('media/' + media_id + '.part').unlink(missing_ok=True)
            for path in (store.root / 'quarantine/orphans').iterdir():
                if path.is_file() and not path.is_symlink():
                    path.unlink()
        except BaseException:
            store.poisoned = True
            raise
        finally:
            store.essential = prior

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
                gate.require(context['source_id'], 'retain', context['purpose'],
                             context['policy_id'], context['policy_version'])
            except PermissionError:
                reports.extend(self.revoke_source(context))
        return reports
