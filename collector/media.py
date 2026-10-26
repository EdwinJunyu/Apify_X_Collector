"""Durable serial media queue; serial execution stays below two-download cap."""
import hashlib
import os
import uuid
from pathlib import Path
from .media_http import MediaError
from .quota import NoSpace
from .storage import replace_file
from .validation import digest, parse_utc, utc_now


def detect_mime(path):
    with path.open('rb') as stream:
        header = stream.read(32)
        stream.seek(max(0, path.stat().st_size - 12))
        tail = stream.read()
    if header.startswith(b'\x89PNG\r\n\x1a\n') and b'IEND' in tail and header[12:16] == b'IHDR':
        return 'image/png'
    if header.startswith(b'\xff\xd8\xff') and tail.endswith(b'\xff\xd9'):
        return 'image/jpeg'
    if header[:6] in (b'GIF87a', b'GIF89a') and tail.endswith(b';'):
        return 'image/gif'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'image/webp'
    if len(header) >= 16 and header[4:8] == b'ftyp' and header[8:12] in (b'isom', b'iso2', b'mp41', b'mp42', b'avc1'):
        return 'video/mp4'
    raise MediaError('UNSUPPORTED_FORMAT')


class MediaManager:
    def __init__(self, store, gate, transport, max_file=250 * 1024 ** 2, batch_limit=1024 ** 3):
        if type(max_file) is not int or max_file <= 0 or type(batch_limit) is not int or batch_limit <= 0:
            raise ValueError('INVALID_MEDIA_LIMIT')
        self.store, self.gate, self.transport = store, gate, transport
        self.max_file, self.batch_limit = max_file, batch_limit

    def _authorize(self, task, context):
        if digest(context) != task['context_hash'] or context['source_id'] != task['source_id']:
            raise PermissionError('CONTEXT_MISMATCH')
        for action in ('access', 'retain', 'download_media'):
            policy = self.gate.require(task['source_id'], action, context['purpose'],
                                       context['policy_id'], context['policy_version'])
            if action in ('retain', 'download_media') and 'MEDIA' not in policy['content_scope']:
                raise PermissionError('MEDIA_SCOPE_REQUIRED')
            if parse_utc(task['expires_at']) > parse_utc(policy['valid_until']):
                raise PermissionError('RETENTION_EXCEEDS_POLICY')
        if parse_utc(task['expires_at']) <= parse_utc(utc_now()):
            raise PermissionError('RETENTION_EXPIRED')
        post = self.store.current_posts().get('X:' + task['post_id'])
        if post is None or post['account_id'] != task['account_id'] or task['source_id'] != 'x:' + post['author_external_id']:
            raise PermissionError('POST_UNAVAILABLE')

    def _rating(self, task):
        if (task['restricted'] or task['content_rating'] == 'UNKNOWN' or not task['rating_evidence'] or
                (task['content_rating'] == 'R18' and not task['allow_r18'])):
            raise MediaError('BLOCKED_RATING')

    def enqueue(self, post_id, url, context, rating, rating_evidence, expires_at,
                restricted=False, allow_r18=False):
        post = self.store.current_posts().get('X:' + post_id)
        if post is None:
            raise PermissionError('POST_UNAVAILABLE')
        task = {'media_id': uuid.uuid4().hex, 'post_id': post_id, 'account_id': post['account_id'],
                'source_id': context['source_id'], 'context_hash': digest(context), 'source_url': url,
                'content_rating': rating, 'rating_evidence': rating_evidence, 'restricted': restricted,
                'allow_r18': allow_r18, 'expires_at': expires_at, 'state': 'QUEUED',
                'sha256': None, 'local_path': None, 'mime': None, 'file_size': 0, 'attempts': 0,
                'error_code': None, 'updated_at': utc_now()}
        # Schema check before any network or persistence.
        from .validation import check
        check('media_task', task)
        try:
            self._authorize(task, context)
            self._rating(task)
        except (PermissionError, MediaError) as exc:
            task['state'] = 'BLOCKED'
            task['source_url'] = None  # no unlicensed URL in diagnostics
            task['error_code'] = 'NO_RIGHTS' if isinstance(exc, PermissionError) else 'BLOCKED_RATING'
        self.store.save_media(task, context)
        return task['media_id']

    def process(self, media_id, context, remaining_batch=None):
        task = self.store.media_tasks()[media_id]
        if (task['state'] in ('DOWNLOADED', 'BLOCKED') or task['attempts'] >= 3 or
                (task['state'] == 'FAILED' and task['error_code'] not in ('NETWORK_ERROR', 'NO_SPACE'))):
            return task
        temp = self.store._path('media/' + media_id + '.part')
        try:
            self._authorize(task, context)
            self._rating(task)
            if temp.exists():
                temp.unlink()  # implementation-owned deterministic partial file
            task['attempts'] += 1
            task['state'] = 'DOWNLOADING'
            self.store.save_media(task, context)
            stream = self.transport.stream(task['source_url'])
            try:
                header = next(stream)
                # 连完整批次都放不下的文件是永久超限；不能无限等待“下一批”。
                file_limit = min(self.max_file, self.batch_limit) if remaining_batch is not None else self.max_file
                if header['length'] is not None and header['length'] > file_limit:
                    raise MediaError('OVERSIZE')
                if remaining_batch is not None and header['length'] is not None and header['length'] > remaining_batch:
                    raise MediaError('BATCH_LIMIT')
                size = 0
                hasher = hashlib.sha256()
                with self.store.quota.transfer(temp) as reserve_chunk, temp.open('xb') as output:
                    for chunk in stream:
                        if not isinstance(chunk, bytes):
                            raise MediaError('NETWORK_ERROR')
                        size += len(chunk)
                        if size > file_limit:
                            raise MediaError('OVERSIZE')
                        if remaining_batch is not None and size > remaining_batch:
                            raise MediaError('BATCH_LIMIT')
                        reserve_chunk(len(chunk))
                        output.write(chunk)
                        hasher.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if header['length'] is not None and size != header['length']:
                    raise MediaError('NETWORK_ERROR')
                mime = detect_mime(temp)
                if mime != header['mime']:
                    raise MediaError('UNSUPPORTED_FORMAT')
                self._authorize(task, context)  # policy may expire during transfer
                sha = hasher.hexdigest()
                relative = 'media/blobs/' + sha[:2] + '/' + sha
                target = self.store._path(relative)
                target.parent.mkdir(exist_ok=True)
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                        raise MediaError('BLOB_INTEGRITY')
                    temp.unlink()
                else:
                    replace_file(temp, target)
                task.update({'state': 'DOWNLOADED', 'sha256': sha, 'local_path': relative,
                             'mime': mime, 'file_size': size, 'error_code': None})
            finally:
                stream.close()
        except (PermissionError, MediaError, NoSpace, OSError) as exc:
            task['state'] = 'FAILED'
            if isinstance(exc, PermissionError):
                task['error_code'] = 'NO_RIGHTS'
                task['state'] = 'BLOCKED'
            elif isinstance(exc, NoSpace):
                task['error_code'] = 'NO_SPACE'
            elif isinstance(exc, MediaError):
                task['error_code'] = str(exc)
                if task['error_code'] == 'BATCH_LIMIT':
                    # 本批额度不足不是坏文件，也不消耗网络失败重试次数。
                    task['state'] = 'QUEUED'
                    task['attempts'] -= 1
            else:
                task['error_code'] = 'NETWORK_ERROR'
            if temp.exists():
                temp.unlink()
        task['updated_at'] = utc_now()
        prior = self.store.essential
        try:
            self.store.essential = True  # bounded error metadata can use safety reserve
            self.store.save_media(task, context)
        finally:
            self.store.essential = prior
        return task

    def process_batch(self, context):
        used = 0
        results = []
        for task in self.store.media_tasks().values():
            if task['context_hash'] != digest(context) or task['state'] not in ('QUEUED', 'FAILED', 'DOWNLOADING'):
                continue
            if used >= self.batch_limit:
                break
            result = self.process(task['media_id'], context, self.batch_limit - used)
            if result['state'] == 'DOWNLOADED':
                used += result['file_size']
            results.append(result)
            if result['error_code'] == 'BATCH_LIMIT':
                break
        return results

    def reauthorize(self, media_id, context, rating, evidence, expires_at, allow_r18=False):
        task = self.store.media_tasks()[media_id]
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError('REAUTHORIZATION_EVIDENCE_REQUIRED')
        # 仅更新同一素材的授权；不改 URL、内容 hash 或最初的来源归属。
        if context['source_id'] != task['source_id'] or task['source_url'] is None:
            raise PermissionError('CONTEXT_MISMATCH')
        task.update(context_hash=digest(context), content_rating=rating,
                    rating_evidence=evidence.strip(), expires_at=expires_at,
                    allow_r18=allow_r18, authorization_evidence_uri=evidence.strip())
        self._authorize(task, context)
        self._rating(task)
        if task['state'] == 'DOWNLOADED':
            path = self.store._path(task['local_path'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != task['sha256']:
                raise MediaError('BLOB_INTEGRITY')
        elif task['error_code'] == 'NO_RIGHTS':
            task.update(state='QUEUED', attempts=0, error_code=None)
        task['updated_at'] = utc_now()
        # save_media 追加事件，原授权 context_hash 与证据仍保留在历史中。
        self.store.save_media(task, context)
        return task

    def preview_path(self, media_id, context):
        task = self.store.media_tasks()[media_id]
        self._authorize(task, context)
        self._rating(task)
        if task['state'] != 'DOWNLOADED':
            raise PermissionError('NO_PREVIEW')
        path = self.store._path(task['local_path'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != task['sha256']:
            raise MediaError('BLOB_INTEGRITY')
        return path

    def repair(self, media_id, context, url, rating, evidence, expires_at, allow_r18=False):
        task = self.store.media_tasks()[media_id]
        if task['state'] == 'DOWNLOADED':
            raise ValueError('ALREADY_DOWNLOADED')
        if context['source_id'] != task['source_id']:
            raise PermissionError('CONTEXT_MISMATCH')
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError('REAUTHORIZATION_EVIDENCE_REQUIRED')
        task.update({'source_url': url, 'content_rating': rating, 'rating_evidence': evidence,
                     'expires_at': expires_at, 'allow_r18': allow_r18,
                     'context_hash': digest(context), 'authorization_evidence_uri': evidence.strip()})
        self._authorize(task, context)
        self._rating(task)
        task.update({'state': 'QUEUED', 'attempts': 0, 'error_code': None, 'updated_at': utc_now()})
        self.store.save_media(task, context)

    def may_redistribute(self, media_id, context):
        task = self.store.media_tasks()[media_id]
        self._authorize(task, context)
        self.gate.require(task['source_id'], 'redistribute', context['purpose'],
                          context['policy_id'], context['policy_version'])
        return True
