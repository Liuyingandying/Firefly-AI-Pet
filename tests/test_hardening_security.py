"""Local wire and malicious archive tests. No external model calls."""
import asyncio
import json
import os
import stat
import threading
import zipfile
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
import websockets
from PySide6.QtWidgets import QApplication
from core.pagelens_bridge import PageLensBridge, MAX_MESSAGE_BYTES
from tools.character_package.validator import extract_validated_archive, _zip_security_errors, MAX_SINGLE_FILE_BYTES


def test_authenticated_loopback_limits_and_isolation():
    app = QApplication.instance() or QApplication([])
    token = 'local-test-credential-' + 'x' * 32
    origin = 'chrome-extension://' + 'a' * 32
    calls = []
    release = threading.Event()
    def local_handler(messages, **kwargs):
        calls.append(messages)
        release.wait(3)
        raise RuntimeError('SECRET-DO-NOT-LEAK')
    bridge = PageLensBridge(chat_handler=local_handler, auth_token=token, allowed_origins=[origin])
    async def run():
        bridge._loop = asyncio.get_running_loop()
        async with websockets.serve(bridge._handle_client, '127.0.0.1', 0,
                process_request=bridge._process_request, max_size=MAX_MESSAGE_BYTES,
                max_queue=4, origins=[origin], compression=None) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            for test_origin, protocol in [(None, None), ('https://evil.invalid', token), (origin, 'wrong')]:
                with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
                    async with websockets.connect(url, origin=test_origin,
                            subprotocols=['firefly-auth.'+protocol] if protocol else None): pass
                assert exc.value.response.status_code == 403
            assert calls == []
            async with websockets.connect(url, origin=origin, subprotocols=['firefly-auth.'+token]) as ws:
                await ws.send('[]'); await ws.send('{malformed')
                await ws.send(json.dumps({'type':'bridge_ping'}))
                assert json.loads(await ws.recv())['type'] == 'bridge_pong'
                with pytest.raises(websockets.exceptions.InvalidStatus):
                    async with websockets.connect(url, origin=origin, subprotocols=['firefly-auth.'+token]): pass
                request = {'type':'ai_chat_request','source':'explain','messages':[{'role':'user','content':'local protocol test'}], 'temperature':0.2}
                for i in range(3): await ws.send(json.dumps({**request, 'requestId':str(i)}))
                busy = json.loads(await ws.recv())
                assert busy['requestId'] == '2' and 'busy' in busy['error']
                release.set()
                errors = [await ws.recv(), await ws.recv()]
                assert all('SECRET' not in e and 'AI request failed' in e for e in errors)
                assert len(calls) == 2
                await ws.send('x' * (MAX_MESSAGE_BYTES + 1))
                with pytest.raises(websockets.exceptions.ConnectionClosed) as closed: await ws.recv()
                assert closed.value.rcvd.code == 1009
        await asyncio.gather(*list(bridge._ai_chat_tasks.values()), return_exceptions=True)
    try: asyncio.run(run())
    finally: release.set()


def test_unpaired_bridge_never_binds_port():
    """RC Option B: without complete pairing the server must not listen.

    ``start()`` returns without spawning the loop thread; the port stays
    closed instead of the previous listening-and-rejecting state. The
    feature ships off unless FIREFLY_PAGELENS_TOKEN + ORIGINS configure
    pairing (decision 2026-09-30; PageLens_Pairing.md).
    """
    import socket
    import time
    app = QApplication.instance() or QApplication([])
    # no token / no origins -> disabled
    bridge = PageLensBridge(chat_handler=lambda *a, **k: {})
    assert len(bridge._auth_token) < 32 and not bridge._allowed_origins
    bridge.start()
    try:
        time.sleep(0.2)
        assert bridge._loop is None and bridge._thread is None
        s = socket.socket()
        s.settimeout(0.5)
        # nothing should have been bound BY THIS BRIDGE; the check that
        # matters is no loop/thread — a global port probe would race with
        # any other Firefly instance on the machine.
        s.close()
    finally:
        bridge.stop()

    # token but no origins -> still disabled
    bridge2 = PageLensBridge(chat_handler=lambda *a, **k: {},
                             auth_token='t' * 40)
    bridge2.start()
    try:
        time.sleep(0.2)
        assert bridge2._loop is None and bridge2._thread is None
    finally:
        bridge2.stop()

    # full pairing -> starts (then stopped immediately)
    bridge3 = PageLensBridge(chat_handler=lambda *a, **k: {},
                             auth_token='t' * 40,
                             allowed_origins=['chrome-extension://' + 'a' * 32])
    bridge3.start()
    try:
        time.sleep(0.5)
        assert bridge3._thread is not None
    finally:
        bridge3.stop()
        assert bridge3._thread is None


@pytest.mark.parametrize('name', ['../escape.txt','/absolute.txt','C:/drive.txt','folder\\escape.txt','CON.txt','safe.txt:stream','x/../../escape'])
def test_zip_traversal_rejected_before_write(tmp_path, name):
    archive=tmp_path/'bad.zip'; out=tmp_path/'out';out.mkdir()
    with zipfile.ZipFile(archive,'w') as z: z.writestr(name.replace('\\', '/'),'bad')
    if '\\' in name:
        archive.write_bytes(archive.read_bytes().replace(name.replace('\\', '/').encode(), name.encode()))
    with zipfile.ZipFile(archive) as z, pytest.raises(ValueError): extract_validated_archive(z,out)
    assert list(out.iterdir()) == []


def test_zip_bomb_symlink_duplicate_and_size(tmp_path):
    out=tmp_path/'out';out.mkdir()
    archive=tmp_path/'bomb.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z: z.writestr('image.png', b'0' * (2*1024*1024))
    with zipfile.ZipFile(archive) as z, pytest.raises(ValueError, match='compression ratio'): extract_validated_archive(z,out)
    info=zipfile.ZipInfo('large.png');info.file_size=MAX_SINGLE_FILE_BYTES+1;info.compress_size=info.file_size
    assert any('too large' in e for e in _zip_security_errors([info])[0])
    link=zipfile.ZipInfo('link');link.external_attr=(stat.S_IFLNK|0o777)<<16
    assert any('symbolic' in e for e in _zip_security_errors([link])[0])
    assert any('conflicting' in e for e in _zip_security_errors([zipfile.ZipInfo('A'),zipfile.ZipInfo('a')])[0])
    assert not list(out.iterdir())


def test_legacy_card_and_skin_share_zip_gate(tmp_path):
    from character.character_card import validate_card
    from skins.validator import validate_skin
    archive=tmp_path/'bad.zip'
    with zipfile.ZipFile(archive,'w') as z: z.writestr('../escape.txt','bad')
    assert not validate_card(archive).ok
    assert not validate_skin(archive).ok
    assert not (tmp_path/'escape.txt').exists()
