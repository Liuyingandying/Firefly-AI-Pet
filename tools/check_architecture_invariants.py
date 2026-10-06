"""Offline architecture gates for production boundaries (no imports of the app).

This checks structural seams, not all Python semantics. Behavioral tests prove
denial and confirmation; this gate prevents new bypass call sites entering CI.
"""
from __future__ import annotations
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Existing protocol adapters own transport parsing, not business decisions.
# Their routed transport is separately required below; no directory wildcards.
PROVIDER_ADAPTERS = {
    'core/learning/agent/model/tjullm.py': 'typed tool-call adapter via ModelRouter.request',
    'curriculum_review/provider.py': 'ProviderManager key discovery and review routing',
    'ui/workflow_provider_runner.py': 'Anthropic protocol adapter via ModelRouter.passthrough',
    'ui/tju_llm_health.py': 'explicit provider health diagnostic; no teaching/chat state',
}


def inspect_source(path: str, source: str) -> list[str]:
    tree = ast.parse(source)
    issues = []
    business = path.startswith(('core/learning/', 'curriculum_review/', 'learning/', 'ui/'))
    skill_view = path.startswith(('core/learning/skill/v2/', 'ui/v2/learning_skill'))
    for node in ast.walk(tree):
        modules = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                   [a.name for a in node.names] if isinstance(node, ast.Import) else [])
        for module in modules:
            if path.startswith('core/learning/') and (module == 'memory' or module.startswith('memory.')):
                issues.append('Learning cannot import Memory owners')
            if business and path not in PROVIDER_ADAPTERS and module.startswith('providers.') and module != 'providers.base':
                issues.append('Business code cannot import concrete providers')
            if business and path not in PROVIDER_ADAPTERS and module in ('requests', 'httpx', 'urllib.request', 'openai', 'anthropic'):
                issues.append('Business code cannot own HTTP model transport')
        if isinstance(node, ast.Call):
            call = ast.unparse(node.func)
            if path.startswith('core/') and any(k.arg == 'asserted_explicit' and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords):
                issues.append('Machine code cannot assert explicit Memory consent')
            if skill_view and path != 'core/learning/skill/v2/session.py' and call.split('.')[-1] in ('atomic_write_json','write_text','write_bytes','MemoryService','JsonMemoryRepository'):
                issues.append('Skill view cannot create a second persistent owner')
            if path.startswith(('character/', 'skins/', 'tools/character_package/')) and isinstance(node.func, ast.Attribute) and node.func.attr in ('extract', 'extractall'):
                issues.append('Archive extraction must use the shared validator')
            if path in ('ui/memory_panel.py', 'ui/memory_manager.py') and isinstance(node.func, ast.Attribute) and node.func.attr in ('delete','clear_all','edit_memory','resolve_conflict'):
                receiver = node.func.value
                if not isinstance(receiver, ast.Call) or ast.unparse(receiver.func).split('.')[-1] not in ('confirmed_memory_handle','_confirmed_handle'):
                    issues.append('Memory UI mutation must use a confirmed handle')
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'call_tool' and business and path not in ('learning/learning_worker.py',):
                issues.append('New raw MCP call site: use the registered transport adapter')
    if path in ('core/learning/agent/model/tjullm.py','ui/workflow_provider_runner.py'):
        if not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr in ('request','passthrough')
                   and 'get_model_router()' in ast.unparse(n.func.value) for n in ast.walk(tree)):
            issues.append('Protocol adapter lost ModelRouter dispatch')
    return issues


def check(root=ROOT):
    errors = []
    for folder in ('core','curriculum_review','learning','ui','character','skins','tools/character_package'):
        for path in (root/folder).rglob('*.py'):
            rel = path.relative_to(root).as_posix()
            errors.extend(f'{rel}: {issue}' for issue in inspect_source(rel,path.read_text(encoding='utf-8-sig')))
    return errors


if __name__ == '__main__':
    errors = check()
    print('\n'.join(errors) if errors else 'Architecture invariants: PASS')
    raise SystemExit(bool(errors))
