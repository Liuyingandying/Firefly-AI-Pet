import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import time
from types import SimpleNamespace
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from core.conversation_store import ConversationStore
from core.settings_manager import SettingsManager
from ui.v2.console import CompanionConsole
from ui.v2.learning_visual_lab import VisualLab
from core.learning.skill.v2.visual import TEMPLATES


@pytest.fixture(scope='module')
def app():return QApplication.instance() or QApplication([])


class Brain:
    def request(self,kind,state,**kw):
        return {'result': {'verdict':'supported','feedback':'合理'} if kind=='grade' else {'text':'只解释当前节点。'},
                'task':'review' if kind=='plan' else 'learning','response_model':'test','latency_ms':1}


def settle(panel,app):
    deadline=time.monotonic()+3
    while (panel.busy or panel.workers) and time.monotonic()<deadline:
        app.processEvents();QTest.qWait(10)
    assert not panel.busy and not panel.workers


@pytest.fixture
def console(tmp_path,app):
    class Runner:
        agent_event=None;session_video=None;video_study=None;learning_controller=None
        _screen_vision_settings=SettingsManager(preferences_file=tmp_path/'prefs.json')
        runtime=SimpleNamespace(conversation_store=ConversationStore(tmp_path/'conversation.json'))
        def ask(self,*a,**kw):return True
    c=CompanionConsole(Runner());c.learning_skill_panel.root=tmp_path/'nav';c.learning_skill_panel.intelligence=Brain()
    yield c
    settle(c.learning_skill_panel,app);c.close();app.processEvents()


def test_real_console_entry_card_wait_grade_and_resume(console,app):
    console.input.setText('我不理解TEM波');console._send()
    panel=console.learning_skill_panel
    assert panel.active and panel.session.state['phase']=='PROBE'
    assert panel.group.checkedId()==-1
    # Unknown is a legitimate diagnosis, not an auto-generated response.
    while panel.session.state['phase']=='PROBE':
        panel.group.button(3).setChecked(True);panel.perform();settle(panel,app)
    assert panel.session.state['phase']=='PLAN'
    panel.perform();settle(panel,app)
    assert panel.session.state['phase']=='TEACH' and panel.lab.canvas.data['template']=='tem'
    panel.perform();assert panel.session.state['phase']=='QUIZ'
    panel.group.button(panel.session.question().correct).setChecked(True)
    panel.reason.setText('横向以传播轴为参照，转到 y 仍然垂直 z。')
    panel.perform();settle(panel,app)
    assert panel.session.state['phase']=='FEEDBACK' and len(panel.session.state['locked'])==1
    panel.pause_learning();assert panel.session.state['phase']=='PAUSED'
    assert panel.route('继续学习') and panel.session.state['phase']=='FEEDBACK'


def test_session_switch_detaches(console,app):
    panel=console.learning_skill_panel;panel.route('我不会状态空间')
    old=panel.session.path
    panel.status.setText('previous model');panel.preference.setText('unsaved preference')
    console._new_chat_session()
    assert not panel.active and panel.isHidden()
    assert not panel.status.text() and not panel.preference.text()
    panel.route('我不理解TEM波');assert panel.session.path!=old


def test_legacy_waiting_answer_owns_input(console):
    panel=console.learning_skill_panel
    console.tutor_chat.waiting_session_id='legacy-test'
    console._route_tutor_turn=lambda text:'旧 Tutor 已处理'
    console.input.setText('我不会状态空间');console._send()
    assert not panel.active


def test_late_model_reply_cannot_enter_new_conversation(console,app):
    import threading
    release=threading.Event()
    class DelayedBrain(Brain):
        def request(self,*args,**kwargs):
            release.wait(2)
            return super().request(*args,**kwargs)
    panel=console.learning_skill_panel;panel.intelligence=DelayedBrain()
    panel.route('我不理解TEM波');panel.ask_model('teach')
    console._new_chat_session();panel.route('我不会状态空间')
    before=panel.session.state.copy()
    release.set();settle(panel,app)
    assert panel.session.state==before


@pytest.mark.parametrize('template',list(TEMPLATES))
def test_lab_renders_real_qt_widget(app,tmp_path,template):
    lab=VisualLab();lab.resize(650,330);lab.set_template(template);lab.show();app.processEvents()
    assert lab.grab().save(str(tmp_path/(template+'.png')))
    if template=='tem':
        before=lab.canvas.data['E'];lab.slider.setValue(90);assert lab.canvas.data['E']!=before
        lab.rotate_view();lab.tick();assert lab.phase>0
    lab.close()
