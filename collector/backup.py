"""Owned external vaults, hash-verified snapshots and empty-root restore."""
import copy
import hashlib
import re
import shutil
import uuid
from pathlib import Path
from .security import checked_path, physical_disks, secure_path
from .storage import StorageEngine, atomic_bytes, replace_file
from .validation import canonical, strict_loads, utc_now


def sha_file(path):
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def safe_relative(value):
    if not isinstance(value, str) or '\\' in value or ':' in value:
        raise ValueError('INVALID_BACKUP_PATH')
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in ('..', '.') for part in path.parts):
        raise ValueError('INVALID_BACKUP_PATH')
    if path.parts[0] not in ('journal', 'media', 'deletions'):
        raise ValueError('BACKUP_PATH_NOT_ALLOWED')
    return path


class BackupManager:
    def __init__(self, store, disk_probe=None):
        self.store = store
        self.disk_probe = disk_probe or getattr(store, 'backup_disk_probe', physical_disks)
        self._verifications = {}
        identity = store._path('deletions/store-identity.json')
        if not identity.exists():
            atomic_bytes(identity, canonical({'store_id': uuid.uuid4().hex}))
        self.store_id = strict_loads(identity.read_bytes())['store_id']
        if not re.fullmatch(r'[a-f0-9]{32}', self.store_id):
            raise ValueError('INVALID_STORE_ID')

    def registry(self):
        path = self.store._path('deletions/backup-registry.json')
        rows = strict_loads(path.read_bytes()) if path.exists() else []
        if not isinstance(rows, list):
            raise ValueError('INVALID_BACKUP_REGISTRY')
        return rows

    def register(self, target):
        self.store._ready()
        root = self.store.root.resolve()
        target = checked_path(target)
        if target == root or target.is_relative_to(root) or root.is_relative_to(target):
            raise ValueError('BACKUP_MUST_BE_OUTSIDE_ACTIVE_ROOT')
        source_disks, target_disks = self.disk_probe(root), self.disk_probe(target)
        if not source_disks or not target_disks or source_disks & target_disks:
            raise ValueError('BACKUP_REQUIRES_OTHER_PHYSICAL_DISK')
        marker = target / '.acg-vault.json'
        if target.exists() and any(target.iterdir()) and not marker.exists():
            raise ValueError('EMPTY_OR_OWNED_VAULT_REQUIRED')
        secure_path(target, directory=True)
        if marker.exists():
            value = strict_loads(marker.read_bytes())
            if value['store_id'] != self.store_id:
                raise ValueError('VAULT_BELONGS_TO_ANOTHER_STORE')
        else:
            value = {'store_id': self.store_id, 'vault_id': uuid.uuid4().hex, 'schema_version': '1.0'}
            # 只有首次注册已验证为空的 vault 才能创建账本。
            # 先写账本再发布所有权标记；初始化中断后也不能被误认成可恢复 vault。
            atomic_bytes(target / 'ERASURES.json', canonical({
                'schema_version': '2.0',
                'posts': sorted(self.store._erased), 'media': sorted(self.store._erased_media),
                'text_events': sorted(self.store._redacted_text), 'media_events': sorted(self.store._redacted_media_refs)}))
            secure_path(target / 'ERASURES.json')
            atomic_bytes(marker, canonical(value))
            secure_path(marker)
        row = {'path': str(target), 'vault_id': value['vault_id'], 'store_id': self.store_id}
        self._ledger(row)
        rows = self.registry()
        rows = [old for old in rows if old['path'] != str(target)]
        rows.append(row)
        atomic_bytes(self.store._path('deletions/backup-registry.json'), canonical(rows))
        self.mirror_accounting()
        return row

    def _vault(self, row):
        path = checked_path(row['path'])
        marker = strict_loads((path / '.acg-vault.json').read_bytes())
        if marker['store_id'] != self.store_id or marker['vault_id'] != row['vault_id']:
            raise ValueError('VAULT_OWNERSHIP_MISMATCH')
        source_disks, target_disks = self.disk_probe(self.store.root), self.disk_probe(path)
        if not source_disks or not target_disks or source_disks & target_disks:
            raise ValueError('VAULT_DISK_CHANGED')
        return path

    def _ledger(self, row):
        vault = self._vault(row)
        path = vault / 'ERASURES.json'
        # 既有 vault 缺少删除记录时必须拒绝，不能把历史丢失当成从未删除。
        if not path.exists():
            raise ValueError('VAULT_ERASURES_MISSING')
        value = strict_loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError('INVALID_VAULT_ERASURES')
        # 旧账本没有字段级清理记录；只有新增的可选字段允许缺省为空。
        if value.get('schema_version') not in (None, '2.0'):
            raise ValueError('INVALID_VAULT_ERASURES')
        for key in ('text_events', 'media_events'):
            if value.get('schema_version') == '2.0' and key not in value:
                raise ValueError('INVALID_VAULT_ERASURES')
            value.setdefault(key, [])
        value['schema_version'] = '2.0'
        for key, pattern in (('posts', r'[0-9]{1,32}'), ('media', r'[a-f0-9]{32}'),
                             ('text_events', r'[a-f0-9]{32}'), ('media_events', r'[a-f0-9]{32}')):
            if not isinstance(value.get(key), list):
                raise ValueError('INVALID_VAULT_ERASURES')
            for entry in value[key]:
                if not isinstance(entry, str) or not re.fullmatch(pattern, entry):
                    raise ValueError('INVALID_VAULT_ERASURES')
        value['posts'] = sorted(set(value['posts']) | self.store._erased)
        value['media'] = sorted(set(value['media']) | self.store._erased_media)
        value['text_events'] = sorted(set(value['text_events']) | self.store._redacted_text)
        value['media_events'] = sorted(set(value['media_events']) | self.store._redacted_media_refs)
        raw = canonical(value)
        if path.read_bytes() != raw:
            atomic_bytes(path, raw)
            secure_path(path)
        return value

    def _copy_files(self, source, destination, relatives):
        files = {}
        total = 0
        for relative in relatives:
            safe_relative(relative)
            path = checked_path(source / relative)
            if not path.is_relative_to(source.resolve()) or not path.is_file():
                raise ValueError('INVALID_SNAPSHOT_FILE')
            total += path.stat().st_size
            if shutil.disk_usage(destination).free - path.stat().st_size < 10 * 1024 ** 3:
                raise RuntimeError('BACKUP_NO_SPACE')
            target = destination / relative
            secure_path(target.parent, directory=True)
            with path.open('rb') as inp, target.open('xb') as out:
                shutil.copyfileobj(inp, out, 1024 * 1024)
                out.flush()
                import os
                os.fsync(out.fileno())
            secure_path(target)
            files[relative] = {'bytes': target.stat().st_size, 'sha256': sha_file(target)}
        return files, total

    def _current_rights(self, row):
        from .policy import PolicyGate
        from .validation import check
        path = checked_path(self._vault(row) / 'POLICIES.json')
        # 旧快照的许可不能证明其后未撤权；当前凭据缺失时禁止回退。
        if not path.exists():
            raise ValueError('VAULT_POLICIES_MISSING')
        value = strict_loads(path.read_bytes())
        if not isinstance(value, dict) or set(value) != {'store_id', 'rights'}:
            raise ValueError('INVALID_VAULT_POLICIES')
        if value['store_id'] != self.store_id:
            raise ValueError('VAULT_POLICY_OWNER_MISMATCH')
        rights = value['rights']
        if (not isinstance(rights, dict) or set(rights) != {'policies', 'contexts'} or
                not isinstance(rights['policies'], list) or not isinstance(rights['contexts'], list)):
            raise ValueError('INVALID_VAULT_POLICIES')
        PolicyGate(rights['policies'])
        for context in rights['contexts']:
            check('context', context)
        return value

    def _relatives(self, engine):
        paths = set()
        current = engine.head
        if current:
            paths.add('journal/CURRENT.json')
        while current:
            cid = current['commit_id']
            manifest = 'journal/manifests/' + cid + '.json'
            paths.update((manifest, 'journal/segments/' + cid + '.jsonl'))
            current = strict_loads(engine._load(manifest))['previous']
        for task in engine.media_tasks().values():
            if task['state'] == 'DOWNLOADED' and task['local_path']:
                paths.add(task['local_path'])
        for name in ('erasure-barrier.json', 'media-erasure-barrier.json', 'scope-erasure-barrier.json', 'store-identity.json', 'format-floor.json'):
            relative = 'deletions/' + name
            if engine._path(relative).exists():
                paths.add(relative)
        return sorted(paths)

    def _write_snapshot(self, engine, row, snapshot_id=None, rights=None):
        vault = self._vault(row)
        folder = vault / 'snapshots'
        secure_path(folder, directory=True)
        sid = snapshot_id or uuid.uuid4().hex
        revision = uuid.uuid4().hex
        stage = folder / (revision + '.staging-' + uuid.uuid4().hex)
        secure_path(stage, directory=True)
        atomic_bytes(stage / '.stage-owner.json', canonical({'store_id': self.store_id, 'vault_id': row['vault_id']}))
        files, total = self._copy_files(engine.root, stage, self._relatives(engine))
        manifest = {'schema_version': '1.0', 'store_id': self.store_id, 'vault_id': row['vault_id'],
                    'snapshot_id': sid, 'created_at': utc_now(), 'files': files, 'total_bytes': total, 'rights': rights}
        atomic_bytes(stage / 'manifest.json', canonical(manifest))
        secure_path(stage / 'manifest.json')
        (stage / '.stage-owner.json').unlink()
        final = folder / revision
        replace_file(stage, final)
        index = self._index(row)
        old_revision = index.get(sid)
        index[sid] = revision
        atomic_bytes(vault / 'snapshot-index.json', canonical(index))
        if old_revision:
            old = checked_path(folder / old_revision)
            previous = strict_loads((old / 'manifest.json').read_bytes())
            if previous['store_id'] != self.store_id or previous['vault_id'] != row['vault_id'] or previous['snapshot_id'] != sid:
                raise ValueError('OLD_SNAPSHOT_OWNERSHIP_MISMATCH')
            shutil.rmtree(old)
        return manifest

    def create(self, row, gate=None, contexts=None):
        self.store._ready()
        if gate is None:
            raise PermissionError('BACKUP_PERMISSION_REVIEW_REQUIRED')
        self.mirror_accounting()
        from .deletion import DeletionManager
        DeletionManager(self.store).enforce_policy_expiry(gate, contexts or [])
        DeletionManager(self.store).resume_cleanup()
        self._ledger(row)
        # Every retained source must have matching valid retention evidence.
        synced = self.update_policies(gate, contexts or [])
        if not any(item['vault_id'] == row['vault_id'] and item['completed'] for item in synced):
            raise ValueError('VAULT_POLICY_SYNC_FAILED')
        known = {}
        for context in contexts or []:
            known[context['source_id']] = context
        for event in self.store._events:
            if event['event_type'] in ('POST_UPSERT', 'POST_OBSERVATION', 'MEDIA_STATE', 'REVIEW_STATE'):
                context = known.get(event['source_id'])
                if context is None:
                    raise PermissionError('BACKUP_SOURCE_CONTEXT_REQUIRED')
                policy = gate.require(context['source_id'], 'retain', context['purpose'], context['policy_id'], context['policy_version'])
                if event['event_type'] == 'POST_UPSERT' and event['payload']['text'] and 'TEXT' not in policy['content_scope']:
                    raise PermissionError('BACKUP_TEXT_RETENTION_DENIED')
                if event['event_type'] == 'MEDIA_STATE' and 'MEDIA' not in policy['content_scope']:
                    raise PermissionError('BACKUP_MEDIA_RETENTION_DENIED')
        # Media with expired retention is never copied.
        from .validation import digest
        mapping = {}
        for context in contexts or []:
            mapping[digest(context)] = context
        DeletionManager(self.store).expire(mapping)
        return self._write_snapshot(self.store, row, rights={'policies': list(gate.policies.values()), 'contexts': contexts or []})

    def _index(self, row):
        file = self._vault(row) / 'snapshot-index.json'
        index = strict_loads(file.read_bytes()) if file.exists() else {}
        if not isinstance(index, dict):
            raise ValueError('INVALID_SNAPSHOT_INDEX')
        for sid, revision in index.items():
            if not re.fullmatch(r'[a-f0-9]{32}', sid) or not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{32}', revision):
                raise ValueError('INVALID_SNAPSHOT_INDEX')
        return index

    def list_ids(self, row):
        return list(self._index(row))

    def snapshot_directory(self, row, snapshot_id):
        index = self._index(row)
        if snapshot_id not in index:
            raise ValueError('SNAPSHOT_NOT_COMMITTED')
        return self._vault(row) / 'snapshots' / index[snapshot_id]

    def describe(self, row, snapshot_id):
        """列表只读取当前 revision 的 manifest；历史校验结果不表示文件目前完整。"""
        directory, manifest, signature = self._manifest(row, snapshot_id)
        receipt = self._verifications.get((row['vault_id'], snapshot_id))
        status, verified_at = 'NOT_VERIFIED', None
        if receipt and receipt['signature'] == signature:
            status, verified_at = receipt['status'], receipt['at']
        return dict(manifest, verification_status=status, last_verified_at=verified_at)

    def _manifest(self, row, snapshot_id):
        if not re.fullmatch(r'[a-f0-9]{32}', snapshot_id):
            raise ValueError('INVALID_SNAPSHOT_ID')
        directory = self.snapshot_directory(row, snapshot_id)
        checked_path(directory)
        raw = checked_path(directory / 'manifest.json').read_bytes()
        manifest = strict_loads(raw)
        if (manifest['store_id'], manifest['vault_id'], manifest['snapshot_id']) != (self.store_id, row['vault_id'], snapshot_id):
            raise ValueError('SNAPSHOT_OWNERSHIP_MISMATCH')
        if not isinstance(manifest['files'], dict) or type(manifest['total_bytes']) is not int or manifest['total_bytes'] < 0:
            raise ValueError('INVALID_SNAPSHOT_MANIFEST')
        for relative, info in manifest['files'].items():
            safe_relative(relative)
            if (not isinstance(info, dict) or type(info.get('bytes')) is not int or info['bytes'] < 0
                    or not isinstance(info.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', info['sha256'])):
                raise ValueError('INVALID_SNAPSHOT_MANIFEST')
        return directory, manifest, (directory.name, hashlib.sha256(raw).hexdigest())

    def verify(self, row, snapshot_id):
        key = (row['vault_id'], snapshot_id)
        self._verifications.pop(key, None)
        directory, manifest, signature = self._manifest(row, snapshot_id)
        # 校验中断或失败不能继续显示上次成功；恢复始终重新执行下面的完整检查。
        self._verifications[key] = {'signature': signature, 'status': 'VERIFICATION_FAILED', 'at': utc_now()}
        for relative, info in manifest['files'].items():
            path = checked_path(directory / safe_relative(relative))
            if not path.is_relative_to(directory.resolve()) or not path.is_file() or path.stat().st_size != info['bytes'] or sha_file(path) != info['sha256']:
                raise ValueError('SNAPSHOT_HASH_MISMATCH')
        # Reject hidden/unlisted data rather than trusting a manifest that omits it.
        actual = set()
        for path in directory.rglob('*'):
            checked_path(path)
            if path.is_file() and path.name != 'manifest.json':
                actual.add(path.relative_to(directory).as_posix())
        if actual != set(manifest['files']):
            raise ValueError('SNAPSHOT_UNLISTED_FILE')
        self._verifications[key] = {'signature': signature, 'status': 'VERIFIED_PREVIOUSLY', 'at': utc_now()}
        return manifest

    def restore(self, row, snapshot_id, target):
        manifest = self.verify(row, snapshot_id)
        target = checked_path(target)
        vault = self._vault(row)
        if (target.exists() and any(target.iterdir())) or target.is_relative_to(vault) or target.is_relative_to(self.store.root.resolve()):
            raise ValueError('RESTORE_REQUIRES_NEW_EMPTY_ROOT')
        ledger = self._ledger(row)
        current_rights = self._current_rights(row)
        manifest = dict(manifest, rights=current_rights['rights'])
        secure_path(target, directory=True)
        restore_token = uuid.uuid4().hex
        atomic_bytes(target / '.restore-in-progress.json', canonical({'token': restore_token, 'store_id': self.store_id, 'snapshot_id': snapshot_id}))
        source = self.snapshot_directory(row, snapshot_id)
        copied, copied_bytes = self._copy_files(source, target, manifest['files'])
        if copied != manifest['files']:
            raise ValueError('SNAPSHOT_CHANGED_DURING_RESTORE')
        # Merge current vault + active deletion records BEFORE loading any old events.
        for key, name in (('posts', 'erasure-barrier.json'), ('media', 'media-erasure-barrier.json')):
            path = target / 'deletions' / name
            old = strict_loads(path.read_bytes()) if path.exists() else []
            path.parent.mkdir(exist_ok=True)
            atomic_bytes(path, canonical(sorted(set(old) | set(ledger[key]))))
        self._merge_scope_barrier(target, ledger)
        from .deletion import DeletionManager
        with StorageEngine(target, restore_token=restore_token) as restored:
            self._merge_accounting(restored, row)
            self._prepare_erasures(restored)
            DeletionManager(restored).resume_cleanup()
            self._filter_restored(restored, manifest)
            settings_rows = restored.admin_events('SETTINGS_STATE')
            if settings_rows:
                settings = settings_rows[-1]
                settings['paused'], settings['schedule_enabled'] = True, False
                restored.save_admin('SETTINGS_STATE', settings, {'source_id':'x:0','policy_id':'local-admin','policy_version':1,'purpose':'local-management'})
            status = restored.status()
            (target / '.restore-in-progress.json').unlink()
        return {'target': str(target), 'snapshot_id': snapshot_id, 'status': status}

    def _merge_scope_barrier(self, target, ledger):
        path = target / 'deletions/scope-erasure-barrier.json'
        value = strict_loads(path.read_bytes()) if path.exists() else {'text': [], 'media': []}
        if not isinstance(value, dict) or set(value) != {'text', 'media'}:
            raise ValueError('INVALID_SCOPE_BARRIER')
        for key, ledger_key in (('text', 'text_events'), ('media', 'media_events')):
            if not isinstance(value[key], list) or any(
                    not isinstance(item, str) or not re.fullmatch(r'[a-f0-9]{32}', item) for item in value[key]):
                raise ValueError('INVALID_SCOPE_BARRIER')
            value[key] = sorted(set(value[key]) | set(ledger[ledger_key]))
        path.parent.mkdir(exist_ok=True)
        atomic_bytes(path, canonical(value))

    def mirror_accounting(self):
        from .validation import digest
        accounts = {}
        for account in self.store.admin_events('ACCOUNT_STATE'):
            accounts[account['account_id']] = account
        payload = {'jobs': list(self.store.jobs().values()), 'accounts': list(accounts.values())}
        document = {'store_id': self.store_id, 'payload': payload, 'payload_hash': digest(payload)}
        for row in self.registry():
            vault = self._vault(row)
            atomic_bytes(vault / 'ACCOUNTING.json', canonical(document))
            secure_path(vault / 'ACCOUNTING.json')

    def _merge_accounting(self, restored, row):
        from .validation import check, digest, parse_utc
        from .sync import money
        document = strict_loads((self._vault(row) / 'ACCOUNTING.json').read_bytes())
        if document['store_id'] != self.store_id or document['payload_hash'] != digest(document['payload']):
            raise ValueError('VAULT_ACCOUNTING_INTEGRITY')
        admin = {'source_id':'x:0','policy_id':'local-admin','policy_version':1,'purpose':'local-management'}
        known_accounts = {}
        for account in restored.admin_events('ACCOUNT_STATE'):
            known_accounts[account['account_id']] = account
        for account in document['payload']['accounts']:
            check('account', account)
            if account['account_id'] not in known_accounts:
                restored.save_admin('ACCOUNT_STATE', account, admin)
        jobs = restored.jobs()
        incoming = list(document['payload']['jobs']) + list(self.store.jobs().values())
        for job in incoming:
            check('job', job)
            previous = jobs.get(job['job_id'])
            if previous:
                for key in ('account_id', 'source_id', 'actor_id', 'actor_build', 'input_fingerprint'):
                    if previous[key] != job[key]:
                        raise ValueError('RESTORE_ACCOUNTING_IDENTITY_CONFLICT')
                for key in ('run_id', 'dataset_id'):
                    if previous[key] and job[key] and previous[key] != job[key]:
                        raise ValueError('RESTORE_ACCOUNTING_REMOTE_CONFLICT')
                chosen = copy.deepcopy(job if parse_utc(job['updated_at']) >= parse_utc(previous['updated_at']) else previous)
                chosen['reserved_usd'] = str(max(money(job['reserved_usd']),money(previous['reserved_usd'])))
                for key in ('run_id', 'dataset_id', 'run_evidence_uri'):
                    chosen[key] = chosen[key] or previous[key] or job[key]
                finalized = []
                for candidate in (previous,job):
                    if candidate['finalized_usd'] is not None:
                        finalized.append(candidate)
                if finalized:
                    bill = max(finalized, key=lambda candidate: money(candidate['finalized_usd']))
                    chosen['finalized_usd'] = bill['finalized_usd']
                    chosen['bill_evidence_uri'] = bill['bill_evidence_uri']
                job = chosen
            jobs[job['job_id']] = copy.deepcopy(job)
        for job in jobs.values():
            # Accounting cannot prove that Dataset pages exist in this snapshot.
            if job['dataset_id'] is not None and job['state'] in ('READING', 'DONE', 'NEEDS_REVIEW'):
                job.update(state='READING', offset=0, pages=0, accepted=0, rejected=0,
                           candidate_cursor=job['previous_cursor'], oldest=None, newest=None,
                           coverage_status='UNKNOWN', coverage_reason='RESTORE_REPLAY_REQUIRED')
            # Target is not yet registered to a vault; this writes locally without recursion.
            restored.save_job(job, admin)
        current_schedule = self.store.admin_events('SCHEDULE_STATE')
        if current_schedule:
            restored.save_admin('SCHEDULE_STATE',current_schedule[-1],admin)

    def update_policies(self, gate, contexts):
        results = []
        for row in self.registry():
            try:
                vault = self._vault(row)
                path = vault / 'POLICIES.json'
                raw = canonical({'store_id': self.store_id,
                    'rights': {'policies': list(gate.policies.values()), 'contexts': contexts}})
                if not path.exists() or path.read_bytes() != raw:
                    atomic_bytes(path, raw)
                results.append({'vault_id': row['vault_id'], 'completed': True})
            except Exception as exc:
                results.append({'vault_id': row['vault_id'], 'completed': False, 'error': type(exc).__name__})
        return results

    def _prepare_erasures(self, engine):
        admin = {'source_id':'x:0','policy_id':'local-admin','policy_version':1,'purpose':'local-management'}
        for post_id in engine._erased:
            if 'X:' + post_id not in engine._tombstones:
                engine.tombstone(post_id, admin)

    def _filter_restored(self, engine, manifest):
        from .policy import PolicyGate
        from .deletion import DeletionManager
        from .validation import digest
        rights = manifest.get('rights')
        if not isinstance(rights, dict):
            raise PermissionError('RESTORE_PERMISSION_EVIDENCE_REQUIRED')
        gate = PolicyGate(rights['policies'])
        admin = {'source_id':'x:0','policy_id':'local-admin','policy_version':1,'purpose':'local-management'}
        for policy in rights['policies']:
            engine.save_admin('POLICY_STATE', policy, admin)
        contexts = {}
        for context in rights['contexts']:
            contexts[context['source_id']] = context
        for post in list(engine.current_posts().values()):
            context = contexts.get('x:' + post['author_external_id'])
            if context is None:
                raise PermissionError('RESTORE_SOURCE_CONTEXT_REQUIRED')
        DeletionManager(engine).enforce_policy_expiry(gate, list(contexts.values()))
        by_hash = {}
        for context in rights['contexts']:
            by_hash[digest(context)] = context
        for task in list(engine.media_tasks().values()):
            context = contexts.get(task['source_id'])
            if context is None:
                raise PermissionError('RESTORE_MEDIA_CONTEXT_REQUIRED')
            permitted = True
            for operation in ('access', 'retain', 'download_media'):
                try:
                    policy = gate.require(context['source_id'], operation, context['purpose'], context['policy_id'], context['policy_version'])
                    if operation != 'access' and 'MEDIA' not in policy['content_scope']:
                        permitted = False
                except PermissionError:
                    permitted = False
            from .validation import parse_utc
            if not permitted or parse_utc(task['expires_at']) <= parse_utc(utc_now()):
                DeletionManager(engine).purge_media(task['media_id'], context)

    def propagate_erasure(self):
        results = []
        for row in self.registry():
            try:
                ledger = self._ledger(row)
                vault = self._vault(row)
                receipt_path = vault / 'erasure-receipts.json'
                receipts = strict_loads(receipt_path.read_bytes()) if receipt_path.exists() else {}
                from .validation import digest
                snapshot_ids = self.list_ids(row)
                # 尚未创建快照的新 vault 可以没有凭据；既有快照必须按当前凭据清理。
                current_rights = self._current_rights(row) if snapshot_ids else None
                ledger_hash = digest({'erasures': ledger, 'policies': current_rights})
                processed, pending = 0, False
                for snapshot_id in snapshot_ids:
                    revision = self._index(row)[snapshot_id]
                    if receipts.get(snapshot_id) == [revision, ledger_hash]:
                        continue
                    if processed >= 1:
                        pending = True
                        continue
                    processed += 1
                    path = self.snapshot_directory(row, snapshot_id)
                    self.verify(row, snapshot_id)
                    temp = vault / ('scrub-' + uuid.uuid4().hex)
                    # restore normally rejects a target within the vault. Use a private owned staging sibling
                    # outside the snapshot tree, and explicitly materialize verified files here.
                    secure_path(temp, directory=True)
                    atomic_bytes(temp / '.stage-owner.json', canonical({'store_id': self.store_id, 'vault_id': row['vault_id']}))
                    manifest = self.verify(row, snapshot_id)
                    self._copy_files(path, temp, manifest['files'])
                    for key, name in (('posts', 'erasure-barrier.json'), ('media', 'media-erasure-barrier.json')):
                        file = temp / 'deletions' / name
                        old = strict_loads(file.read_bytes()) if file.exists() else []
                        file.parent.mkdir(exist_ok=True)
                        atomic_bytes(file, canonical(sorted(set(old) | set(ledger[key]))))
                    self._merge_scope_barrier(temp, ledger)
                    from .deletion import DeletionManager
                    with StorageEngine(temp) as engine:
                        self._prepare_erasures(engine)
                        DeletionManager(engine).resume_cleanup()
                        if current_rights is not None:
                            manifest = dict(manifest, rights=current_rights['rights'])
                        self._filter_restored(engine, manifest)
                        self._write_snapshot(engine, row, snapshot_id, rights=manifest['rights'])
                    shutil.rmtree(temp)
                    receipts[snapshot_id] = [self._index(row)[snapshot_id], ledger_hash]
                    atomic_bytes(receipt_path, canonical(receipts))
                self.cleanup_orphans(row)
                results.append({'vault_id': row['vault_id'], 'completed': not pending})
            except Exception as exc:
                results.append({'vault_id': row['vault_id'], 'completed': False, 'error': type(exc).__name__})
        return results

    def cleanup_orphans(self, row):
        vault = self._vault(row)
        referenced = set(self._index(row).values())
        candidates = []
        folder = vault / 'snapshots'
        if folder.exists():
            for path in folder.iterdir():
                if path.name not in referenced:
                    candidates.append(path)
        candidates.extend(vault.glob('scrub-*'))
        for path in candidates:
            checked_path(path)
            if not path.is_dir() or not re.fullmatch(r'(?:[a-f0-9]{32}(?:\.staging-[a-f0-9]{32})?|scrub-[a-f0-9]{32})', path.name):
                raise ValueError('UNOWNED_VAULT_ORPHAN')
            proof = path / 'manifest.json' if (path / 'manifest.json').exists() else path / '.stage-owner.json'
            owner = strict_loads(proof.read_bytes())
            if owner.get('store_id') != self.store_id or owner.get('vault_id') != row['vault_id']:
                raise ValueError('VAULT_ORPHAN_OWNERSHIP_MISMATCH')
            for child in path.rglob('*'):
                checked_path(child)
            shutil.rmtree(path)
