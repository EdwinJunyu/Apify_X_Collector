if __name__ == '__main__':
    import argparse,json,os
    from pathlib import Path
    from collector.release import build_manifest,verify_release,install,activate
    parser=argparse.ArgumentParser(description='Per-user install/upgrade/rollback; data is never overwritten')
    parser.add_argument('command',choices=('verify','manifest','install','activate'))
    parser.add_argument('--source',default=str(Path(__file__).parent))
    parser.add_argument('--installation',default=str(Path(os.environ.get('LOCALAPPDATA') or Path.home())/'ACGCollectorApp'))
    parser.add_argument('--version')
    parser.add_argument('--data-root',default=str(Path(os.environ.get('LOCALAPPDATA') or Path.home())/'ACGCollectorData'))
    args=parser.parse_args()
    if args.command=='verify':result=verify_release(args.source)
    elif args.command=='manifest':result=build_manifest(args.source)
    elif args.command=='install':result=install(args.source,args.installation)
    else:result=activate(args.installation,args.version,args.data_root)
    print(json.dumps(result,ensure_ascii=False,indent=2))
