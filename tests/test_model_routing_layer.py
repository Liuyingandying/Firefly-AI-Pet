"""Offline routing, payload, continuity and settings gates. No live credentials."""
import json
import os
from copy import deepcopy
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.model_router import ModelRouter
from providers.base import ProviderHTTPError
from providers.tju_qwen import TJUQwenProvider

MESSAGES = [{"role": "user", "content": "routing smoke"}]


def completion(text="ok"):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture
def routing(monkeypatch):
    import core.model_router as module
    events, selection = [], ["tju-stable"]
    router = ModelRouter(selection=lambda: selection[0], event_sink=events.append)
    monkeypatch.setattr(module, "_default_router", router)
    return router, selection, events


def test_profile_discovery_default_and_all_task_routes(routing):
    router, selected, _ = routing
    for task in ("chat", "learning", "authoring", "review"):
        assert router.resolve(task=task).name == "tju-stable"
    selected[0] = "auto"
    assert router.resolve(task="chat").model == "tju-llm"
    assert router.resolve(task="learning").name == "tju-stable"
    for task in ("authoring", "review"):
        p = router.resolve(task=task)
        assert (p.model, p.variant, p.fallback) == ("tju-llm-max", "dsv4.1flash", "tju-stable")
    assert router.resolve(task="learning", profile="tju-max").name == "tju-max"
    assert router.resolve(model="existing-custom-model").model == "existing-custom-model"
    with pytest.raises(ValueError): router.resolve(profile="not-a-profile")
    with pytest.raises(ValueError): router.resolve(profile="tju-max", model="tju-llm")


def test_wire_model_variant_and_next_call_switch(routing):
    router, selected, _ = routing
    calls = []
    def transport(url, payload, headers, timeout):
        calls.append(deepcopy(payload)); return completion()
    provider = TJUQwenProvider(api_key="fake", model_router=router, transport=transport)
    provider.chat(MESSAGES)
    selected[0] = "tju-max"
    provider.chat(MESSAGES)
    assert calls[0]["model"] == "tju-llm" and "variant" not in calls[0]
    assert calls[1]["model"] == "tju-llm-max" and calls[1]["variant"] == "dsv4.1flash"
    assert all(c["messages"] == MESSAGES for c in calls)


@pytest.mark.parametrize("failure", [ProviderHTTPError(429), ProviderHTTPError(401), TimeoutError(), ValueError("invalid response")])
def test_max_fallback_removes_variant_and_redacts_events(routing, failure):
    router, _, events = routing
    payload = {"messages": deepcopy(MESSAGES), "tools": [{"type": "function", "function": {"name": "lookup"}}]}
    before = deepcopy(payload); calls = []
    def send(body, timeout):
        calls.append(body)
        if body["model"] == "tju-llm-max":
            raise failure
        return completion()
    router.request(payload, send, profile="tju-max", task="learning")
    assert [c["model"] for c in calls] == ["tju-llm-max", "tju-llm"]
    assert "variant" not in calls[1]
    assert payload == before and calls[0]["tools"] == calls[1]["tools"]
    switches = [e for e in events if e["event"] == "model_switch_event"]
    assert len(switches) == 1 and switches[0]["to_profile"] == "tju-stable"
    assert "routing smoke" not in json.dumps(events)


def test_malformed_max_completion_falls_back_and_stable_failure_stops(routing):
    router, _, _ = routing; calls = []
    def send(body, timeout):
        calls.append(body["model"]); return {"choices": []}
    with pytest.raises(Exception, match="usable completion"):
        router.request({}, send, profile="tju-max")
    assert calls == ["tju-llm-max", "tju-llm"]


def test_deadline_reserves_time_for_stable_and_snapshot_does_not_change():
    clock, mode, calls = [0.0], ["tju-max"], []
    router = ModelRouter(selection=lambda: mode[0], clock=lambda: clock[0], event_sink=lambda e: None)
    def send(body, timeout):
        calls.append((body["model"], timeout)); clock[0] += timeout
        mode[0] = "tju-stable"
        if len(calls) == 1: raise TimeoutError()
        return completion()
    router.request({}, send, timeout=20)
    assert calls == [("tju-llm-max", 10.0), ("tju-llm", 10.0)]


