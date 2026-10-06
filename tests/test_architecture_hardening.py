import ast
from pathlib import Path
import pytest
from tools.check_architecture_invariants import check, inspect_source, ROOT


def test_production_architecture():
    assert check() == []


@pytest.mark.parametrize('path,source', [
    ('ui/memory_panel.py', 'memory_service.delete(record_id)'),
    ('ui/memory_manager.py', 'self._service.clear_all()'),
    ('core/learning/skill/new_owner.py', 'from memory.service import MemoryService'),
    ('core/learning/skill/v2/new_owner.py', 'path.write_text(data)'),
    ('core/learning/new_business.py', 'from providers.tju_qwen import TJUQwenProvider'),
    ('ui/new_teaching.py', 'import requests'),
    ('character/new_importer.py', 'archive.extractall(root)'),
    ('learning/new_transport.py', 'client.call_tool(name, args)'),
    ('core/learning/new_writer.py', 'service.remember(text, asserted_explicit=True)'),
])
def test_violation_examples_are_caught(path, source):
    assert inspect_source(path, source)


def test_memory_action_enum_exhaustive():
    from memory.m2 import WriteAction
    tree = ast.parse((ROOT/'memory/service.py').read_text(encoding='utf-8'))
    fn = next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='remember_detailed')
    handled = {n.attr for n in ast.walk(fn) if isinstance(n,ast.Attribute) and isinstance(n.value,ast.Name) and n.value.id=='M2Action'}
    assert set(WriteAction.__members__) == handled


def test_shared_learning_intent_seams():
    for name in ('ui/v2/tutor_chat.py','core/learning/skill/v2/catalog.py','core/video_study.py'):
        tree = ast.parse((ROOT/name).read_text(encoding='utf-8'))
        assert any(isinstance(n,ast.ImportFrom) and n.module=='core.learning.intents' for n in ast.walk(tree))
        assignments = [t.id for n in ast.walk(tree) if isinstance(n,ast.Assign) for t in n.targets if isinstance(t,ast.Name)]
        assert not {'_LEARN_KEYWORDS','_QUIZ_MARKERS','_DEFAULT_CONCEPT'} & set(assignments)


def test_mcp_unregistered_refused_before_io():
    from learning.teach_mcp_stdio import StdioMcpTransport, McpTransportError
    transport=StdioMcpTransport('unused')
    transport._process=object()
    def forbidden(*a,**k): raise AssertionError('unregistered tool reached IO')
    transport._request=forbidden
    with pytest.raises(McpTransportError, match='not registered'): transport.call_tool('untrusted_shell', {})
    transport._process=None


@pytest.mark.parametrize('text', ['自动控制原理', '考考我', '继续学习', '我不理解状态空间'])
def test_unresolved_course_cannot_be_hijacked_by_default_tutor(text):
    from ui.v2.tutor_chat import TutorChatRouter, ACTION_PASSTHROUGH
    class NoDefaultExecutor:
        def start(self, plan):
            raise AssertionError('unrelated default concept was started')
    assert TutorChatRouter(NoDefaultExecutor()).route(text).action == ACTION_PASSTHROUGH
    from core.learning.intents import parse_learning_intent, LearningIntent
    if text == '考考我':
        assert parse_learning_intent(text).intent == LearningIntent.QUIZ_REQUEST
    if text == '继续学习':
        assert parse_learning_intent(text).intent == LearningIntent.CONTINUE_LEARNING
    if text == '我不理解状态空间':
        from core.learning.skill.v2.catalog import detect
        assert detect(text) == 'state'
