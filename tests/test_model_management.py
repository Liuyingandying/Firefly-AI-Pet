"""Capability resources and policy boundaries: synthetic, no network."""
import json
from pathlib import Path
import pytest
from core.model_router import ModelRouter, PROFILE_PATH
from core.model_management import ModelManagement
from core.settings_manager import SettingsManager


def custom_router(tmp_path, extra, **changes):
    data = json.loads(PROFILE_PATH.read_text())
    data['profiles'].append(extra)
    data.update(changes)
    p = tmp_path/'profiles.json'; p.write_text(json.dumps(data))
    return ModelRouter(profiles_path=p, event_sink=lambda e: None)


@pytest.mark.parametrize('provider,protocol', [('openai','openai-chat-completions'),('claude','anthropic-messages'),('local','openai-chat-completions')])
def test_future_resource_requires_matching_adapter(tmp_path,provider,protocol):
    profile={'name':'future','provider':provider,'protocol':protocol,'model':'test-model',
             'variant':None,'capabilities':['text','vision'],'fallback':None}
    router=custom_router(tmp_path,profile); sent=[]
    def transport(body,budget):
        sent.append(body);return {'choices':[{'message':{'content':'ok'}}]}
    with pytest.raises(ValueError,match='bound provider adapter'):
        router.request({},transport,profile='future')
    assert not sent
    router.request({},transport,profile='future',provider=provider,protocol=protocol)
    assert sent[0]['model']=='test-model'


def test_cross_provider_fallback_is_rejected_before_transport(tmp_path):
    with pytest.raises(ValueError,match='cross-provider'):
        custom_router(tmp_path,{'name':'future','provider':'openai','model':'test','variant':None,
                                'capabilities':['text'],'fallback':'tju-stable'})


def test_task_capability_contract_cannot_be_silently_dropped(tmp_path):
    router=custom_router(tmp_path,{'name':'text-only','provider':'tju','model':'test','variant':None,
                                  'capabilities':['text'],'fallback':None})
    sent=[]
    with pytest.raises(ValueError,match='capability'):
        router.request({},lambda *a:sent.append(a),task='authoring',profile='text-only')
    assert not sent


def test_legacy_profile_file_still_loads(tmp_path):
    data=json.loads(PROFILE_PATH.read_text());data.pop('task_capabilities')
    for p in data['profiles']:
        for k in ('protocol','display_name','verified_capabilities','evidence'):p.pop(k,None)
    path=tmp_path/'v1.json';path.write_text(json.dumps(data))
    assert ModelRouter(profiles_path=path).resolve(profile='tju-max').model=='tju-llm-max'


def test_resources_separate_configuration_declaration_and_evidence(tmp_path):
    from core.provider_manager import ProviderManager
    s=SettingsManager(preferences_file=tmp_path/'prefs.json')
    m=ModelManagement(settings=s)
    class Providers:
        def get_provider_status(self,p):return {'configured':False}
    rows=m.resources(Providers())
    assert all(not r['configured'] for r in rows)
    maximum=next(r for r in rows if r['id']=='tju-max')
    assert maximum['verified_capabilities']==('text',)
    assert 'tools' in maximum['declared_capabilities']
    assert not s.preferences_file.exists()  # read-only management never saves
    assert all(r['profile']=='tju-stable' for r in m.task_plan())
    m.set_policy('auto')
    assert {r['task']:r['profile'] for r in m.task_plan()}['review']=='tju-max'
