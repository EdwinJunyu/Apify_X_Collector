if __name__ == '__main__':
    import argparse,tempfile,json
    from pathlib import Path
    from collector.backup import BackupManager
    from collector.storage import StorageEngine,atomic_bytes
    from collector.security import checked_path
    from collector.validation import strict_loads,canonical
    parser=argparse.ArgumentParser(description='Recover from an owned vault after loss of the original active disk')
    parser.add_argument('--vault',required=True)
    parser.add_argument('--snapshot',required=True)
    parser.add_argument('--target',required=True)
    parser.add_argument('--confirm-store-id',required=True)
    args=parser.parse_args()
    vault=checked_path(args.vault)
    marker=strict_loads((vault/'.acg-vault.json').read_bytes())
    if marker['store_id']!=args.confirm_store_id:raise ValueError('EXPLICIT_VAULT_IDENTITY_CONFIRMATION_REQUIRED')
    with tempfile.TemporaryDirectory(prefix='acg-recovery-') as directory:
        with StorageEngine(Path(directory)/'control') as store:
            atomic_bytes(store._path('deletions/store-identity.json'),canonical({'store_id':marker['store_id']}))
            manager=BackupManager(store)
            result=manager.restore({'path':str(vault),'store_id':marker['store_id'],'vault_id':marker['vault_id']},args.snapshot,args.target)
            print(json.dumps(result,ensure_ascii=False,indent=2))
