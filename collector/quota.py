"""Root-wide and volume-free-space checks, including temporary write overhead."""
import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

GiB = 1024 ** 3


class NoSpace(RuntimeError):
    pass


class Quota:
    def __init__(self, root, limit=50 * GiB, min_free=10 * GiB, free_probe=None, clock=None):
        if type(limit) is not int or limit <= 0 or type(min_free) is not int or min_free < 10 * GiB:
            raise ValueError('INVALID_QUOTA')
        self.root = Path(root)
        self.limit = limit
        self.min_free = min_free
        self.free_probe = free_probe or (lambda: shutil.disk_usage(self.root).free)
        self.clock = clock or time.monotonic
        self._cached_used = None
        self._checked_at = None
        self.reconcile_seconds = 5.0

    def invalidate(self):
        self._cached_used = None
        self._checked_at = None

    def used(self):
        total = 0
        for directory, folders, files in os.walk(self.root, followlinks=False):
            for name in folders + files:
                path = Path(directory) / name
                if path.is_symlink():
                    raise NoSpace('UNTRUSTED_SYMLINK_IN_ROOT')
            for name in files:
                total += (Path(directory) / name).stat().st_size
        self._cached_used, self._checked_at = total, self.clock()
        return total

    def require(self, additional, essential=False):
        if type(additional) is not int or additional < 0:
            raise ValueError('INVALID_ALLOCATION')
        self.invalidate()  # 写入将改变容量；UI 缓存永远不能参与写入授权。
        free = self.free_probe()
        # Essential metadata/erasure writes may use 8 MiB of the safety reserve.
        reserve = self.min_free - (8 * 1024 ** 2 if essential else 0)
        if free - additional < reserve or (not essential and self.used() + additional > self.limit):
            raise NoSpace('NO_SPACE')
        self.invalidate()

    @contextmanager
    def transfer(self, path):
        # 单写者同步下载期间仅有这个 .part 增长。先全量核对，再按累计预留计算。
        path = Path(path)
        baseline = self.used()
        reserved = 0
        checked_at = self.clock()
        def reserve_chunk(additional, overhead=65536):
            nonlocal baseline, reserved, checked_at
            if type(additional) is not int or additional < 0:
                raise ValueError('INVALID_ALLOCATION')
            if self.clock() - checked_at >= self.reconcile_seconds:
                # 去除磁盘上已落盘的 .part 后加累计预留，缓冲区尚未落盘也不会漏计。
                baseline = self.used() - (path.stat().st_size if path.exists() else 0)
                checked_at = self.clock()
            if (baseline + reserved + additional + overhead > self.limit or
                    self.free_probe() - additional - overhead < self.min_free):
                raise NoSpace('NO_SPACE')
            reserved += additional
        try:
            yield reserve_chunk
        finally:
            self.invalidate()  # 成功、失败、去重、清理部分文件后都重新核对。

    def status(self):
        if self._cached_used is None or self.clock() - self._checked_at >= self.reconcile_seconds:
            self.used()
        used = self._cached_used
        return {'used_bytes': used, 'limit_bytes': self.limit,
                'warning': used >= self.limit * 0.8, 'free_bytes': self.free_probe(),
                'usage_age_seconds': max(0, self.clock() - self._checked_at)}
