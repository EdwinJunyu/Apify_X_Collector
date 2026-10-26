if __name__ == '__main__':
    import argparse,json,os,subprocess,sys
    from pathlib import Path
    from collector.release import verify_release
    parser=argparse.ArgumentParser(description='Launch current per-user installed release')
    parser.add_argument('--installation',default=str(Path(os.environ.get('LOCALAPPDATA') or Path.home())/'ACGCollectorApp'))
    parser.add_argument('--root',default=str(Path(os.environ.get('LOCALAPPDATA') or Path.home())/'ACGCollectorData'))
    args=parser.parse_args()
    base=Path(args.installation).resolve();pointer=json.loads((base/'current.json').read_text())
    import re
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',pointer['version']):raise ValueError('INVALID_INSTALLED_VERSION')
    target=base/'versions'/pointer['version'];verify_release(target)
    python=target/'.venv'/('Scripts/pythonw.exe' if os.name=='nt' else 'bin/python')
    subprocess.Popen([str(python),str(target/'desktop_app.py'),'--root',args.root],cwd=target)
