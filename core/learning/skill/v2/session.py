"""Single-owner, event-driven teaching navigation. No formal mastery writes."""
import copy
import hashlib
import json
import logging
import time
from pathlib import Path
from uuid import uuid4
from learning._storage import atomic_write_json
from .catalog import NODES, TOPICS, CATALOG_HASH, path_to, curriculum_evidence

log = logging.getLogger(__name__)
PHASES = {'PROBE', 'PLAN', 'TEACH', 'QUIZ', 'FEEDBACK', 'REMEDIATE', 'PAUSED', 'COMPLETE'}


class SkillStateError(ValueError):
    pass


def checkpoint_path(root, conversation_id):
    key = hashlib.sha256(conversation_id.encode()).hexdigest()[:24]
    return Path(root) / 'skill_v2' / (key + '.json')


class LearningSession:
    def __init__(self, path):
        self.path = Path(path)
        self.state = None
        if self.path.exists():
            try:
                state = json.loads(self.path.read_text(encoding='utf-8'))
                self._validate(state)
                self.state = state
            except Exception as exc:
                raise SkillStateError('CHECKPOINT_INVALID_OR_VERSION_CHANGED') from exc

    @staticmethod
    def _validate(s):
        if (s['schema'] != 2 or s['catalog_hash'] != CATALOG_HASH or s['phase'] not in PHASES
                or type(s['revision']) is not int or s['revision'] < 1
                or s['topic'] not in TOPICS or s['node'] not in NODES
                or s['path'] != path_to(TOPICS[s['topic']][1])
                or s['node'] not in s['path'] or not s['plan']
                or len(set(s['plan'])) != len(s['plan']) or not set(s['plan']) <= set(s['path'])
                or type(s['probe_index']) is not int or not 0 <= s['probe_index'] < len(s['path'])
                or not set(s['evidence']) <= set(s['path'])
                or not set(s['locked']) <= set(s['path'])
                or type(s['attempt']) is not int or s['attempt'] < 0):
            raise SkillStateError('CHECKPOINT_INVALID')
        if s['phase'] == 'PAUSED' and s.get('resume_phase') not in PHASES - {'PAUSED'}:
            raise SkillStateError('INVALID_RESUME_PHASE')

    def _commit(self, candidate, *, new=False):
        # Optimistic revision guard: the UI is the only writer; disk detects a stale owner.
        if self.state is not None:
            disk = json.loads(self.path.read_text(encoding='utf-8'))
            if disk['revision'] != self.state['revision'] or disk['invocation_id'] != self.state['invocation_id']:
                raise SkillStateError('STALE_OWNER')
        candidate['revision'] = (self.state or {}).get('revision', 0) + 1
        self._validate(candidate)
        for attempt in range(3):
            try:
                atomic_write_json(self.path, candidate)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(.02)
        self.state = candidate
        log.info('learning_skill_event invocation=%s revision=%s phase=%s node=%s',
                 candidate['invocation_id'], candidate['revision'], candidate['phase'], candidate['node'])

    def start(self, topic, goal, *, evidence=None):
        if self.state and self.state['phase'] not in {'COMPLETE', 'PAUSED'}:
            raise SkillStateError('ACTIVE_INVOCATION')
        path = path_to(TOPICS[topic][1])
        self._commit({'schema': 2, 'catalog_hash': CATALOG_HASH, 'invocation_id': uuid4().hex,
                      'revision': 0, 'topic': topic, 'goal': goal, 'path': path, 'plan': path[:],
                      'node': path[0], 'probe_index': 0, 'phase': 'PROBE', 'attempt': 0,
                      'evidence': {}, 'locked': [], 'feedback': '', 'teaching': '', 'model_note': '',
                      'curriculum': evidence if evidence is not None else curriculum_evidence(topic)}, new=True)

    @property
    def node(self):
        return NODES[self.state['node']]

    def question(self):
        # Probe uses first item; independent lock-in starts on the other item.
        offset = 0 if self.state['phase'] == 'PROBE' else 1 + self.state['attempt']
        return self.node.questions[offset % len(self.node.questions)]

    def view(self):
        s = self.state
        if not s:
            return None
        result = {k: copy.deepcopy(s[k]) for k in ('goal', 'topic', 'phase', 'revision', 'node', 'plan', 'locked', 'feedback', 'teaching', 'model_note', 'curriculum', 'evidence')}
        result['title'] = self.node.title
        result['claim'] = self.node.claim
        result['explanation'] = s['teaching'] or self.node.explanation
        result['lab'] = self.node.lab
        if s['phase'] in {'PROBE', 'QUIZ'}:
            q = self.question()
            result['question'] = {'text': q.text, 'options': list(q.options), 'id': f"{s['invocation_id']}:{s['revision']}"}
        # Deliberately excludes answer keys and grading rubrics.
        return result

    def event(self, action, revision, *, selection=None, reason='', assessment=None):
        if self.state is None or revision != self.state['revision']:
            raise SkillStateError('STALE_EVENT')
        s = copy.deepcopy(self.state)
        phase = s['phase']
        if action == 'pause' and phase not in {'PAUSED', 'COMPLETE'}:
            s['resume_phase'], s['phase'] = phase, 'PAUSED'
        elif action == 'resume' and phase == 'PAUSED':
            # Resources are verified before resuming a pinned preview.
            current = curriculum_evidence(s['topic'])
            if current.get('sha256') != s['curriculum'].get('sha256'):
                raise SkillStateError('CURRICULUM_VERSION_CHANGED')
            s['phase'] = s.pop('resume_phase')
        elif action == 'answer' and phase in {'PROBE', 'QUIZ'}:
            q = self.question()
            if type(selection) is not int or not 0 <= selection < len(q.options):
                raise SkillStateError('ANSWER_REQUIRED')
            valid_reason = bool(assessment and assessment.get('verdict') == 'supported')
            correct = selection == q.correct
            if phase == 'PROBE':
                status = 'known' if correct and valid_reason else ('edge' if correct else 'unknown')
                s['evidence'][s['node']] = {'status': status, 'reason_checked': valid_reason,
                                           'answer': q.options[selection], 'learner_reason': reason[:1000],
                                           'origin': 'probe', 'feedback': (assessment or {}).get('feedback', '')[:500]}
                if s['probe_index'] + 1 < len(s['path']):
                    s['probe_index'] += 1
                    s['node'] = s['path'][s['probe_index']]
                else:
                    # All unknown prerequisites remain in topological order. Verify the target even if known.
                    s['plan'] = [n for n in s['path'] if s['evidence'][n]['status'] != 'known'] or [s['path'][-1]]
                    s['phase'], s['node'] = 'PLAN', s['plan'][0]
            else:
                if correct and not valid_reason:
                    s['feedback'] = '选项符合题意；理由尚未验证。请解释关键关系后再提交，不会因此标记掌握。'
                    if assessment:
                        s['feedback'] += '\n' + assessment.get('feedback', '')[:500]
                    # Correct selection alone never advances.
                else:
                    locked = correct and valid_reason
                    s['phase'] = 'FEEDBACK'
                    s['feedback'] = ('这一步已通过独立问题验证。' if locked else '这一步还需要补充。') + '\n' + q.reason
                    s['evidence'][s['node']] = {'status': 'locked_in' if locked else 'edge', 'reason_checked': valid_reason,
                                               'answer': q.options[selection], 'learner_reason': reason[:1000],
                                               'question': q.text,
                                               'origin': 'lock_in', 'feedback': (assessment or {}).get('feedback', '')[:500]}
                    if locked and s['node'] not in s['locked']:
                        s['locked'].append(s['node'])
        elif action == 'confirm_plan' and phase == 'PLAN':
            s['phase'] = 'TEACH'
        elif action == 'quiz' and phase == 'TEACH':
            s['phase'], s['feedback'] = 'QUIZ', ''
        elif action == 'next' and phase == 'FEEDBACK':
            if s['node'] in s['locked']:
                index = s['plan'].index(s['node']) + 1
                if index == len(s['plan']):
                    s['phase'] = 'COMPLETE'
                else:
                    s['node'], s['phase'], s['attempt'] = s['plan'][index], 'TEACH', 0
                    s['teaching'] = ''
            else:
                s['attempt'] += 1
                s['phase'] = 'REMEDIATE'
                if s['attempt'] >= 2:
                    s['resume_phase'], s['phase'] = 'REMEDIATE', 'PAUSED'
                    s['feedback'] += '\n先停一下。可继续换一种解释，或退出后换目标；不会自动刷下一题。'
        elif action == 'retry' and phase == 'REMEDIATE':
            if s['attempt'] >= len(self.node.questions):
                raise SkillStateError('FRESH_QUESTION_REQUIRED')
            s['phase'], s['teaching'] = 'TEACH', ''
        else:
            raise SkillStateError('EVENT_NOT_ALLOWED')
        self._commit(s)
        return self.view()

    def annotate(self, revision, *, text, note):
        if revision != self.state['revision'] or self.state['phase'] not in {'PLAN', 'TEACH', 'REMEDIATE'}:
            raise SkillStateError('STALE_MODEL_RESULT')
        s = copy.deepcopy(self.state)
        if s['phase'] == 'PLAN':
            s['model_note'] = text[:1200]
        else:
            s['teaching'] = text[:1800]
        s['model_note'] = (s['model_note'] + '\n' + note).strip()
        self._commit(s)

    def plan_mermaid(self):
        return 'flowchart LR\n' + '\n'.join(f' n{i}["{NODES[n].title}"]' for i, n in enumerate(self.state['plan'])) + '\n' + '\n'.join(f' n{i} --> n{i+1}' for i in range(len(self.state['plan'])-1))
