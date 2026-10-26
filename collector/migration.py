"""Read-only v0.1 migration and formula-safe, permission-filtered CSV views."""
import csv
import hashlib
import io
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from .storage import atomic_bytes, prepare_post
from .validation import utc_now


def csv_cell(value):
    text = '' if value is None else str(value)
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


def export_posts(service):
    stream = io.StringIO(newline='')
    fields = ('post_id', 'account_id', 'published_at', 'post_type', 'source_url', 'content_rating', 'review_status', 'rights_status')
    writer = csv.writer(stream)
    writer.writerow(fields)
    subjects = []
    for post in service.posts():
        if post['rights_status'] != 'PERMITTED':
            continue
        row = []
        for field in fields:
            row.append(csv_cell(post.get(field)))
        writer.writerow(row)
        subjects.append(post['post_id'])
    data = stream.getvalue().encode('utf-8-sig')
    service.store.quota.require(len(data) + 65536)
    relative = 'exports/review-' + uuid.uuid4().hex + '.csv'
    path = service.store._path(relative)
    atomic_bytes(path, data)
    from .deletion import DeletionManager
    DeletionManager(service.store).track_artifact(subjects, relative)
    return {'path': str(path), 'rows': len(subjects)}


def import_legacy(store, source, account, gate, context, execute=False):
    source = Path(source).resolve()
    if not source.is_file() or source.stat().st_size > 1024 ** 3:
        raise ValueError('LEGACY_FILE_LIMIT')
    from .backup import sha_file
    fingerprint = sha_file(source)
    for old in store.admin_events('LEGACY_IMPORT'):
        if old['source_sha256'] == fingerprint and old['account_id'] == account['account_id']:
            return {'already_imported': True, 'accepted': 0, 'quarantined': 0}
    if context['source_id'] != 'x:' + str(account['external_user_id']) or context['policy_id'] != account['source_policy_id']:
        raise PermissionError('LEGACY_ACCOUNT_POLICY_BINDING')
    policy = gate.require(context['source_id'], 'retain', context['purpose'], context['policy_id'], context['policy_version'])
    if 'METADATA' not in policy['content_scope']:
        raise PermissionError('LEGACY_METADATA_RETENTION_DENIED')
    uri = 'file:' + quote(str(source), safe='/:') + '?mode=ro&immutable=1'
    accepted, quarantined, batch = 0, 0, []
    def flush():
        if execute and batch:
            store.ingest(batch, account, gate, context)
        batch.clear()
    # sqlite 的事务上下文不会关闭连接；只读导入结束后也要释放 Windows 文件句柄。
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute('PRAGMA trusted_schema=OFF')
        if connection.execute("SELECT type FROM sqlite_master WHERE name='staged_posts'").fetchone() != ('table',):
            raise ValueError('LEGACY_TABLE_REQUIRED')
        cursor = connection.execute('SELECT id, account, published_at, url, post_kind, lang, full_text FROM staged_posts')
        while True:
            rows = cursor.fetchmany(100)
            if not rows:
                break
            for pid, handle, published, url, kind, language, text in rows:
                if accepted + quarantined >= 100000:
                    raise ValueError('LEGACY_RECORD_LIMIT')
                try:
                    stamp = datetime.fromisoformat(published.replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        raise ValueError('LEGACY_NAIVE_TIME')
                    row = {'platform': 'X', 'post_id': pid, 'account_id': account['account_id'],
                        'author_external_id': account['external_user_id'], 'author_handle_at_observation': handle,
                        'published_at': stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
                        'source_url': url, 'post_type': str(kind).upper(), 'quoted_post_id': None,
                        'in_reply_to_id': None, 'reposted_post_id': None, 'language': language,
                        'text': text if 'TEXT' in policy['content_scope'] else None, 'media_refs': [], 'metrics': {},
                        'hashtags': [], 'mentions': [], 'content_rating': 'UNKNOWN', 'coverage_evidence_id': None, 'raw_ref': None}
                    prepare_post(row, account)
                    batch.append(row)
                    accepted += 1
                except (ValueError, TypeError, KeyError):
                    batch.append({})
                    quarantined += 1
            flush()
    report = {'source_sha256': fingerprint, 'account_id': account['account_id'],
              'accepted': accepted, 'quarantined': quarantined, 'imported_at': utc_now()}
    if execute:
        store.save_admin('LEGACY_IMPORT', report, context)
    return dict(report, dry_run=not execute)
