"""Kernel-held process lock; stale PID metadata never grants ownership."""
import json
import os
import threading


class WriterBusy(RuntimeError):
    pass


class WriterLock:
    def __init__(self, root):
        self.root = root
        self.file = None
        self.pid = os.getpid()
        self.thread = threading.get_ident()

    def acquire(self):
        for name in ('writer.lock', 'writer-owner.json'):
            if (self.root / 'journal' / name).is_symlink():
                raise WriterBusy('SYMLINK_LOCK_FILE')
        self.file = (self.root / 'journal' / 'writer.lock').open('a+b')
        try:
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                if self.file.read(1) == b'':
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise WriterBusy('WRITER_ALREADY_RUNNING') from None
        try:
            owner = {'pid': self.pid, 'thread_id': self.thread}
            (self.root / 'journal' / 'writer-owner.json').write_text(json.dumps(owner), encoding='utf-8')
        except BaseException:
            self.close()
            raise

    def require(self):
        if self.file is None or self.pid != os.getpid() or self.thread != threading.get_ident():
            raise WriterBusy('WRITER_NOT_OWNED')

    def close(self):
        if self.file is not None:
            if self.pid != os.getpid():
                self.file.close()
                self.file = None
                return
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None
