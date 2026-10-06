"""Learning Skill UI adapter. It owns presentation, never formal learning state."""
import copy
import logging
import threading
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QRadioButton, QButtonGroup, QLineEdit, QScrollArea)
from core.learning.skill.v2.catalog import NODES, TOPICS, detect
from core.learning.skill.v2.session import LearningSession, checkpoint_path, SkillStateError
from core.learning.skill.v2.intelligence import LearningIntelligence
from core.user_paths import get_user_data_paths
from .learning_visual_lab import VisualLab
from .learning_skill_context import PHASE_LABELS, skill_context

log = logging.getLogger(__name__)


class ModelJob(QObject):
    result = Signal(object)
    finished = Signal()
    def __init__(self, intelligence, kind, state, args, parent):
        super().__init__(parent);self.intelligence=intelligence;self.kind=kind;self.state=state;self.args=args
    def run(self):
        try:
            value=self.intelligence.request(self.kind,self.state,**self.args)
            result={'ok':True,**value}
        except Exception as exc:
            codes=getattr(exc,'statuses',())
            result={'ok':False,'error':type(exc).__name__,
                    'status_codes':[x for x in codes if type(x) is int]}
        try:
            self.result.emit(result)
            self.finished.emit()
        except RuntimeError:
            # Application may have closed while a bounded provider request ran.
            pass
    def start(self):
        # No QThread can be destroyed while running when the user exits Firefly.
        threading.Thread(target=self.run,daemon=True,name='FireflyLearningRequest').start()


