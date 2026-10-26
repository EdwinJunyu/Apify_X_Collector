if __name__ == '__main__':
    import argparse
    import base64
    import json
    from pathlib import Path
    from collector.deletion import DeletionManager
    from collector.media import MediaManager
    from collector.media_http import FakeMediaHTTP
    from collector.policy import PolicyGate
    from collector.storage import StorageEngine

    parser = argparse.ArgumentParser(description='M4 offline media + deletion demo')
    parser.add_argument('--root', default='media-demo-data')
    args = parser.parse_args()
    fixture = json.loads((Path(__file__).parent / 'fixtures/offline.json').read_bytes())
    for policy in fixture['policies']:
        if policy['operation'] != 'redistribute':
            policy['permission_state'] = 'PERMITTED'
            policy['content_scope'] = ['METADATA', 'TEXT', 'MEDIA']
    gate = PolicyGate(fixture['policies'])
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=')
    with StorageEngine(args.root) as store:
        post = fixture['post']
        # Deletion is permanent for each ID: each demo run uses a new synthetic ID.
        import time
        post['post_id'] = str(time.time_ns())
        post['source_url'] = 'https://x.com/mock_artist/status/' + post['post_id']
        store.ingest([post], fixture['account'], gate, fixture['context'])
        media = MediaManager(store, gate, FakeMediaHTTP(png))
        mid = media.enqueue(post['post_id'], 'https://pbs.twimg.com/media/mock.png', fixture['context'],
                            'SFW', 'mock://manual-rating', '2090-01-01T00:00:00Z')
        print('Download:', media.process(mid, fixture['context'])['state'])
        print('Preview verified:', media.preview_path(mid, fixture['context']).is_file())
        report = DeletionManager(store).purge(post['post_id'], fixture['context'])
        print('Cleanup:', json.dumps(report, ensure_ascii=False))
        print('Remaining current posts:', len(store.current_posts()))
