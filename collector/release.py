"""User-space versioned installer; atomic code pointer, never overwrites data."""
import hashlib
import os
import re
import shutil
import subprocess
import sys
import uuid
import venv
from pathlib import Path
from .security import checked_path, secure_path
from .storage import atomic_bytes, replace_file
from .validation import canonical, strict_loads, utc_now
from .backup import sha_file

VERSION = '0.0.9'
READER_LEVEL = 8
EXCLUDE = {'__pycache__', '.venv', 'data', 'output', 'node_modules', 'requests', 'responses', 'sync-demo-data', 'media-demo-data'}


def release_files(source):
    result = []
    source = checked_path(source)
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        skip = False
        for part in relative.parts:
            if part in EXCLUDE:
                skip = True
        if skip:
            continue
        checked_path(path)
        if not path.is_file():
            continue
        if relative.as_posix() in ('docs/TEST_RESULTS.txt', 'acceptance-report.json'):
            continue
        if path.name in ('worker.json', 'release-manifest.json') or path.suffix in ('.pyc', '.sqlite3', '.zip'):
            continue
        if path.name.startswith('.env') and path.name != '.env.example':
            raise PermissionError('SECRET_IN_RELEASE_TREE')
        result.append(relative.as_posix())
    return result


def build_manifest(source):
    source = Path(source).resolve()
    files = {}
    for relative in release_files(source):
        path = source / relative
        # Binary screenshots and code strings are not heuristically interpreted as credentials.
        if path.name == '.env.example' and path.read_text().split('APIFY_TOKEN=', 1)[-1].strip():
            raise PermissionError('NONEMPTY_ENV_EXAMPLE')
        files[relative] = {'bytes': path.stat().st_size, 'sha256': sha_file(path)}
    manifest = {'version': VERSION, 'reader_level': READER_LEVEL, 'created_at': utc_now(), 'files': files}
    atomic_bytes(source / 'release-manifest.json', canonical(manifest))
    return manifest


def verify_release(source):
    source = checked_path(source)
    manifest = strict_loads((source / 'release-manifest.json').read_bytes())
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', manifest['version']):
        raise ValueError('INVALID_RELEASE_VERSION')
    if set(release_files(source)) != set(manifest['files']):
        raise ValueError('RELEASE_FILE_SET_MISMATCH')
    for relative, info in manifest['files'].items():
        path = checked_path(source / relative)
        if not path.is_relative_to(source) or path.stat().st_size != info['bytes'] or sha_file(path) != info['sha256']:
            raise ValueError('RELEASE_HASH_MISMATCH')
    return manifest


def install(source, installation, with_dependencies=True):
    source, installation = checked_path(source), checked_path(installation)
    manifest = verify_release(source)
    secure_path(installation, directory=True)
    versions = installation / 'versions'
    secure_path(versions, directory=True)
    target = versions / manifest['version']
    if target.exists():
        raise ValueError('VERSION_ALREADY_INSTALLED')
    stage = versions / (manifest['version'] + '.staging-' + uuid.uuid4().hex)
    secure_path(stage, directory=True)
    total = sum(info['bytes'] for info in manifest['files'].values())
    if shutil.disk_usage(installation).free < total * 2 + 2 * 1024 ** 3:
        raise RuntimeError('INSTALL_NO_SPACE')
    for relative in manifest['files']:
        path = stage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, path)
    shutil.copy2(source / 'release-manifest.json', stage / 'release-manifest.json')
    verify_release(stage)
    if with_dependencies:
        venv.EnvBuilder(with_pip=True).create(stage / '.venv')
        python = stage / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        subprocess.run([str(python), '-m', 'pip', 'install', '--require-hashes', '-r', str(stage / 'requirements-desktop.lock')], check=True)
        subprocess.run([str(python), '-c', 'import PySide6, tzdata'], check=True)
    replace_file(stage, target)
    pointer = installation / 'current.json'
    previous = strict_loads(pointer.read_bytes())['version'] if pointer.exists() else None
    atomic_bytes(pointer, canonical({'version': manifest['version'], 'previous': previous, 'reader_level': manifest['reader_level']}))
    return {'version': manifest['version'], 'path': str(target), 'previous': previous}


def activate(installation, version, data_root):
    installation = checked_path(installation)
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('INVALID_RELEASE_VERSION')
    target = installation / 'versions' / version
    manifest = verify_release(target)
    floor_file = checked_path(data_root) / 'deletions/format-floor.json'
    floor = strict_loads(floor_file.read_bytes())['min_reader_level'] if floor_file.exists() else 8
    if manifest['reader_level'] < floor:
        raise ValueError('ROLLBACK_WOULD_IGNORE_DATA_OR_ERASURES')
    pointer = installation / 'current.json'
    old = strict_loads(pointer.read_bytes()) if pointer.exists() else {'version': None}
    atomic_bytes(pointer, canonical({'version': version, 'previous': old['version'], 'reader_level': manifest['reader_level']}))
    return {'activated': version, 'data_untouched': True}
