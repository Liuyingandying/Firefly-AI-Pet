"""Command facade over existing owners. Returned metadata is allowlisted."""
from __future__ import annotations
import os
import sqlite3
from contextlib import closing
from core.model_management import ModelManagement
from core.provider_manager import ProviderManager
from core.settings_manager import SettingsManager
from core.user_paths import get_user_data_paths

COMMANDS = frozenset({'status', 'model.list', 'model.status', 'model.use',
                      'skill.list', 'learning.status', 'session.list', 'doctor'})


class ControlError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ControlService:
    def __init__(self, *, shell=None, settings=None, providers=None, paths=None):
        self.shell = shell
        self.settings = settings if settings is not None else (shell.settings if shell is not None else SettingsManager())
        self.models = ModelManagement(settings=self.settings)
        self.providers = providers if providers is not None else ProviderManager(model_management=self.models)
        self.paths = paths if paths is not None else get_user_data_paths()

    def execute(self, command, profile=None):
        if command not in COMMANDS:
            raise ControlError('UNKNOWN_COMMAND')
        if command == 'model.use':
            if self.shell is None:
                raise ControlError('RUNTIME_REQUIRED')
            # Resolve aliases through the one Router resource registry.
            if profile == 'auto':
                selected = 'auto'
            else:
                selected = self.models.router.resolve(model=profile).name if isinstance(profile, str) else ''
                if selected not in self.models.router.profiles:
                    raise ControlError('UNKNOWN_PROFILE')
            self.models.set_policy(selected)
            window = getattr(self.shell, '_provider_manager_window', None)
            if window is not None:
                window.refresh()
            return self.model_status()
        if profile is not None:
            raise ControlError('UNEXPECTED_ARGUMENT')
        return {
            'status': self.status, 'model.list': lambda: {'profiles': self.models.resources(self.providers)},
            'model.status': self.model_status, 'skill.list': self.skills,
            'learning.status': self.learning, 'session.list': self.sessions, 'doctor': self.doctor,
        }[command]()

    def runtime(self):
        return {'connected': self.shell is not None, 'pid': os.getpid() if self.shell is not None else None,
                'source': 'live_runtime' if self.shell is not None else 'offline_read_only'}

    def model_status(self):
        window = getattr(self.shell, '_provider_manager_window', None)
        return {'policy': self.models.policy, 'default': self.models.router.default,
                'task_plan': self.models.task_plan(),
                'gui_policy': window._policy_combo.currentData() if window is not None else None,
                'gui_management_open': bool(window is not None and window.isVisible())}

    def _runner(self):
        return getattr(self.shell, 'character_conversation', None)

    def sessions(self):
        from core.conversation_store import ConversationStore
        runtime = getattr(self._runner(), 'runtime', None)
        store = getattr(runtime, 'conversation_store', None)
        live = store is not None
        if not live:
            store = ConversationStore(path=self.paths.conversation / 'conversation.json')
        return {'source': 'live_store' if live else 'persisted_store',
                'active_session_id': store.active_session_id if live else None,
                'session_ids': store.list_sessions()}

    def memory(self):
        return {'runtime_attached': getattr(self._runner(), 'memory_service', None) is not None,
                'records_present': (self.paths.memory / 'memory_records.json').is_file(),
                'history_present': (self.paths.memory / 'history.db').is_file()}

    def learning(self):
        db = self.paths.learning / 'learning_store.sqlite3'
        result = {'database_present': db.is_file(), 'database_status': 'missing', 'counts': {}}
        if db.is_file():
            try:
                with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.25)) as conn:
                    for table in ('courses', 'concepts', 'study_sessions', 'assessment_records'):
                        result['counts'][table] = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                result['database_status'] = 'readable'
            except sqlite3.Error:
                result['database_status'] = 'unavailable_or_incompatible'
        state = getattr(getattr(self._runner(), 'learning_controller', None), 'state', None)
        result['runtime_context'] = None if state is None else {
            name: getattr(state, name, None) for name in ('enabled', 'active_course_id', 'active_session_id')}
        result['last_course_id'] = self.settings.learning_last_course_id
        result['interaction'] = self.interaction()
        # Draft navigation and official mastery have different owners; do not infer
        # progress from counts or write a LearningStore merely to query it.
        return result

    def interaction(self):
        reference = getattr(self.shell, '_interaction_snapshot', None)
        try:
            reader = reference() if callable(reference) else None
            if reader is not None:
                return reader()
        except RuntimeError:
            # A Qt Console can close between binding and this read.
            pass
        activity = getattr(getattr(self.shell, 'runtime_state_aggregator', None), 'current', None)
        return {'source':'console_unavailable' if self.shell is not None else 'offline_read_only',
                'owner':None, 'conversation_id':None,
                'runtime_activity':getattr(activity, 'value', None), 'skill_session':None}

    def skills(self):
        from learning.skill_installer import DEFAULT_SKILLS_ROOT, SKILL_NAME, SKILL_SOURCE_DIR, skill_version
        installed = {p.parent.name for p in DEFAULT_SKILLS_ROOT.glob('*/SKILL.md')}
        rows = [{'id': name, 'installed': True, 'scope': 'zcode_skill_manifest',
                 'active': 'unverified'} for name in sorted(installed)]
        if SKILL_NAME not in installed:
            rows.append({'id': SKILL_NAME, 'installed': False, 'scope': 'firefly_bundled', 'active': 'unverified'})
        for row in rows:
            if row['id'] == SKILL_NAME:
                bundled = (SKILL_SOURCE_DIR / 'SKILL.md').is_file()
                row.update(bundled=bundled, version=skill_version() if bundled else None)
        return {'discovery': 'existing skill_installer paths; no installation or execution', 'skills': rows}

    def mcp(self):
        from tools.teach_mcp_provider_launcher import DEFAULT_SERVER
        return {'teach_mcp_server_present': DEFAULT_SERVER.is_file(),
                'firefly_bridge_attached': getattr(self.shell, '_learning_bridge_session', None) is not None,
                'protocol_health': 'not_probed', 'external_process_state': 'unknown'}

    def status(self):
        sessions = self.sessions()
        return {'runtime': self.runtime(), 'model': self.model_status(), 'memory': self.memory(),
                'interaction': self.interaction(),
                'session_count': len(sessions['session_ids']), 'active_session_id': sessions['active_session_id']}

    def doctor(self):
        return {'runtime': self.runtime(), 'providers': self.providers.list_providers(),
                'model': self.model_status(), 'model_resources': self.models.resources(self.providers),
                'skills': self.skills(), 'mcp': self.mcp(), 'memory': self.memory(), 'learning': self.learning(),
                'network_probes': False}
