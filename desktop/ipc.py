"""Same-user local sockets with a separate capability token; no TCP listener."""
import csv
import hashlib
import io
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from collector.storage import StorageEngine, atomic_bytes
from collector.validation import canonical, strict_loads
from .service import WorkerService

MAX_MESSAGE = 1024 * 1024


def private_directory(root, base=None):
    if base is None:
        base = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / '.local' / 'share')) / 'ACGCollector'
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:20]
    from collector.security import checked_path, secure_path
    path = checked_path(Path(base) / key)
    secure_path(path, directory=True)
    return path


def endpoint(root, base=None):
    return private_directory(root, base) / 'worker.json'


class Client:
    def __init__(self, root, base=None, timeout=5000):
        self.root, self.base, self.timeout = str(Path(root).resolve()), base, timeout

    def call(self, command, args=None, request_id=None):
        marker = strict_loads(endpoint(self.root, self.base).read_bytes())
        if marker.get('transport') == 'mailbox':
            return self.mailbox(marker, command, args, request_id)
        socket = QLocalSocket()
        socket.connectToServer(marker['name'])
        if not socket.waitForConnected(self.timeout):
            raise ConnectionError('WORKER_OFFLINE')
        request = {'token': marker['token'], 'id': request_id or uuid.uuid4().hex,
                   'command': command, 'args': args or {}}
        raw = canonical(request) + b'\n'
        if len(raw) > MAX_MESSAGE:
            raise ValueError('MESSAGE_LIMIT')
        socket.write(raw)
        socket.waitForBytesWritten(self.timeout)
        deadline = time.monotonic() + self.timeout / 1000
        received = bytearray()
        try:
            while b'\n' not in received:
                remaining = int((deadline - time.monotonic()) * 1000)
                if remaining <= 0:
                    raise TimeoutError('COMMAND_RESULT_UNKNOWN_REFRESH_BEFORE_RETRY')
                if socket.bytesAvailable() == 0 and not socket.waitForReadyRead(remaining):
                    raise TimeoutError('COMMAND_RESULT_UNKNOWN_REFRESH_BEFORE_RETRY')
                received.extend(bytes(socket.readAll()))
                if len(received) > 4 * MAX_MESSAGE:
                    raise ValueError('RESPONSE_LIMIT')
            response = strict_loads(received.split(b'\n', 1)[0])
            if response['id'] != request['id']:
                raise ValueError('RESPONSE_ID_MISMATCH')
            if not response['ok']:
                raise RuntimeError(response['error'])
            return response['result']
        finally:
            socket.abort()

    def mailbox(self, marker, command, args, request_id):
        rid = request_id or uuid.uuid4().hex
        if not isinstance(rid, str) or len(rid) != 32 or any(c not in '0123456789abcdef' for c in rid):
            raise ValueError('INVALID_REQUEST_ID')
        private = private_directory(self.root, self.base)
        request = {'id': rid, 'token': marker['token'], 'command': command, 'args': args or {}}
        raw = canonical(request)
        if len(raw) > MAX_MESSAGE:
            raise ValueError('MESSAGE_LIMIT')
        path = private / 'requests' / (rid + '.json')
        response_path = private / 'responses' / (rid + '.json')
        if path.is_symlink() or response_path.is_symlink():
            raise PermissionError('UNSAFE_MAILBOX')
        # A timed-out retry uses the same id, never a new financial intent.
        response_path.unlink(missing_ok=True)
        atomic_bytes(path, raw)
        os.chmod(path, 0o600)
        deadline = time.monotonic() + self.timeout / 1000
        while time.monotonic() < deadline:
            if response_path.exists():
                if response_path.is_symlink() or response_path.stat().st_size > 4 * MAX_MESSAGE:
                    raise ValueError('RESPONSE_LIMIT')
                result = strict_loads(response_path.read_bytes())
                response_path.unlink(missing_ok=True)
                if result['id'] != rid:
                    raise ValueError('RESPONSE_ID_MISMATCH')
                if not result['ok']:
                    raise RuntimeError(result['error'])
                return result['result']
            time.sleep(0.025)
        raise TimeoutError('COMMAND_RESULT_UNKNOWN_REFRESH_BEFORE_RETRY')