def test_vision_never_sent_to_unverified_max(routing):
    router, _, events = routing; models = []
    router.request({}, lambda p,t: models.append(p["model"]) or completion(),
                   profile="tju-max", capabilities=("vision",))
    assert models == ["tju-llm"]
    assert events[0]["reason"] == "unsupported_capability"


def test_cross_provider_fallback_never_receives_tju_model(routing, tmp_path):
    from core.ai_router import ProviderRouter
    from providers.zhipu_glm import ZhipuGLMProvider
    calls = []
    def down(url, payload, headers, timeout):
        calls.append(payload["model"]); raise ProviderHTTPError(503)
    def up(url, payload, headers, timeout):
        calls.append(payload["model"]); return completion()
    p = ProviderRouter([TJUQwenProvider(api_key="fake", transport=down),
                        ZhipuGLMProvider(api_key="fake", model="glm-test", transport=up)],
                       state_path=tmp_path/"router.json")
    assert p.chat(MESSAGES, profile="tju-max")["provider"] == "zhipu"
    assert calls == ["tju-llm-max", "tju-llm", "glm-test"]


def test_learning_tool_contract_unchanged_across_max_fallback(routing):
    from core.learning.agent.model.tjullm import TJULLMModel
    from core.learning.agent.model.schema import ModelRequest
    _, selected, _ = routing; selected[0] = "tju-max"; seen=[]
    body={"choices": [{"message": {"content": None, "tool_calls": [{"function": {
        "name": "query_concept", "arguments": '{"concept_id":"ct01"}'}}]}}]}
    def transport(url,payload,headers,timeout):
        seen.append(deepcopy(payload))
        return (429, "rate limited") if len(seen)==1 else (200,json.dumps(body))
    req=ModelRequest(messages=tuple(MESSAGES), tools=({"type":"function","function":{"name":"query_concept"}},),
                     context={"session_id":"existing-session", "progress":0.4})
    before=req.to_dict()
    result=TJULLMModel(api_key="fake", transport=transport).generate(req)
    assert result.tool_calls[0].arguments == {"concept_id":"ct01"}
    assert req.to_dict()==before and seen[0]["tools"]==seen[1]["tools"]


def test_review_key_rotation_then_model_fallback(routing):
    from curriculum_review.provider import RotatingTJUProvider
    router, selected, events=routing; selected[0]="auto"; seen=[]
    def transport(url,payload,headers,timeout):
        seen.append((payload["model"],headers["Authorization"]))
        if payload["model"]=="tju-llm-max":raise ProviderHTTPError(429)
        return completion('{"decision":"KEEP_ORIGINAL"}')
    p=RotatingTJUProvider([(i,TJUQwenProvider(api_key=f"fake-{i}",routing_enabled=False,transport=transport)) for i in (1,2,3)])
    p.chat(MESSAGES)
    assert [m for m,k in seen]==["tju-llm-max"]*3+["tju-llm"]
    assert "fake-" not in json.dumps(events)


def test_authoring_callback_preserves_contract_without_running_book(routing,monkeypatch):
    from tools import authoring_model_router_adapter as adapter
    payloads=[]
    def complete(p,task):
        payloads.append((deepcopy(p),task));return completion()
    monkeypatch.setattr(adapter,"_provider",lambda:SimpleNamespace(complete=complete))
    original=lambda *a:None
    book=SimpleNamespace(_tjullm_chat=original)
    adapter.install_authoring_model_router(book);adapter.install_authoring_model_router(book)
    result=book._tjullm_chat(MESSAGES,[],800)
    assert payloads==[({"messages":MESSAGES,"tools":[],"tool_choice":"auto","max_tokens":800},"authoring")]
    assert result["calls"]==1 and "elapsed" in result
    assert book._firefly_original_model_chat is original