class LearningSkillPanel(QWidget):
    context_changed = Signal()

    def __init__(self, console, *, root=None, intelligence=None):
        super().__init__(console)
        self.console=console;self.root=root or get_user_data_paths().learning/'curriculum_navigation'
        self.intelligence=intelligence or LearningIntelligence(
            memory=getattr(console.runner,'memory_service',None),
            persona_reader=getattr(console.runner,'read_persona_context',None))
        self.session=None;self.bound_conversation=None;self.workers=[];self.busy=False;self.active=False
        self.failed_request = None
        layout=QVBoxLayout(self);layout.setContentsMargins(4,4,4,4)
        top=QHBoxLayout();self.heading=QLabel('Learning Skill');top.addWidget(self.heading,1)
        self.pause=QPushButton('暂停学习');self.pause.clicked.connect(self.toggle_pause);top.addWidget(self.pause)
        layout.addLayout(top)
        scroll=QScrollArea();self.scroll=scroll;scroll.setWidgetResizable(True);scroll.setMaximumHeight(490)
        body=QWidget();self.body=QVBoxLayout(body);scroll.setWidget(body);layout.addWidget(scroll)
        self.text=QLabel();self.text.setWordWrap(True);self.text.setTextFormat(__import__('PySide6.QtCore',fromlist=['Qt']).Qt.TextFormat.PlainText)
        self.body.addWidget(self.text)
        self.lab=VisualLab();self.body.addWidget(self.lab)
        self.question=QLabel();self.question.setWordWrap(True);self.body.addWidget(self.question)
        self.options=QWidget();self.options_layout=QVBoxLayout(self.options);self.body.addWidget(self.options)
        self.group=QButtonGroup(self)
        self.reason=QLineEdit();self.reason.setPlaceholderText('说说理由：你用到了哪个关系？');self.reason.setAccessibleName('学习理由');self.body.addWidget(self.reason)
        row=QHBoxLayout();self.action=QPushButton();self.action.clicked.connect(self.perform);row.addWidget(self.action)
        self.explain=QPushButton('换一种解释');self.explain.clicked.connect(lambda:self.ask_model('teach'));row.addWidget(self.explain)
        self.body.addLayout(row)
        self.status=QLabel();self.status.setWordWrap(True);self.body.addWidget(self.status)
        self.retry_model=QPushButton('重试上次请求');self.retry_model.clicked.connect(self.retry_request)
        self.retry_model.hide();self.body.addWidget(self.retry_model)
        memoryrow=QHBoxLayout();self.preference=QLineEdit();self.preference.setPlaceholderText('想让我长期记住的学习偏好（可选）');memoryrow.addWidget(self.preference)
        remember=QPushButton('明确记住');remember.clicked.connect(self.remember);memoryrow.addWidget(remember);self.body.addLayout(memoryrow)
        self.setVisible(False)

    def conversation(self):
        return self.console._current_session_id or 'firefly-main'

    def valid_binding(self):
        return self.bound_conversation==self.conversation()

    def context_view(self):
        return skill_context(self.session.state, busy=self.busy)

    def activity_snapshot(self):
        """Allowlisted facts from the bound owner, with no prompt/answer content."""
        if not self.session or not self.valid_binding():return None
        state=self.session.state
        return {
            'conversation_id': self.bound_conversation,
            'invocation_id': state['invocation_id'], 'revision': state['revision'],
            'topic': state['topic'], 'node_id': state['node'], 'phase': state['phase'],
            'active': self.active, 'busy': self.busy,
            'resume_available': not self.active and state['phase']!='COMPLETE',
            'verified_nodes': len(state['locked']), 'planned_nodes': len(state['plan']),
            'scope': 'local_teaching_navigation',
        }

    def _resume_candidate(self):
        paths=[checkpoint_path(self.root,self.conversation()+'|'+topic) for topic in TOPICS]
        paths=sorted((p for p in paths if p.is_file()),key=lambda p:(p.stat().st_mtime_ns,p.name),reverse=True)
        for path in paths:
            session=LearningSession(path)  # Invalid newest record fails closed, never silently replaced.
            if session.state['phase']!='COMPLETE':return session
        return None

    def offer_resume(self):
        """Discover a recovery affordance without activating, writing or calling AI."""
        if not self.console._current_session_id or (self.active and self.valid_binding()):return
        self.session=None;self.bound_conversation=self.conversation();self.active=False;self.busy=False
        try:
            self.session=self._resume_candidate()
            if self.session is None:
                self.hide();self.context_changed.emit();return
            self.render();self.show()
        except Exception as exc:
            self.session=None;self.scroll.hide();self.pause.hide()
            self.heading.setText('发现学习记录，但暂不能恢复。原记录已保留；请检查记录版本或完整性。')
            self.show();self.context_changed.emit()
            log.warning('learning_recovery_discovery_failed error=%s',type(exc).__name__)

    def toggle_pause(self):
        if self.session and not self.active:
            self.route('继续学习')
        else:
            self.pause_learning()

    def retry_request(self):
        request=self.failed_request
        if not request or self.busy or not self.active or not self.valid_binding():return
        kind,args,invocation,revision=request
        if (self.session.state['invocation_id'],self.session.state['revision'])!=(invocation,revision):
            self.failed_request=None;self.retry_model.hide();return
        self.ask_model(kind,**args)

    def detach(self):
        # Do not let a late model result enter another conversation or old invocation.
        if self.session and self.active and self.session.state['phase'] not in {'PAUSED','COMPLETE'}:
            try:self.session.event('pause',self.session.state['revision'])
            except Exception:log.warning('skill pause checkpoint failed')
        self.active=False;self.busy=False;self.bound_conversation=None;self.lab.timer.stop()
        self.failed_request=None;self.retry_model.hide()
        self.status.clear();self.preference.clear();self.hide()
        self.context_changed.emit()

    def route(self,text):
        if not self.valid_binding():
            self.detach()
        topic=detect(text)
        if text.strip() in {'退出学习','暂停学习','结束学习','返回聊天','先聊别的','随便聊聊'} and self.active:
            self.pause_learning();return True
        if not topic and not self.active and text.strip()!='继续学习':return False
        if not topic and self.active:
            if text.strip()=='继续学习':
                if self.session.state['phase']=='PAUSED':self.perform()
                else:self.status.setText('请使用当前节点卡片继续；待答题不会被跳过。')
                return True
            if self.session.state['phase'] in {'PROBE','QUIZ'}:
                self.reason.setText(text)
                self.status.setText('已填入你的理由。请在卡片选择答案并提交；不会替你选择。')
                return True
            if '?' in text or '？' in text or any(x in text for x in ('为什么','怎么','不懂')):
                self.ask_model('teach',question=text);return True
            # Unrelated conversation is still ordinary Firefly chat.
            self.pause_learning()
            return False
        try:
            sid=self.conversation()
            if topic is None:
                candidate=self._resume_candidate()
                if candidate is None:return False
                path=candidate.path
            else:path=checkpoint_path(self.root,sid+'|'+topic)
            self.detach();session=LearningSession(path)
            restored=session.state is not None
            if session.state is None:session.start(topic,text)
            elif session.state['phase']=='PAUSED':session.event('resume',session.state['revision'])
            self.session=session;self.bound_conversation=sid;self.active=True;self.busy=False
            self.show();self.render()
            self.console.chat.append_assistant(
                '已恢复到：'+NODES[session.state['node']].title+'。请从卡片继续。' if restored
                else '我们先定位你卡住的一步。请使用学习卡片；随时可以暂停。')
            store=self.console._conversation_store()
            if store is not None:store.append_exchange(text,'学习 Skill：'+session.state['goal'],session_id=sid)
            return True
        except Exception as exc:
            self.console.chat.append_assistant('学习状态暂不可恢复：'+type(exc).__name__+'。原记录保留。')
            return True

    def render(self):
        if not self.session or not self.valid_binding():return
        v=self.session.view();phase=v['phase']
        self.failed_request=None;self.retry_model.hide();self.status.clear()
        self.pause.show()
        if not self.active:
            self.heading.setText(f"上次学到：{TOPICS[v['topic']][0]} · {v['title']} · 已验证 {len(v['locked'])}/{len(v['plan'])} 节点")
            self.scroll.hide();self.pause.setText('恢复学习');self.lab.timer.stop()
            self.context_changed.emit();return
        self.heading.setText(f"{TOPICS[v['topic']][0]} · {PHASE_LABELS[phase]} · 已验证 {len(v['locked'])}/{len(v['plan'])} 节点")
        self.scroll.setVisible(phase!='PAUSED')
        text='目标：'+v['goal']+'\n当前：'+v['title']+'\n'
        if phase=='PROBE':text+='先判断理解边界；不知道可以直接选，不需要猜。'
        elif phase=='PLAN':text+='最短学习路径：\n'+' → '.join(NODES[n].title for n in v['plan'])+'\n'+'\n'.join(line for line in v['model_note'].splitlines() if not line.startswith('task='))
        elif phase in {'TEACH','REMEDIATE'}:text+=v['claim']+'\n'+v['explanation']+'\n先预测变化，再操作图形观察。'
        elif phase=='COMPLETE':text+='这次目标的节点已验证。局部证据不等同于正式课程掌握度。'
        text+='\n'+v['feedback']
        if v['curriculum']['mode']=='curriculum_preview':
            text+='\n材料：自动控制原理草稿知识地图（待审单元未自动纳入）'
        elif v['curriculum']['mode']=='unconfigured':
            text+='\n'+v['curriculum']['note']
        self.text.setText(text)
        self.lab.setVisible(phase in {'TEACH','REMEDIATE'})
        if phase in {'TEACH','REMEDIATE'}:self.lab.set_template(v['lab'])
        for button in self.group.buttons():self.group.removeButton(button);self.options_layout.removeWidget(button);button.deleteLater()
        self.question.setText(v.get('question',{}).get('text',''))
        for i,option in enumerate(v.get('question',{}).get('options',[])):
            b=QRadioButton(option);b.setAccessibleName(option);self.group.addButton(b,i);self.options_layout.addWidget(b)
        self.reason.setVisible(phase in {'PROBE','QUIZ'});self.reason.clear()
        labels={'PROBE':'提交诊断','PLAN':'确认路径，开始这一节点','TEACH':'开始独立检验','QUIZ':'提交答案与理由',
                'FEEDBACK':'继续 / 针对性补救','REMEDIATE':'再学这一步','PAUSED':'恢复这一节点','COMPLETE':'本次目标已完成'}
        self.action.setText(labels[phase]);self.action.setEnabled(not self.busy and phase!='COMPLETE')
        self.explain.setVisible(phase in {'TEACH','REMEDIATE'});self.explain.setEnabled(not self.busy)
        self.pause.setText('恢复学习' if phase=='PAUSED' else '暂停学习')
        self.context_changed.emit()

    def perform(self):
        if self.busy or not self.valid_binding():return
        s=self.session.state;phase=s['phase'];revision=s['revision']
        try:
            if phase in {'PROBE','QUIZ'}:
                choice=self.group.checkedId();reason=self.reason.text().strip();q=self.session.question()
                if choice<0:self.status.setText('请选择一个答案，或选择不知道。');return
                if choice==q.correct and reason:
                    self.ask_model('grade',answer=q.options[choice],reason=reason,question=q.text,rubric=q.reason,selection=choice)
                    return
                self.session.event('answer',revision,selection=choice,reason=reason)
            else:
                action={'PLAN':'confirm_plan','TEACH':'quiz','FEEDBACK':'next','REMEDIATE':'retry','PAUSED':'resume'}.get(phase)
                if not action:return
                self.session.event(action,revision)
            self.render()
            if self.session.state['phase']=='PLAN':self.ask_model('plan')
            elif self.session.state['phase'] in {'TEACH','REMEDIATE'}:self.ask_model('teach')
        except Exception as exc:self.status.setText('未推进：'+type(exc).__name__+'；原状态保留。')

    def ask_model(self,kind,**args):
        if self.busy or not self.valid_binding():return
        state=copy.deepcopy(self.session.state);revision=state['revision'];sid=self.bound_conversation
        retry_args=copy.deepcopy(args);selection=args.pop('selection',None)
        self.failed_request=None;self.retry_model.hide()
        self.busy=True;self.action.setEnabled(False);self.explain.setEnabled(False);self.status.setText('正在思考这一步；你可以随时暂停，学习位置会保留。')
        self.context_changed.emit()
        job=ModelJob(self.intelligence,kind,state,args,self)
        self.workers.append(job)
        def receive(result):
            if not self.valid_binding() or sid!=self.bound_conversation or self.session.state['invocation_id']!=state['invocation_id'] or self.session.state['revision']!=revision:
                return
            self.busy=False
            if not result['ok']:
                codes=result.get('status_codes',[])
                self.status.setText('这次请求未完成，学习位置和答案仍保留。可以重试上次请求，或暂停后再继续。')
                self.status.setToolTip(result['error']+(f' HTTP {codes}' if codes else ''))
                log.warning('learning_skill_request_failed kind=%s error=%s http=%s',kind,result['error'],codes)
                self.failed_request=(kind,retry_args,state['invocation_id'],revision);self.retry_model.show()
                self.action.setEnabled(True);self.explain.setEnabled(True);self.context_changed.emit();return
            try:
                info=f"task={result['task']} · response={result['response_model']} · {result['latency_ms']}ms"
                if kind=='grade':
                    self.session.event('answer',revision,selection=selection,reason=args['reason'],assessment=result['result'])
                else:self.session.annotate(revision,text=result['result']['text'],note='')
                log.info('learning_skill_model node=%s %s',state['node'],info)
                self.render();self.status.setText('本次回复模型：'+result['response_model'])
                self.status.setToolTip(info)
                if kind=='grade' and self.session.state['phase']=='PLAN':self.ask_model('plan')
            except Exception as exc:
                self.status.setText('模型结果未应用：'+type(exc).__name__)
                self.action.setEnabled(True);self.explain.setEnabled(True);self.context_changed.emit()
        job.result.connect(receive)
        job.finished.connect(lambda:self.workers.remove(job) if job in self.workers else None)
        job.finished.connect(job.deleteLater);job.start()

    def pause_learning(self):
        if not self.session or not self.valid_binding():return
        try:
            if self.session.state['phase'] not in {'PAUSED','COMPLETE'}:self.session.event('pause',self.session.state['revision'])
            self.active=False;self.busy=False;self.lab.timer.stop();self.render();self.show()
            self.console.chat.append_assistant('已暂停，学习位置保留。点击“恢复学习”或输入“继续学习”可恢复；现在可自由聊天。')
        except Exception as exc:self.status.setText('暂停保存失败：'+type(exc).__name__)

    def remember(self):
        text=self.preference.text().strip()
        if not text:return
        try:
            record=self.intelligence.remember(text)
            self.status.setText('已通过 Firefly Memory 保存。' if record is not None else '当前 Memory 策略未允许保存。')
        except Exception as exc:self.status.setText('没有保存：'+type(exc).__name__)
