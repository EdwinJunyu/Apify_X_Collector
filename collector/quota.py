"""Root-wide and volume-free-space checks, including temporary write overhead."""
import os
import shutil
from pathlib import Path

GiB = 1024 ** 3


class NoSpace(RuntimeError):
    pass


class Quota:
    def __init__(self, root, limit=50 * GiB, min_free=10 * GiB, free_probe=None):
        if type(limit) is not int or limit <= 0 or type(min_free) is not int or min_free < 10 * GiB:
            raise ValueError('INVALID_QUOTA')
        self.root = Path(root)
        self.limit = limit
        self.min_free = min_free
        self.free_probe = free_probe or (lambda: shutil.disk_usage(self.root).free)

    def used(self):
        total = 0
        for directory, folders, files in os.walk(self.root, followlinks=False):
            for name in folders + files:
                path = Path(directory) / name
                if path.is_symlink():
                    raise NoSpace('UNTRUSTED_SYMLINK_IN_ROOT')
            for name in files:
                total += (Path(directory) / name).stat().st_size
        return total

    def require(self, additional, essential=False):
        if type(additional) is not int or additional < 0:
            raise ValueError('INVALID_ALLOCATION')
        free = self.free_probe()
        # Essential metadata/erasure writes may use 8 MiB of the safety reserve.
        reserve = self.min_free - (8 * 1024 ** 2 if essential else 0)
        if free - additional < reserve or (not essential and self.used() + additional > self.limit):
            raise NoSpace('NO_SPACE')

    def status(self):
        used = self.used()
        return {'used_bytes': used, 'limit_bytes': self.limit,
                'warning': used >= self.limit * 0.8, 'free_bytes': self.free_probe()}
