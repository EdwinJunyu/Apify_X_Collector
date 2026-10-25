if __name__ == '__main__':
    import argparse
    import sys
    from PySide6.QtCore import QCoreApplication
    from desktop.ipc import WorkerServer
    parser = argparse.ArgumentParser(description='ACG Collector independent Worker')
    parser.add_argument('--root', required=True)
    parser.add_argument('--private-base')
    parser.add_argument('--scheduled', action='store_true')
    args = parser.parse_args()
    app = QCoreApplication(sys.argv)
    try:
        server = WorkerServer(args.root, args.private_base)
    except Exception as exc:
        from collector.locking import WriterBusy
        if isinstance(exc, WriterBusy):
            raise SystemExit(0)  # existing Worker already owns scheduling and recovery
        print('Worker 启动失败：' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2)
    app.aboutToQuit.connect(server.close)
    raise SystemExit(app.exec())
