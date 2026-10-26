"""Private filesystem boundaries and exact current-user ACLs."""
import csv
import ctypes
import io
import os
import re
import subprocess
from pathlib import Path


def checked_path(value):
    path = Path(value).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            raise PermissionError('LINK_OR_JUNCTION_REFUSED')
        if os.name == 'nt' and part.exists():
            from ctypes import wintypes
            attributes = ctypes.windll.kernel32.GetFileAttributesW
            attributes.argtypes = (wintypes.LPCWSTR,)
            attributes.restype = wintypes.DWORD
            attrs = attributes(str(part))
            if attrs != 0xFFFFFFFF and attrs & 0x400:
                raise PermissionError('REPARSE_POINT_REFUSED')
    return path.resolve()


def current_sid():
    if os.name != 'nt':
        return str(os.getuid())
    result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True,
                            text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    sid = list(csv.reader(io.StringIO(result.stdout)))[0][-1]
    if not re.fullmatch(r'S-1-[0-9-]+', sid):
        raise PermissionError('INVALID_USER_SID')
    return sid


def secure_path(path, directory=False):
    path = checked_path(path)
    if directory:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        os.chmod(path, 0o700 if directory else 0o600)
        return
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    descriptor = ctypes.c_void_p()
    sddl = 'D:P(A;' + ('OICI' if directory else '') + ';FA;;;' + current_sid() + ')'
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    convert.restype = wintypes.BOOL
    setter = advapi.SetFileSecurityW
    setter.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
    setter.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not setter(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def physical_disks(path):
    path = checked_path(path)
    while not path.exists():
        path = path.parent
    if os.name == 'nt':
        return windows_physical_disks(path)
    device = path.stat().st_dev
    link = Path('/sys/dev/block') / (str(os.major(device)) + ':' + str(os.minor(device)))
    if not link.exists():
        raise RuntimeError('UNKNOWN_PHYSICAL_DISK')
    block = link.resolve()
    if (block / 'partition').exists():
        block = block.parent
    # dm/md/multi-device volumes cannot safely be identified by one st_dev.
    if (block / 'slaves').exists() and list((block / 'slaves').iterdir()):
        raise RuntimeError('COMPOSITE_DISK_UNSUPPORTED')
    if block.name.startswith(('loop', 'dm-', 'md')):
        raise RuntimeError('UNKNOWN_PHYSICAL_DISK')
    return {'linux:' + block.name}


def parse_disk_extents(data):
    import struct
    if len(data) < 8:
        raise RuntimeError('INVALID_VOLUME_EXTENTS')
    count = struct.unpack_from('<I', data, 0)[0]
    if not 1 <= count <= 16 or len(data) < 8 + count * 24:
        raise RuntimeError('INVALID_VOLUME_EXTENTS')
    disks = set()
    for index in range(count):
        disks.add(struct.unpack_from('<I', data, 8 + index * 24)[0])
    return disks


def windows_physical_disks(path):
    # FILE_ANY_ACCESS IOCTLs; never requests administrator privileges or write access.
    import struct
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    volume_path = kernel.GetVolumePathNameW
    volume_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    volume_path.restype = wintypes.BOOL
    volume_name = kernel.GetVolumeNameForVolumeMountPointW
    volume_name.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    volume_name.restype = wintypes.BOOL
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    control = kernel.DeviceIoControl
    control.argtypes = (wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p)
    control.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    def query(device, code, payload=b''):
        handle = create(device,0,7,None,3,0,None)
        if handle == ctypes.c_void_p(-1).value:
            raise RuntimeError('PHYSICAL_DISK_QUERY_DENIED')
        try:
            output = ctypes.create_string_buffer(4096)
            input_buffer = ctypes.create_string_buffer(payload) if payload else None
            returned = wintypes.DWORD()
            if not control(handle,code,input_buffer,len(payload),output,len(output),ctypes.byref(returned),None):
                raise RuntimeError('PHYSICAL_DISK_QUERY_FAILED')
            return output.raw[:returned.value]
        finally:
            close(handle)
    mount = ctypes.create_unicode_buffer(32768)
    name = ctypes.create_unicode_buffer(32768)
    if not volume_path(str(path),mount,len(mount)) or not volume_name(mount.value,name,len(name)):
        raise RuntimeError('UNKNOWN_VOLUME_MAPPING')
    disks = parse_disk_extents(query(name.value.rstrip('\\'),0x00560000))
    result = set()
    for disk in disks:
        descriptor = query('\\\\.\\PhysicalDrive' + str(disk),0x002D1400,b'\x00'*12)
        if len(descriptor) < 36:
            raise RuntimeError('UNKNOWN_PHYSICAL_DISK')
        bus_type = struct.unpack_from('<I',descriptor,28)[0]
        if bus_type in (0,9,14,15,16):
            raise RuntimeError('VIRTUAL_OR_UNKNOWN_DISK_REFUSED')
        result.add('windows-physical:' + str(disk))
    return result
