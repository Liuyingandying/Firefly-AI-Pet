"""Product seams: real Console widgets with isolated persistence/provider doubles."""
import threading
from test_learning_skill_v2_ui import app, console, settle, Brain


def test_one_owner_across_card_strip_and_sidebar(console):
    panel=console.learning_skill_panel
    panel.route('我不理解TEM波')
    view=console.companion.context_status.view
    assert view.mode=='学习陪伴'
    assert view.focus==panel.session.view()['title']
    assert '了解你的起点' in view.activity and 'PROBE' not in panel.heading.text()
    assert console.mode_strip.text()==view.activity
    before=panel.session.path.read_bytes()
    console._update_context_status()
    assert panel.session.path.read_bytes()==before  # projection never writes


def test_pause_and_button_resume_preserve_owner(console):
    panel=console.learning_skill_panel;panel.route('我不理解TEM波')
    invocation=panel.session.state['invocation_id'];node=panel.session.state['node']
    panel.pause.click()
    assert not panel.active and not panel.isHidden() and panel.scroll.isHidden()
    view=console.companion.context_status.view
    assert view.mode=='自由对话' and not view.activity and view.resume_hint
    assert panel.pause.text()=='恢复学习'
    panel.pause.click()
    assert panel.active and not panel.scroll.isHidden()
    assert panel.session.state['invocation_id']==invocation
    assert panel.session.state['node']==node
    assert console.companion.context_status.view.mode=='学习陪伴'


def test_chat_escape_never_becomes_quiz_answer(console):
    panel=console.learning_skill_panel;panel.route('我不理解TEM波')
    assert panel.route('先聊别的')
    assert panel.session.state['phase']=='PAUSED'
    assert not panel.session.state['evidence']
    console._new_chat_session()
    assert not console.companion.context_status.view.resume_hint
    assert not console.companion.context_status.view.activity
    assert panel.isHidden()


def plan(panel,app):
    while panel.session.state['phase']=='PROBE':
        panel.group.button(3).setChecked(True);panel.perform();settle(panel,app)


def test_failed_plan_retry_uses_same_checkpoint_without_reprobing(console,app):
    class Once(Brain):
        calls=0
        def request(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1:raise TimeoutError('must not leak sensitive exception text')
            return super().request(*args,**kwargs)
    panel=console.learning_skill_panel;panel.intelligence=Once()
    panel.route('我不理解TEM波');plan(panel,app)
    assert panel.session.state['phase']=='PLAN'
    assert not panel.retry_model.isHidden() and not panel.busy
    assert 'sensitive' not in panel.status.text()+panel.status.toolTip()
    before=panel.session.state.copy()
    panel.retry_model.click();settle(panel,app)
    assert panel.intelligence.calls==2 and panel.retry_model.isHidden()
    assert panel.session.state['phase']=='PLAN'
    for key in ('invocation_id','node','evidence','locked'):
        assert panel.session.state[key]==before[key]
    assert '当前节点' in panel.session.state['model_note']


def test_failed_grade_retry_preserves_original_answer(console,app):
    class GradeOnce(Brain):
        failed=False
        def request(self,kind,*args,**kwargs):
            if kind=='grade' and not self.failed:
                self.failed=True;raise ConnectionError()
            return super().request(kind,*args,**kwargs)
    panel=console.learning_skill_panel;panel.intelligence=GradeOnce()
    panel.route('我不理解TEM波');plan(panel,app);panel.perform();settle(panel,app);panel.perform()
    panel.group.button(panel.session.question().correct).setChecked(True)
    panel.reason.setText('横向以传播轴为参照');panel.perform();settle(panel,app)
    assert panel.session.state['phase']=='QUIZ' and not panel.session.state['locked']
    assert panel.reason.text()=='横向以传播轴为参照'
    panel.retry_model.click();settle(panel,app)
    assert panel.session.state['phase']=='FEEDBACK' and len(panel.session.state['locked'])==1


def test_busy_projection_and_late_reply_after_pause(console,app):
    release=threading.Event()
    class Slow(Brain):
        def request(self,*a,**kw):
            release.wait(2);return super().request(*a,**kw)
    panel=console.learning_skill_panel;panel.intelligence=Slow()
    panel.route('我不理解TEM波');panel.ask_model('teach')
    assert '正在处理' in console.companion.context_status.view.next_action
    panel.pause.click();before=panel.session.path.read_bytes()
    release.set();settle(panel,app)
    assert panel.session.path.read_bytes()==before
    assert not panel.active and console.companion.context_status.view.mode=='自由对话'


def test_retry_is_invalid_after_switch(console,app):
    class Failing(Brain):
        def request(self,*a,**kw):raise TimeoutError()
    panel=console.learning_skill_panel;panel.intelligence=Failing()
    panel.route('我不理解TEM波');plan(panel,app)
    old_request=panel.failed_request
    console._new_chat_session();panel.route('我不会状态空间')
    before=panel.session.path.read_bytes()
    panel.failed_request=old_request;panel.retry_request()
    assert panel.session.path.read_bytes()==before and not panel.busy


def test_bridge_handoff_clears_skill_projection(console):
    panel=console.learning_skill_panel;panel.route('我不理解TEM波')
    from types import SimpleNamespace
    console._learning_bridge_session=SimpleNamespace(
        handle_incoming_text=lambda text:'旧课程回答',
        learning_context=dict(course_title='旧课程',chapter_title='原章',concept_title='原节点',button_label='继续'))
    console.input.setText('答案');console._send()
    view=console.companion.context_status.view
    assert not panel.active and not view.activity
    assert view.course_name=='旧课程'


def test_runtime_activity_cannot_overwrite_active_skill(console):
    from types import SimpleNamespace
    panel=console.learning_skill_panel;panel.route('我不理解TEM波')
    console._on_runtime_event(SimpleNamespace(kind='runtime.activity',payload='idle'))
    assert console.companion.companion_state_label.text()=='陪你学习'
    panel.busy=True;panel.context_changed.emit()
    assert console.companion.companion_state_label.text()=='正在思考这一步'
    panel.pause_learning()
    assert console.companion.companion_state_label.text()=='等待指令'
