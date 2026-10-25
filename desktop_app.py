if __name__ == '__main__':
    import argparse
    import os
    import sys
    from pathlib import Path
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from desktop.ipc import Client, launch_worker
    from desktop.gui import MainWindow
    parser = argparse.ArgumentParser(description='ACG Collector 桌面管理台')
    parser.add_argument('--root', default=str(Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'ACGCollectorData'))
    parser.add_argument('--private-base')
    args = parser.parse_args()
    app = QApplication(sys.argv)
    client = Client(args.root, args.private_base, timeout=65000)
    # Startup probe only; normal commands run in the GUI thread pool.
    try:
        snapshot = Client(args.root, args.private_base, timeout=500).call('snapshot')
        from collector import __version__
        if snapshot.get('version') != __version__:
            # Upgrades never attach the new GUI to an older running writer.
            Client(args.root, args.private_base, timeout=65000).call('stop_worker')
            import time
            from desktop.ipc import endpoint
            deadline = time.monotonic() + 10
            while endpoint(args.root, args.private_base).exists() and time.monotonic() < deadline:
                time.sleep(.05)
            if endpoint(args.root, args.private_base).exists():
                raise RuntimeError('OLDER_WORKER_STILL_STOPPING')
            launch_worker(args.root, args.private_base)
    except (FileNotFoundError, ConnectionError, TimeoutError):
        launch_worker(args.root, args.private_base)
    window = MainWindow(client)
    window.show()
    QTimer.singleShot(1200, window.refresh_status)
    raise SystemExit(app.exec())
