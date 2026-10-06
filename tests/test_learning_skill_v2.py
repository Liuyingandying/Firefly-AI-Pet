import copy
import json
from pathlib import Path
import numpy as np
import pytest
from core.learning.skill.v2.catalog import NODES, TOPICS, detect, path_to, curriculum_evidence
from core.learning.skill.v2.session import LearningSession, SkillStateError, checkpoint_path
from core.learning.skill.v2.visual import calculate, TEMPLATES
from core.learning.skill.v2.intelligence import LearningIntelligence


def start(tmp_path,topic='tem'):
    s=LearningSession(tmp_path/'navigation.json');s.start(topic,'我不理解'+TOPICS[topic][0]);return s


def event(s,action,**kw):return s.event(action,s.state['revision'],**kw)


def plan(s):
    while s.state['phase']=='PROBE':event(s,'answer',selection=len(s.question().options)-1)


def test_unconfigured_course_does_not_claim_formal_curriculum(tmp_path, monkeypatch):
    monkeypatch.setattr('learning.orchestrator.curriculum.ROOT', tmp_path)
    data=curriculum_evidence('state')
    assert data['mode']=='unconfigured' and data['course_id'] is None
    assert data['refs']==[] and data['review_units']==[]
    assert '不代表正式课程进度' in data['note']


@pytest.mark.parametrize('text,topic',[('我不理解TEM波','tem'),('我不会状态空间','state'),('我不会反馈控制','feedback'),('今天看到了一个状态空间图',None)])
def test_explicit_intent(text,topic):assert detect(text)==topic


def test_no_answer_no_progress_no_key_leak(tmp_path):
    s=start(tmp_path);before=copy.deepcopy(s.state)
    assert 'correct' not in json.dumps(s.view())
    with pytest.raises(SkillStateError):event(s,'answer')
    assert s.state==before


def test_plan_prunes_only_evidence_backed_known(tmp_path):
    s=start(tmp_path)
    event(s,'answer',selection=s.question().correct,assessment={'verdict':'supported','feedback':'valid'})
    plan(s)
    assert 'em.transverse' not in s.state['plan']
    assert 'em.energy' in s.state['plan']


def test_lockin_and_wrong_remediation(tmp_path):
    s=start(tmp_path);plan(s);event(s,'confirm_plan');event(s,'quiz')
    event(s,'answer',selection=s.question().correct)
    assert s.state['phase']=='QUIZ' and not s.state['locked']
    event(s,'answer',selection=(s.question().correct+1)%3)
    old=s.state['node'];event(s,'next')
    assert s.state['phase']=='REMEDIATE' and s.state['node']==old
    event(s,'retry');event(s,'quiz')
    event(s,'answer',selection=s.question().correct,assessment={'verdict':'supported'})
    assert s.state['locked']==[old]
    event(s,'next');assert s.state['node']!=old


def test_duplicate_event_and_stale_owner(tmp_path):
    s=start(tmp_path);other=LearningSession(s.path);rev=s.state['revision']
    event(s,'answer',selection=3)
    with pytest.raises(SkillStateError):s.event('answer',rev,selection=3)
    with pytest.raises(SkillStateError):event(other,'answer',selection=3)


def test_restore_pending_pause_version_corruption(tmp_path):
    s=start(tmp_path);plan(s);event(s,'confirm_plan');event(s,'quiz');event(s,'pause')
    other=LearningSession(s.path);event(other,'resume');assert other.state['phase']=='QUIZ'
    data=json.loads(s.path.read_text());data['catalog_hash']='other';s.path.write_text(json.dumps(data))
    with pytest.raises(SkillStateError):LearningSession(s.path)
    assert json.loads(s.path.read_text())['catalog_hash']=='other'


def test_write_failure_not_published(tmp_path,monkeypatch):
    s=start(tmp_path);before=copy.deepcopy(s.state)
    def fail(*args):raise PermissionError()
    monkeypatch.setattr('core.learning.skill.v2.session.atomic_write_json',fail)
    with pytest.raises(PermissionError):event(s,'answer',selection=3)
    assert s.state==before


def test_conversation_namespace(tmp_path):
    assert checkpoint_path(tmp_path,'one')!=checkpoint_path(tmp_path,'two')
    assert checkpoint_path(tmp_path,'../../escape').parent==tmp_path/'skill_v2'