class WorkerServer(QObject):
    def __init__(self, root, base=None):
        super().__init__()
        self.private = private_directory(root, base)
        self.store = StorageEngine(root)
        self.service = WorkerService(self.store, self.private)
        user_key = hashlib.sha256(str(self.private).encode()).hexdigest()[:32]
        self.name = 'acg-collector-' + user_key
        self.token = secrets.token_hex(32)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        QLocalServer.removeServer(self.name)  # writer lock already proves no active Worker
        self.transport = 'local-socket' if self.server.listen(self.name) else 'mailbox'
        if self.transport == 'mailbox':
            for directory in ('requests', 'responses'):
                folder = self.private / directory
                if folder.is_symlink():
                    raise PermissionError('UNSAFE_MAILBOX')
                folder.mkdir(mode=0o700, exist_ok=True)
        self.connections = {}
        self.cache = {}
        self.server.newConnection.connect(self.accept)
        atomic_bytes(self.private / 'worker.json', canonical({'name': self.name, 'token': self.token, 'pid': os.getpid(), 'transport': self.transport}))
        os.chmod(self.private / 'worker.json', 0o600)
        self.timer = QTimer(self)
        self.timer.setInterval(700)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.mail_timer = QTimer(self)
        self.mail_timer.setInterval(60)
        self.mail_timer.timeout.connect(self.poll_mailbox)
        if self.transport == 'mailbox':
            self.mail_timer.start()

    def accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if len(self.connections) >= 32:
                socket.abort()
                socket.deleteLater()
                continue
            self.connections[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: self.read(s))
            socket.disconnected.connect(lambda s=socket: self.disconnected(s))
            QTimer.singleShot(5000, socket.abort)

    def disconnected(self, socket):
        self.connections.pop(socket, None)
        socket.deleteLater()

    def read(self, socket):
        if socket not in self.connections:
            return
        buffer = self.connections[socket]
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > MAX_MESSAGE:
            socket.abort()
            return
        if b'\n' not in buffer:
            return
        response = self.execute(bytes(buffer.split(b'\n', 1)[0]))
        socket.write(canonical(response) + b'\n')
        socket.flush()
        socket.disconnectFromServer()
        if self.service.stopping:
            QTimer.singleShot(150, QCoreApplication.instance().quit)

    def execute(self, raw):
        request_id = None
        try:
            request = strict_loads(raw)
            if set(request) != {'id', 'token', 'command', 'args'}:
                raise ValueError('INVALID_COMMAND_ENVELOPE')
            request_id = request['id']
            if not isinstance(request_id, str) or len(request_id) != 32 or any(c not in '0123456789abcdef' for c in request_id):
                request_id = None
                raise ValueError('INVALID_REQUEST_ID')
            if not secrets.compare_digest(str(request['token']), self.token):
                raise PermissionError('IPC_AUTH_REQUIRED')
            if not isinstance(request_id, str) or len(request_id) != 32 or not isinstance(request['command'], str):
                raise ValueError('INVALID_REQUEST_ID')
            fingerprint = hashlib.sha256(canonical([request['command'], request['args']])).hexdigest()
            if request_id in self.cache:
                old_hash, response = self.cache[request_id]
                if old_hash != fingerprint:
                    raise ValueError('REQUEST_ID_REUSED_WITH_DIFFERENT_CONTENT')
            else:
                result = self.service.dispatch(request['command'], request['args'], request_id)
                response = {'id': request_id, 'ok': True, 'result': result}
                if request['command'] not in ('save_token', 'snapshot', 'query', 'preview', 'post_detail'):
                    if len(self.cache) > 500:
                        self.cache.pop(next(iter(self.cache)))
                    self.cache[request_id] = (fingerprint, response)
        except Exception as exc:
            code = str(exc) if isinstance(exc, (ValueError, PermissionError)) else type(exc).__name__
            # Only program-owned constant codes. Never echo input, URL, token or traceback.
            if not code or len(code) > 80 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789' for c in code):
                code = type(exc).__name__
            response = {'id': request_id, 'ok': False, 'error': code}
        if len(canonical(response)) > 4 * MAX_MESSAGE:
            return {'id': request_id, 'ok': False, 'error': 'RESPONSE_LIMIT'}
        return response

    def poll_mailbox(self):
        for path in (self.private / 'requests').glob('*.json'):
            rid = path.stem
            if len(rid) != 32 or any(c not in '0123456789abcdef' for c in rid) or path.is_symlink():
                continue
            if path.stat().st_size > MAX_MESSAGE:
                path.unlink()
                continue
            raw = path.read_bytes()
            response = self.execute(raw)
            path.unlink(missing_ok=True)  # secret request never retained in event data
            target = self.private / 'responses' / (rid + '.json')
            if target.is_symlink():
                continue
            atomic_bytes(target, canonical(response))
            os.chmod(target, 0o600)
            if self.service.stopping:
                QTimer.singleShot(150, QCoreApplication.instance().quit)
            break
        now = time.time()
        for path in (self.private / 'responses').glob('*.json'):
            if not path.is_symlink() and now - path.stat().st_mtime > 120:
                path.unlink(missing_ok=True)

    def tick(self):
        try:
            self.service.tick()
        except Exception as exc:
            self.service.failed_ticks.add('WORKER_STORE_ERROR')

    def close(self):
        self.timer.stop()
        self.mail_timer.stop()
        self.server.close()
        (self.private / 'worker.json').unlink(missing_ok=True)
        self.store.close()


def launch_worker(root, base=None):
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'worker.py'), '--root', str(Path(root).resolve())]
    if base:
        command.extend(['--private-base', str(base)])
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options['start_new_session'] = True
    return subprocess.Popen(command, **options)
