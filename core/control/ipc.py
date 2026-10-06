"""Versioned JSON lines on Firefly's existing, user-scoped local socket."""
import json
import uuid
import time
from core.control.service import ControlService, ControlError

SERVER_NAME = 'FireflyAIPet-SingleInstance'
MAX_REQUEST = 16384


def dispatch(shell, request):
    rid = request.get('id') if isinstance(request, dict) else None
    try:
        if not isinstance(request, dict) or request.get('version') != 1 or not isinstance(rid, str) or len(rid) > 64:
            raise ControlError('INVALID_REQUEST')
        if set(request) - {'id', 'version', 'command', 'profile'}:
            raise ControlError('INVALID_REQUEST')
        result = ControlService(shell=shell).execute(request.get('command'), request.get('profile'))
        return {'version': 1, 'id': rid, 'ok': True, 'result': result}
    except ControlError as exc:
        return {'version': 1, 'id': rid, 'ok': False, 'error': exc.code}
    except Exception:
        # Never serialize exception text, environment, credentials or HTTP bodies.
        return {'version': 1, 'id': rid, 'ok': False, 'error': 'CONTROL_OPERATION_FAILED'}


def handle_socket(shell, socket):
    data = bytes(socket.property('firefly_control_buffer') or b'') + bytes(socket.readAll())
    if len(data) > MAX_REQUEST:
        socket.write(b'{"version":1,"ok":false,"error":"REQUEST_TOO_LARGE"}\n')
        socket.disconnectFromServer()
        return
    # Preserve the established exact legacy shutdown request, never substring matching.
    if data.strip() == b'quit':
        socket.disconnectFromServer()
        shell.exit_application()
        return
    if b'\n' not in data:
        socket.setProperty('firefly_control_buffer', data)
        return
    try:
        request = json.loads(data.split(b'\n', 1)[0])
    except (ValueError, UnicodeError):
        request = None
    result = dispatch(shell, request)
    socket.write((json.dumps(result, ensure_ascii=False) + '\n').encode('utf-8'))
    socket.flush()
    socket.disconnectFromServer()


def call(command, profile=None, *, timeout_ms=2500, server_name=SERVER_NAME):
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    app = QCoreApplication.instance() or QCoreApplication([])
    socket = QLocalSocket()
    deadline = time.monotonic() + timeout_ms/1000
    socket.connectToServer(server_name)
    if not socket.waitForConnected(min(timeout_ms, 400)):
        return None
    request = {'version': 1, 'id': uuid.uuid4().hex, 'command': command}
    if profile is not None:
        request['profile'] = profile
    socket.write((json.dumps(request) + '\n').encode())
    socket.flush()
    data = b''
    while b'\n' not in data and time.monotonic() < deadline:
        if socket.bytesAvailable() or socket.waitForReadyRead(max(1, int((deadline-time.monotonic())*1000))):
            data += bytes(socket.readAll())
        else:
            break
        if len(data) > 1024*1024:
            raise ControlError('RESPONSE_TOO_LARGE')
    socket.disconnectFromServer()
    try:
        response = json.loads(data.split(b'\n', 1)[0])
        if response.get('id') != request['id'] or response.get('version') != 1:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ControlError('RUNTIME_PROTOCOL_UNAVAILABLE') from None
    return response