def test_same_runtime_and_history_survive_setting_switch(routing,tmp_path):
    from core.companion_runtime import CompanionRuntime
    from core.companion_config import CompanionConfig,SuggestionSettings
    from core.settings_manager import SettingsManager
    from core.ai_router import ProviderRouter
    router,_,_=routing; settings=SettingsManager(preferences_file=tmp_path/"prefs.json")
    router.selection=lambda:settings.ai_model_profile
    calls=[]
    def transport(url,payload,headers,timeout):
        calls.append(deepcopy(payload));return completion("answer")
    class Store:
        def __init__(self):self.turns=[];self.session_id="existing-session"
        def load_working_window(self):
            return [SimpleNamespace(to_chat_message=lambda msg=m:dict(msg)) for m in self.turns]
        def append_exchange(self,user,assistant):
            self.turns.extend([{"role":"user","content":user},{"role":"assistant","content":assistant}])
    store=Store(); memory=SimpleNamespace(search=lambda *a,**kw:[])
    provider=ProviderRouter([TJUQwenProvider(api_key="fake",transport=transport)],state_path=tmp_path/"state.json")
    runtime=CompanionRuntime(SimpleNamespace(to_system_messages=lambda:[]), memory, store,
                             SimpleNamespace(read=lambda:None),provider,
                             config=CompanionConfig(suggestion=SuggestionSettings(enabled=False)))
    runtime.chat("first")
    settings.set_ai_model_profile("tju-max")
    runtime.chat("second")
    assert [c["model"] for c in calls]==["tju-llm","tju-llm-max"]
    assert {"role":"user","content":"first"} in calls[1]["messages"]
    assert store.session_id=="existing-session" and len(store.turns)==4
    assert runtime.memory_service is memory and runtime.conversation_store is store


@pytest.fixture(scope="session")
def model_qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_system_management_isolated_persistence(model_qapp,tmp_path):
    from core.settings_manager import SettingsManager
    from ui.settings_popover import SettingsPopover
    from core.model_management import ModelManagement
    from core.provider_manager import ProviderManager
    from ui.provider_manager_window import ProviderManagerWindow
    path=tmp_path/"preferences.json"
    path.write_text(json.dumps({"learning_last_course_id":"unchanged","session":"same","memory":"preserved"}))
    settings=SettingsManager(preferences_file=path)
    popover=SettingsPopover(settings,autostart=SimpleNamespace(is_enabled=lambda:False))
    assert not hasattr(popover, "_model_buttons")  # one system management surface
    management = ModelManagement(settings=settings)
    window = ProviderManagerWindow(manager=ProviderManager(model_management=management))
    assert window._policy_combo.currentData() == "tju-stable"
    window._policy_combo.setCurrentIndex(window._policy_combo.findData("tju-max"));model_qapp.processEvents()
    assert SettingsManager(preferences_file=path).ai_model_profile=="tju-max"
    window._policy_combo.setCurrentIndex(window._policy_combo.findData("auto"));model_qapp.processEvents()
    saved=json.loads(path.read_text())
    assert (saved["learning_last_course_id"],saved["session"],saved["memory"])==("unchanged","same","preserved")
    assert saved["ai_model_profile"]=="auto"
    popover.close()
    window.close()


def test_workflow_protocol_and_cancel_survive_profile_selection(routing):
    import threading
    from core.provider_client import WorkflowProviderConfig
    from core.agent_events import AgentEventType
    from ui.workflow_provider_runner import DirectProviderRunner
    _, selected, events = routing
    selected[0] = "tju-max"
    calls = []
    config = WorkflowProviderConfig("https://example.invalid", "fake-token", "existing-workflow-model")
    def transport(endpoint, payload, headers, timeout, cancel):
        calls.append(deepcopy(payload))
        return {"content": [{"type": "text", "text": "plan"}]}
    runner = DirectProviderRunner(config=config, transport=transport)
    result, text = runner._perform("workflow smoke", config, threading.Event())
    assert text == "plan" and result[-1].type == AgentEventType.FINAL
    assert calls[0]["model"] == config.model and "variant" not in calls[0]
    assert events[-1]["profile"] == "protocol-passthrough"
    cancel = threading.Event(); cancel.set()
    result, text = runner._perform("cancelled", config, cancel)
    assert result[-1].type == AgentEventType.CANCELLED and len(calls) == 1


def test_switch_event_file_excludes_sensitive_fields(tmp_path, monkeypatch):
    from core.model_router import emit_model_event
    import core.user_paths
    monkeypatch.setattr(core.user_paths, "get_user_data_paths", lambda: SimpleNamespace(logs=tmp_path))
    emit_model_event({"event": "model_switch_event", "from_profile": "tju-max",
                      "to_profile": "tju-stable", "reason": "ProviderHTTPError",
                      "prompt": "PRIVATE-PROMPT", "api_key": "PRIVATE-KEY", "error": "PRIVATE-ERROR"})
    data = (tmp_path / "model_routing.jsonl").read_text()
    assert "PRIVATE-" not in data and json.loads(data)["to_profile"] == "tju-stable"
