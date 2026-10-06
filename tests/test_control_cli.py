"""Control CLI and real local IPC; temporary state and fake provider status."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

from core.control.service import ControlService, ControlError
from core.control.ipc import dispatch, handle_socket
from core.settings_manager import SettingsManager
from core.user_paths import UserDataPaths

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def state(tmp_path):
    settings=SettingsManager(preferences_file=tmp_path/'preferences.json')
    settings._preferences.update(session='same', memory='preserved', learning_last_course_id='course-existing')
    shell=SimpleNamespace(settings=settings, _provider_manager_window=None)
    providers=SimpleNamespace(list_providers=lambda:[{'id':'tju','configured':True,'source':'credential_store'}],
                              get_provider_status=lambda _: {'configured':True})
    return ControlService(shell=shell,settings=settings,providers=providers,paths=UserDataPaths(tmp_path/'user'))


def test_status_and_model_list_no_private_configuration(state):
    assert state.execute('status')['runtime']['connected']
    resources=state.execute('model.list')['profiles']
    assert {r['model'] for r in resources}=={'tju-llm','tju-llm-max'}
    assert state.execute('model.status')['policy']=='tju-stable'
    assert not state.settings.preferences_file.exists()


@pytest.mark.parametrize('alias,expected',[('tju-llm','tju-stable'),('tju-llm-max','tju-max'),('tju-max','tju-max'),('auto','auto')])
def test_model_aliases_reuse_single_policy_owner(state,alias,expected):
    owner=state.settings
    result=state.execute('model.use',alias)
    assert result['policy']==expected
    assert state.shell.settings is owner
    assert owner._preferences['session']=='same'
    assert owner._preferences['memory']=='preserved'
    assert owner._preferences['learning_last_course_id']=='course-existing'


def test_offline_mutation_and_unknown_profile_are_rejected(state):
    with pytest.raises(ControlError,match='UNKNOWN_PROFILE'):state.execute('model.use','unsupported-model')
    state.shell=None
    with pytest.raises(ControlError,match='RUNTIME_REQUIRED'):state.execute('model.use','tju-max')
    assert not state.settings.preferences_file.exists()


def test_learning_offline_is_readonly_and_does_not_create_database(state):
    state.shell=None
    result=state.execute('learning.status')
    assert not result['database_present']
    assert not state.paths.root.exists()


def test_memory_uses_existing_runner_facade(state):
    state.shell.character_conversation=SimpleNamespace(memory_service=object(),runtime=SimpleNamespace())
    assert state.memory()['runtime_attached'] is True


def test_protocol_errors_redact_details_and_do_not_quit(state,monkeypatch):
    result=dispatch(state.shell, {'version':1,'id':'x','command':'quit'})
    assert result['error']=='UNKNOWN_COMMAND'
    monkeypatch.setattr(ControlService,'execute',lambda *a:(_ for _ in ()).throw(RuntimeError('SECRET')))
    result=dispatch(state.shell, {'version':1,'id':'x','command':'status'})
    assert result['error']=='CONTROL_OPERATION_FAILED' and 'SECRET' not in json.dumps(result)


def test_fragmented_frames_and_exact_legacy_quit(state):
    class Socket:
        data=b''; saved=None; output=b''; disconnected=False
        def readAll(self):return self.data
        def property(self,k):return self.saved
        def setProperty(self,k,v):self.saved=v
        def write(self,b):self.output+=b
        def flush(self):pass
        def disconnectFromServer(self):self.disconnected=True
    socket=Socket();closed=[];state.shell.exit_application=lambda:closed.append(True)
    socket.data=b'{"version":1,"id":"quit-in-id",'
    handle_socket(state.shell,socket)
    assert not socket.output and not closed
    socket.data=b'"command":"model.status"}\n'
    handle_socket(state.shell,socket)
    assert json.loads(socket.output)['ok'] and not closed
    socket=Socket();socket.data=b'quit';handle_socket(state.shell,socket)
    assert closed==[True]


def test_cli_parser_has_no_chat_command():
    result=subprocess.run([sys.executable,str(ROOT/'firefly_cli.py'),'--help'],capture_output=True,text=True)
    assert result.returncode==0 and 'doctor' in result.stdout
    result=subprocess.run([sys.executable,str(ROOT/'firefly_cli.py'),'chat','hello'],capture_output=True,text=True)
    assert result.returncode==2


def test_real_cli_ipc_and_gui_settings_sync(state,monkeypatch):
    # Real CLI process -> named local socket -> GUI main thread -> production
    # management window. No live app/state/network is needed by this test.
    from PySide6.QtWidgets import QApplication
    from PySide6.QtNetwork import QLocalServer
    from core.provider_manager import ProviderManager
    from core.model_management import ModelManagement
    from ui.provider_manager_window import ProviderManagerWindow
    app=QApplication.instance() or QApplication([])
    window=ProviderManagerWindow(manager=ProviderManager(model_management=ModelManagement(settings=state.settings)))
    state.shell._provider_manager_window=window
    server=QLocalServer();name='Firefly-Control-Test-'+str(os.getpid());assert server.listen(name)
    sockets=[]
    def accept():
        s=server.nextPendingConnection();sockets.append(s)
        s.readyRead.connect(lambda:handle_socket(state.shell,s))
        if s.bytesAvailable():handle_socket(state.shell,s)
    server.newConnection.connect(accept)
    code="from core.control.ipc import call; import json; print(json.dumps(call('model.use','tju-llm-max',server_name="+repr(name)+")))"
    p=subprocess.Popen([sys.executable,'-c',code],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    import time
    deadline=time.monotonic()+6
    while p.poll() is None and time.monotonic()<deadline:
        app.processEvents();time.sleep(.01)
    if p.poll() is None:p.kill();pytest.fail('local IPC timeout')
    output,error=p.communicate()
    result=json.loads(output)
    assert result['ok'] and result['result']['gui_policy']=='tju-max'
    assert window._policy_combo.currentData()=='tju-max'
    window._policy_combo.setCurrentIndex(window._policy_combo.findData('tju-stable'))
    assert state.execute('model.status')['policy']=='tju-stable'
    server.close();window.close()