@pytest.mark.parametrize('topic',list(TOPICS))
def test_complete_each_supported_goal(tmp_path,topic):
    s=start(tmp_path,topic);plan(s);event(s,'confirm_plan')
    for _ in s.state['plan'][:]:
        event(s,'quiz');event(s,'answer',selection=s.question().correct,assessment={'verdict':'supported'});event(s,'next')
    assert s.state['phase']=='COMPLETE'
    assert 'mastery' not in s.state


@pytest.mark.parametrize('template',list(TEMPLATES))
def test_all_visual_templates(template):
    data=calculate(template,30 if template=='tem' else .7)
    assert data['validation']=='deterministic_checks_passed'
    json.dumps(data,allow_nan=False)


@pytest.mark.parametrize('zeta,expected',[(.2,.52662),(.7,.04599),(1,0),(2,0)])
def test_step_physics(zeta,expected):
    y=calculate('step',zeta)['curves'][0]['y']
    assert abs(y[0])<1e-10 and abs(y[-1]-1)<.005
    assert abs(max(0,max(y)-1)-expected)<.002


def test_tem_vectors():
    data=calculate('tem',90)
    assert np.allclose(data['E'],[0,1,0])
    assert data['H'][0]<0 and data['S'][2]>0
    assert abs(np.dot(data['E'],data['H']))<1e-12
    assert np.allclose(calculate('tem',90,phase=np.pi/2)['E'],[0,0,0],atol=1e-12)


def test_root_and_bode():
    d=calculate('root_locus',1)
    for x,y in d['markers']:assert abs(complex(x,y)**2+complex(x,y)+1)<1e-12
    d=calculate('bode',.7)
    assert abs(d['curves'][0]['y'][0])<.01
    assert d['curves'][1]['y'][-1]<-170


@pytest.mark.parametrize('value',[float('nan'),float('inf'),True,-1])
def test_invalid_lab(value):
    with pytest.raises(ValueError):calculate('step',value)


def test_model_reuses_provider_tasks_and_validates():
    class Provider:
        def complete(self,payload,**kw):
            self.kw=kw;return {'model':'test-response','choices':[{'message':{'content':'{"text":"先确认缺失的前置节点。"}'}}]}
    provider=Provider();brain=LearningIntelligence(provider)
    state={'node':'em.transverse','goal':'TEM','plan':['em.transverse'],'evidence':{}}
    result=brain.request('plan',state)
    assert provider.kw['task']=='review' and 'model' not in provider.kw and 'profile' not in provider.kw
    assert result['response_model']=='test-response'
    with pytest.raises(ValueError):brain.request('grade',state,reason='anything')


def test_explicit_memory_only():
    class Memory:
        def remember(self,*args,**kwargs):self.args=args;self.kw=kwargs;return object()
    memory=Memory();brain=LearningIntelligence(memory=memory)
    assert not hasattr(memory,'args')
    brain.remember('偏好方向图')
    assert memory.args==('记住：偏好方向图',) and 'asserted_explicit' not in memory.kw


def test_memory_uses_existing_recall_gate():
    from types import SimpleNamespace
    class Memory:
        def retrieve_for_prompt(self, query, **kwargs):
            self.query=query;self.kw=kwargs
            return [SimpleNamespace(content='喜欢先预测后观察')]
        def search(self,*args,**kwargs):raise AssertionError('must not bypass recall gate')
    class Provider:
        def complete(self,payload,**kwargs):
            self.payload=payload
            return {'choices':[{'message':{'content':'{"text":"先预测，再观察方向。"}'}}]}
    memory=Memory();provider=Provider();brain=LearningIntelligence(provider,memory)
    state={'node':'em.transverse','goal':'TEM','plan':['em.transverse'],'evidence':{}}
    brain.request('teach',state)
    assert memory.kw=={'max_injected':3,'context':{'mode':'learning','intent':'teach'}}
    data = next(m['content'] for m in provider.payload['messages'] if m['role'] == 'user')
    assert '喜欢先预测后观察' in data
    assert 'PERSONA CONTEXT READ LAYER' in provider.payload['messages'][0]['content']


def test_wrong_reason_is_available_to_adaptive_teaching(tmp_path):
    s=start(tmp_path);plan(s);event(s,'confirm_plan');event(s,'quiz')
    event(s,'answer',selection=1,reason='我以为横向就是屏幕水平方向')
    e=s.state['evidence'][s.state['node']]
    assert e['learner_reason']=='我以为横向就是屏幕水平方向'
    assert e['question'] and e['answer']
