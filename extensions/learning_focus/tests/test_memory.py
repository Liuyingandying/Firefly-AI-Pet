"""LearningMemory 单元测试：画像往返、证据折算、误解生命周期、事件日志。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from learning_focus.learner.evaluator import Evaluator  # noqa: E402
from learning_focus.learner.profile import LearnerProfile  # noqa: E402
from learning_focus.learner.state import KnowledgeState  # noqa: E402
from learning_focus.memory.learning_memory import LearningMemory  # noqa: E402
from learning_focus.planner.knowledge_graph import AUTOMATIC_CONTROL_GRAPH  # noqa: E402

GRAPH = AUTOMATIC_CONTROL_GRAPH


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = LearningMemory(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_profile_roundtrip(self):
        profile = LearnerProfile(learner_id="u1", goals=["自动控制原理"])
        profile.set_state(
            KnowledgeState(node_id="laplace", mastery=0.85, confidence=0.8, session_count=5)
        )
        profile.add_misconception("transfer_function", "无法理解零极点", source="seed")
        self.memory._profile = profile
        self.memory.save_profile()

        loaded = LearningMemory(self.tmp.name).load_profile()
        self.assertEqual(loaded.learner_id, "u1")
        self.assertEqual(loaded.goals, ["自动控制原理"])
        self.assertAlmostEqual(loaded.knowledge["laplace"].mastery, 0.85)
        self.assertEqual(len(loaded.misconceptions), 1)

    def test_default_profile_created_when_missing(self):
        profile = self.memory.load_profile(learner_id="demo")
        self.assertEqual(profile.learner_id, "demo")
        self.assertEqual(profile.knowledge, {})

    def test_events_appended_and_read(self):
        self.memory.append_event({"type": "goal", "goal": "x"})
        self.memory.append_event({"type": "evidence", "node_id": "n"})
        events = self.memory.load_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["type"], "goal")


class TestApplyEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = LearningMemory(self.tmp.name)
        self.memory.load_profile(learner_id="demo")
        self.evaluator = Evaluator()

    def tearDown(self):
        self.tmp.cleanup()

    def test_correct_updates_mastery_and_persists(self):
        st = KnowledgeState(node_id="poles_zeros", mastery=0.4, session_count=2, last_seen_at="2026-08-30T00:00:00")
        self.memory.profile.set_state(st)
        ev = self.evaluator.evaluate("poles_zeros", "零点", "零点")
        upd = self.memory.apply_evidence(ev, graph=GRAPH)
        self.assertGreater(upd["mastery_after"], upd["mastery_before"])
        # 落盘后可从磁盘读到新掌握度
        reloaded = LearningMemory(self.tmp.name).load_profile()
        self.assertAlmostEqual(reloaded.knowledge["poles_zeros"].mastery, upd["mastery_after"])

    def test_incorrect_records_misconception(self):
        ev = self.evaluator.evaluate("poles_zeros", "错误答案", "零点")
        self.memory.apply_evidence(ev, graph=GRAPH)
        open_ones = self.memory.profile.unresolved_misconceptions("poles_zeros")
        self.assertEqual(len(open_ones), 1)
        self.assertIn("poles_zeros", self.memory.profile.knowledge)

    def test_cross_node_misconception_closed_by_node_name(self):
        # 误解挂在 transfer_function 上，描述提及"零极点"
        self.memory.profile.set_state(
            KnowledgeState(node_id="transfer_function", mastery=0.4, session_count=2)
        )
        self.memory.profile.add_misconception("transfer_function", "无法理解零极点", source="seed")
        # 答对零极点 -> 描述中含"零极点"的误解一并解决
        ev = self.evaluator.evaluate("poles_zeros", "零点", "零点")
        self.memory.apply_evidence(ev, graph=GRAPH)
        self.assertEqual(
            len(self.memory.profile.unresolved_misconceptions("transfer_function")), 0
        )

    def test_correct_resolves_own_node_misconception(self):
        self.memory.profile.add_misconception("poles_zeros", "概念混淆", source="seed")
        ev = self.evaluator.evaluate("poles_zeros", "零点", "零点")
        self.memory.apply_evidence(ev, graph=GRAPH)
        self.assertEqual(len(self.memory.profile.unresolved_misconceptions("poles_zeros")), 0)


if __name__ == "__main__":
    unittest.main()
