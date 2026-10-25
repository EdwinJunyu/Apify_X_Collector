if __name__ == '__main__':
    import json
    from pathlib import Path

    root = Path(__file__).parent
    destination = root / 'collector' / 'schemas'
    destination.mkdir(parents=True, exist_ok=True)

    def obj(fields, required=None):
        names = []
        for key in fields:
            names.append(key)
        return {'type': 'object', 'properties': fields,
                'required': names if required is None else required,
                'additionalProperties': False}

    def string(pattern=None, nullable=False, maximum=512):
        value = {'type': ['string', 'null'] if nullable else 'string', 'maxLength': maximum}
        if not nullable:
            value['minLength'] = 1
        if pattern:
            value['pattern'] = pattern
        return value

    def choices(*values):
        return {'type': 'string', 'enum': list(values)}

    def array(items, limit=1000):
        return {'type': 'array', 'items': items, 'maxItems': limit}

    identifier = string(r'^[a-f0-9]{32}$')
    sha = string(r'^[a-f0-9]{64}$')
    timestamp = {'type': 'string', 'format': 'date-time'}
    maybe_time = {'type': ['string', 'null'], 'format': 'date-time'}
    xid = string(r'^[0-9]{1,32}$')
    maybe_xid = string(r'^[0-9]{1,32}$', True)
    handle = string(r'^[A-Za-z0-9_]{1,15}$')
    positive = {'type': 'integer', 'minimum': 1}
    nonnegative = {'type': 'integer', 'minimum': 0}
    rating = choices('SFW', 'R18', 'UNKNOWN')
    version = {'type': 'string', 'const': '1.0'}
    platform = {'type': 'string', 'const': 'X'}
    operation = choices('access', 'retain', 'download_media', 'redistribute')
    rights = choices('PERMITTED', 'DENIED', 'UNKNOWN', 'REVOKED')
    schemas = {}
    schemas['context'] = obj({'source_id': string(r'^x:[0-9]{1,32}$'),
                              'policy_id': string(), 'policy_version': positive,
                              'purpose': string(), 'actor_run_id': string(nullable=True)},
                             ['source_id', 'policy_id', 'policy_version', 'purpose'])
    schemas['account'] = obj({
        'account_id': string(), 'entity_id': string(nullable=True), 'platform': platform,
        'external_user_id': maybe_xid, 'current_handle': handle, 'handle_history': array(handle),
        'verified_source_urls': array(string()),
        'verification_status': choices('VERIFIED', 'PENDING', 'REJECTED'),
        'account_content_rating': rating, 'source_policy_id': string(nullable=True),
        'observed_at': timestamp, 'active_status': choices('ACTIVE', 'PAUSED', 'DISABLED')})
    schemas['policy'] = obj({
        'policy_id': string(), 'version': positive, 'source_id': string(),
        'operation': operation, 'permission_state': rights, 'purpose': string(),
        'evidence_uri': string(), 'reviewed_at': timestamp, 'reviewer': string(),
        'valid_until': timestamp, 'retention_rule': string(), 'display_rule': string(),
        'deletion_rule': string(), 'content_scope': array(choices('METADATA', 'TEXT', 'RAW', 'MEDIA'))})
    schemas['post'] = obj({
        'platform': platform, 'post_id': xid, 'account_id': string(), 'author_external_id': xid,
        'author_handle_at_observation': handle,
        'post_type': choices('ORIGINAL', 'QUOTE', 'REPLY', 'REPOST', 'UNKNOWN'),
        'quoted_post_id': maybe_xid, 'in_reply_to_id': maybe_xid, 'reposted_post_id': maybe_xid,
        'published_at': timestamp, 'first_seen_at': timestamp, 'observed_at': timestamp,
        'indexed_at': timestamp, 'source_url': string(maximum=2048),
        'language': string(nullable=True), 'text': string(nullable=True, maximum=100000),
        'media_refs': array(string()),
        'metrics': {'type': 'object', 'additionalProperties': nonnegative},
        'hashtags': array(string()), 'mentions': array(string()),
        'version_number': positive, 'content_hash': sha, 'is_current': {'const': True, 'type': 'boolean'},
        'content_rating': rating, 'review_status': {'const': 'STAGING', 'type': 'string'},
        'rights_status': {'const': 'PERMITTED', 'type': 'string'},
        'coverage_evidence_id': string(nullable=True), 'raw_ref': {'type': 'null'},
        'deletion_state': {'const': 'ACTIVE', 'type': 'string'}})
    schemas['checkpoint'] = obj({
        'account_id': string(), 'previous_cursor': maybe_xid, 'requested_window': string(),
        'overlap_start': timestamp, 'candidate_cursor': maybe_xid, 'committed_cursor': maybe_xid,
        'coverage_status': choices('PARTIAL', 'UNKNOWN'),
        'coverage_reason': string(), 'last_success_at': timestamp})
    schemas['quarantine'] = obj({'reason': choices('INVALID_POST_OR_TOMBSTONED', 'TEXT_RETENTION_DENIED',
        'TOMBSTONED', 'POST_IDENTITY_CONFLICT', 'FIELD_TYPE', 'FIELD_CONST', 'FIELD_ENUM',
        'MISSING_FIELD', 'UNKNOWN_FIELD', 'ARRAY_LIMIT', 'STRING_LIMIT', 'FIELD_PATTERN',
        'NUMBER_LIMIT', 'UTC_TIMESTAMP_REQUIRED', 'INVALID_DATE', 'SOURCE_URL_MISMATCH',
        'MISSING_RELATION_ID', 'POST_OBJECT_REQUIRED', 'AUTHOR_MISMATCH'),
                                 'item_index': nonnegative, 'quarantine_id': identifier})
    schemas['tombstone'] = obj({
        'platform': platform, 'subject_type': {'const': 'POST', 'type': 'string'},
        'subject_id': xid, 'request_reason': choices('WITHDRAWN', 'POLICY_REVOKED', 'AUTHOR_REQUEST', 'INVALID_SOURCE'),
        'requested_at': timestamp, 'cleanup_scope': array(choices('current_index')),
        'cleaned_at': {'type': 'null'},
        'verification_status': {'const': 'PENDING_PHYSICAL_CLEANUP', 'type': 'string'}})
    schemas['pointer'] = obj({'schema_version': version, 'commit_id': identifier, 'manifest_hash': sha})
    previous = dict(schemas['pointer'])
    previous['type'] = ['object', 'null']
    schemas['manifest'] = obj({'schema_version': version, 'commit_id': identifier,
                               'previous': previous, 'segment_hash': sha,
                               'segment_bytes': positive, 'event_count': positive})
    schemas['event'] = obj({
        'schema_version': version, 'event_id': identifier,
        'event_type': choices('POST_UPSERT', 'QUARANTINE', 'TOMBSTONE', 'CHECKPOINT', 'JOB_STATE', 'MEDIA_STATE', 'ACCOUNT_STATE', 'POLICY_STATE', 'REVIEW_STATE', 'SETTINGS_STATE', 'OPERATION_LOG', 'SCHEDULE_STATE', 'LEGACY_IMPORT', 'SCALE_APPROVAL'),
        'recorded_at': timestamp, 'commit_id': identifier, 'source_id': string(r'^x:[0-9]{1,32}$'),
        'actor_run_id': string(nullable=True), 'rights_policy_id': string(),
        'rights_policy_version': positive, 'payload': {'type': 'object'}, 'payload_hash': sha,
        'retention_class': {'const': 'policy-bound', 'type': 'string'},
        'redaction_state': choices('NONE', 'SANITIZED')})
    # Contract-only records for later milestones; StorageEngine cannot commit these yet.
    schemas['media'] = obj({
        'media_id': string(), 'post_id': xid, 'media_kind': choices('IMAGE', 'VIDEO', 'UNKNOWN'),
        'source_url': string(), 'rights_status': rights, 'requested_at': timestamp,
        'download_state': choices('QUEUED', 'DOWNLOADED', 'FAILED', 'BLOCKED'),
        'sha256': string(r'^[a-f0-9]{64}$', True), 'mime_detected': string(nullable=True),
        'file_size': nonnegative, 'local_path': string(nullable=True),
        'error_code': string(nullable=True), 'retry_count': nonnegative, 'last_checked_at': timestamp})
    schemas['run'] = obj({
        'job_id': string(), 'actor_id': string(), 'actor_build': string(nullable=True),
        'input_fingerprint': sha, 'run_id': string(nullable=True), 'dataset_id': string(nullable=True),
        'requested_window': string(), 'run_status': choices('INTENT', 'RUNNING', 'SUCCEEDED', 'FAILED', 'REMOTE_OUTCOME_UNKNOWN'),
        'began_at': timestamp, 'completed_at': maybe_time, 'downloaded_offset': nonnegative,
        'final_dataset_count': nonnegative, 'cost_status': choices('UNKNOWN', 'RESERVED', 'FINALIZED')})
    money = string(r'^[0-9]+\.[0-9]{2,6}$')
    schemas['budget'] = obj({
        'accounting_scope_id': string(), 'job_id': string(), 'run_id': string(nullable=True),
        'event_type': choices('RESERVE', 'FINALIZE', 'REVERSE', 'APPROVE'),
        'reserved_usd': money, 'finalized_usd': money, 'currency': {'const': 'USD', 'type': 'string'},
        'source': string(), 'finalized_at': maybe_time, 'approved_by': string(nullable=True)})
    schemas['review'] = obj({
        'post_id': xid, 'reviewer_id': string(), 'action': choices('REVIEW', 'REJECT', 'MARK_PUBLISHABLE'),
        'category': rating, 'source_evidence': string(), 'notes': string(nullable=True), 'reviewed_at': timestamp})
    schemas['config'] = obj({
        'schema_version': version, 'mode': {'const': 'OFFLINE', 'type': 'string'},
        'schedule_enabled': {'const': False, 'type': 'boolean'}, 'schedule_local_time': string(r'^([01][0-9]|2[0-3]):[0-5][0-9]$'),
        'timezone': string(), 'per_run_limit_usd': money, 'pilot_limit_usd': money,
        'active_limit_bytes': positive, 'min_free_bytes': positive, 'overlap_hours': positive,
        'data_root': string(), 'backup_root': string(nullable=True),
        'paid_actor_concurrency': {'const': 1, 'type': 'integer'}})
    schemas['job'] = obj({
        'job_id': identifier, 'account_id': string(), 'source_id': string(),
        'context_hash': sha, 'account_hash': sha, 'actor_id': string(), 'actor_build': string(),
        'input_fingerprint': sha, 'window_start': timestamp, 'window_end': timestamp,
        'previous_cursor': maybe_xid, 'run_id': string(nullable=True), 'dataset_id': string(nullable=True),
        'state': choices('RUN_INTENT', 'REMOTE_OUTCOME_UNKNOWN', 'RUNNING', 'READING',
                         'FAILED', 'NEEDS_REVIEW', 'DONE'),
        'reserved_usd': money, 'observed_usd': string(nullable=True), 'finalized_usd': string(nullable=True),
        'offset': nonnegative, 'total': {'type': ['integer', 'null'], 'minimum': 0},
        'candidate_cursor': maybe_xid, 'rejected': nonnegative, 'accepted': nonnegative,
        'oldest': maybe_time, 'newest': maybe_time, 'pages': nonnegative,
        'coverage_status': choices('UNKNOWN', 'PARTIAL'), 'coverage_reason': string(),
        'error_code': string(nullable=True), 'updated_at': timestamp,
        'run_evidence_uri': string(nullable=True), 'bill_evidence_uri': string(nullable=True)})
    schemas['media_task'] = obj({
        'media_id': identifier, 'post_id': xid, 'account_id': string(), 'source_id': string(),
        'context_hash': sha, 'source_url': string(nullable=True, maximum=4096),
        'content_rating': rating, 'rating_evidence': string(nullable=True),
        'restricted': {'type': 'boolean'}, 'allow_r18': {'type': 'boolean'},
        'expires_at': timestamp, 'state': choices('QUEUED', 'DOWNLOADING', 'DOWNLOADED', 'FAILED', 'BLOCKED'),
        'sha256': string(r'^[a-f0-9]{64}$', True), 'local_path': string(nullable=True),
        'mime': string(nullable=True), 'file_size': nonnegative, 'attempts': nonnegative,
        'error_code': string(nullable=True), 'updated_at': timestamp})
    schemas['review_action'] = obj({'post_id': xid, 'status': choices('REVIEWED', 'REJECTED', 'PUBLISHABLE'),
        'reviewer': string(), 'category': rating, 'notes': string(nullable=True, maximum=2048), 'evidence_uri': string(), 'reviewed_at': timestamp})
    schemas['desktop_settings'] = obj({'mode': choices('OFFLINE', 'LIVE'), 'paused': {'type': 'boolean'},
        'schedule_enabled': {'type': 'boolean'}, 'schedule_local_time': string(r'^([01][0-9]|2[0-3]):[0-5][0-9]$'),
        'timezone': string(), 'backup_root': string(nullable=True), 'actor_id': string(), 'actor_build': string(),
        'price_evidence_uri': string(nullable=True), 'price_valid_until': timestamp, 'price_minimum_usd': money,
        'cap_supported': {'type': 'boolean'}, 'per_run_usd': money, 'pilot_usd': money,
        'quota_bytes': positive, 'min_free_bytes': positive, 'max_file_bytes': positive, 'batch_bytes': positive,
        'overlap_hours': positive})
    schemas['operation_log'] = obj({'operation': string(), 'outcome': choices('SUCCESS', 'FAILED'),
        'error_code': string(nullable=True), 'at': timestamp})
    schemas['job']['properties']['command_id'] = string(nullable=True)
    schemas['schedule_state'] = obj({'schedule_key': string(nullable=True),
        'claimed_day': string(nullable=True), 'missed_days': nonnegative,
        'entries': array(obj({'account_id': string(), 'request_id': identifier,
            'status': choices('PENDING','STARTED','BLOCKED','DONE','FAILED'),
            'job_id': string(nullable=True), 'error_code': string(nullable=True)}), 150)})
    schemas['legacy_import'] = obj({'source_sha256': sha, 'account_id': string(),
        'accepted': nonnegative, 'quarantined': nonnegative, 'imported_at': timestamp})
    schemas['scale_approval'] = obj({'stage': {'type':'integer','enum':[20,50,150]}, 'reviewer':string(),
        'evidence_uri':string(), 'approved_at':timestamp})
    for name, schema in schemas.items():
        schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
        schema['$id'] = 'urn:acg:collector:' + name + ':1.0'
        schema['title'] = name + ' Schema 1.0'
        (destination / (name + '.json')).write_text(json.dumps(schema, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    fixture_dir = root / 'fixtures'
    fixture_dir.mkdir(exist_ok=True)
    account = {'account_id': 'mock-artist-1', 'entity_id': None, 'platform': 'X',
               'external_user_id': '101', 'current_handle': 'mock_artist', 'handle_history': [],
               'verified_source_urls': ['mock://fixture/identity'], 'verification_status': 'VERIFIED',
               'account_content_rating': 'UNKNOWN', 'source_policy_id': 'mock-retain',
               'observed_at': '2026-10-24T12:00:00Z', 'active_status': 'ACTIVE'}
    post = {'platform': 'X', 'post_id': '1234567890123456789', 'account_id': 'mock-artist-1',
            'author_external_id': '101', 'author_handle_at_observation': 'mock_artist',
            'post_type': 'ORIGINAL', 'quoted_post_id': None, 'in_reply_to_id': None, 'reposted_post_id': None,
            'published_at': '2026-10-24T12:00:00Z', 'source_url': 'https://x.com/mock_artist/status/1234567890123456789',
            'language': 'ja', 'text': '合成テスト：新作発表', 'media_refs': [], 'metrics': {},
            'hashtags': [], 'mentions': [], 'content_rating': 'UNKNOWN', 'coverage_evidence_id': None, 'raw_ref': None}
    policies = []
    for action in ('access', 'retain', 'download_media', 'redistribute'):
        policies.append({'policy_id': 'mock-retain', 'version': 1, 'source_id': 'x:101',
                         'operation': action, 'permission_state': 'PERMITTED' if action == 'retain' else 'UNKNOWN',
                         'purpose': 'offline-fixture', 'evidence_uri': 'mock://synthetic-owned-fixtures',
                         'reviewed_at': '2026-01-01T00:00:00Z', 'reviewer': 'fixture-author',
                         'valid_until': '2099-01-01T00:00:00Z', 'retention_rule': 'synthetic-test-only',
                         'display_rule': 'no-public-publish', 'deletion_rule': 'delete-test-root',
                         'content_scope': ['METADATA', 'TEXT'] if action == 'retain' else []})
    fixtures = {'account': account, 'post': post, 'policies': policies,
                'context': {'source_id': 'x:101', 'policy_id': 'mock-retain', 'policy_version': 1, 'purpose': 'offline-fixture'}}
    (fixture_dir / 'offline.json').write_text(json.dumps(fixtures, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    config = {'schema_version': '1.0', 'mode': 'OFFLINE', 'schedule_enabled': False,
              'schedule_local_time': '09:00', 'timezone': 'America/Vancouver',
              'per_run_limit_usd': '0.50', 'pilot_limit_usd': '10.00',
              'active_limit_bytes': 50 * 1024 ** 3, 'min_free_bytes': 10 * 1024 ** 3,
              'overlap_hours': 48, 'data_root': './data', 'backup_root': None, 'paid_actor_concurrency': 1}
    (root / 'config.example.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    print('Created', len(schemas), 'Schema 1.0 contracts and offline fixtures.')
