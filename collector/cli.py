"""Internal offline diagnostics; the desktop GUI arrives in M5."""
import argparse
import json
from pathlib import Path
from .config import load_config
from .policy import PolicyGate
from .storage import CorruptStore, StorageEngine
from .validation import strict_loads


def main():
    parser = argparse.ArgumentParser(description='ACG Collector M0–M1（仅离线；无收费网络操作）')
    parser.add_argument('command', choices=('demo', 'status', 'recover'))
    parser.add_argument('--root', type=Path)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'config.example.json')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        root = args.root or (args.config.resolve().parent / config['data_root'])
        with StorageEngine(root) as store:
            if args.command == 'demo':
                fixture = strict_loads((Path(__file__).resolve().parents[1] / 'fixtures' / 'offline.json').read_bytes())
                result = store.ingest([fixture['post']], fixture['account'],
                                      PolicyGate(fixture['policies']), fixture['context'])
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(json.dumps(store.status(), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        # Error category only; never echo raw records, paths, or credentials.
        print('操作失败：' + type(exc).__name__ + '。请关闭写入进程并检查数据完整性。')
        return 2
