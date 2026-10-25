if __name__ == '__main__':
    import argparse
    import json
    from pathlib import Path
    from collector.adapter import StandardFixtureAdapter
    from collector.apify import FakeTransport
    from collector.policy import PolicyGate
    from collector.storage import StorageEngine
    from collector.sync import SyncCoordinator

    parser = argparse.ArgumentParser(description='M2–M3 offline sync demo (no API token)')
    parser.add_argument('--root', default='sync-demo-data')
    args = parser.parse_args()
    fixture = json.loads((Path(__file__).parent / 'fixtures/offline.json').read_bytes())
    fixture['policies'][0]['permission_state'] = 'PERMITTED'
    with StorageEngine(args.root) as store:
        sync = SyncCoordinator(store, FakeTransport([fixture['post']]), StandardFixtureAdapter(),
                               PolicyGate(fixture['policies']))
        pending = None
        for job in store.jobs().values():
            if job['state'] not in ('DONE', 'FAILED', 'NEEDS_REVIEW'):
                pending = job['job_id']
        jid = pending or sync.start(fixture['account'], fixture['context'], now='2026-10-25T00:00:00Z')
        for _ in range(20):
            job = sync.advance(jid, fixture['account'], fixture['context'])
            if job['state'] in ('DONE', 'FAILED', 'NEEDS_REVIEW'):
                break
        print(json.dumps(sync.coverage_report(), ensure_ascii=False, indent=2))
        print('Current posts:', store.status()['posts'])
