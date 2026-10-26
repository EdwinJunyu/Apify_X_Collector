if __name__ == '__main__':
    import json
    import os
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from collector.policy import PolicyGate
    from collector.storage import StorageEngine

    fixture = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/offline.json').read_bytes())
    fixture['post']['text'] = 'hard-crash-revision'

    def crash(stage):
        if stage == sys.argv[2]:
            os._exit(37)

    with StorageEngine(sys.argv[1], fault=crash) as store:
        checkpoint = {'account_id': fixture['account']['account_id'], 'previous_cursor': None,
                      'requested_window': 'offline', 'overlap_start': '2026-10-22T00:00:00Z',
                      'candidate_cursor': fixture['post']['post_id'], 'committed_cursor': fixture['post']['post_id'],
                      'coverage_status': 'UNKNOWN', 'coverage_reason': 'synthetic',
                      'last_success_at': '2026-10-24T13:00:00Z'}
        store.ingest([fixture['post']], fixture['account'], PolicyGate(fixture['policies']),
                     fixture['context'], checkpoint)
