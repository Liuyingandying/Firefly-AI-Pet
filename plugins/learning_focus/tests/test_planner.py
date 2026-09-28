"""LearningPlanner 单元测试：误解门控、目标解析、规划排序。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from learning_focus.learner.profile import LearnerProfile  # noqa: E402
from learning_focus.learner.state import KnowledgeState  # noqa: E402
from learning_focus.planner.knowledge_graph import AUTOMATIC_CONTROL_GRAPH  # noqa: E402
from learning_focus.planner.planner import LearningPlanner, summarize_state  # noqa: E402

GRAPH = AUTOMATIC_CONTROL_GRAPH
NOW = "2026-09-01T00:00:00"


def empty_profile() -> LearnerProfile:
    return LearnerProfile(learner_id="t")


class TestPlannerBasics(unittest.TestCase):
    def setUp(self):
        self.planner = LearningPlanner()

    def test_unseen_nodes_get_diagnose(self):
        profile = empty_profile()
        plan = self.planner.plan(profile, GRAPH, now=NOW)
        actions = {it.node_id: it.action for it in plan}
        self.assertEqual(actions["laplace"], "diagnose")
        self.assertEqual(actions["transfer_function"], "diagnose")

    def test_stable_node_excluded(self):
        profile = empty_profile()
        profile.set_state(
            KnowledgeState(
                node_id="laplace", mastery=0.9, confidence=0.8,
                stability_days=21.0, session_count=6, status="stable",
            )
        )
        plan = self.planner.plan(profile, GRAPH, now=NOW)
        node_ids = [it.node_id for it in plan]
        self.assertNotIn("laplace", node_ids)

    def test_weak_seen_node_gets_quiz(self):
        profile = empty_profile()
        profile.set_state(
            KnowledgeState(
                node_id="transfer_function", mastery=0.5, confidence=0.4,
                stability_days=7.0, session_count=2, last_seen_at="2026-08-30T00:00:00",
            )
        )
        plan = self.planner.plan(profile, GRAPH, now=NOW)
        item = next(it for it in plan if it.node_id == "transfer_function")
        self.assertEqual(item.action, "quiz")

    def test_misconception_gate_teach_first(self):
        profile = empty_profile()
        profile.set_state(
            KnowledgeState(
                node_id="transfer_function", mastery=0.4, confidence=0.3,
                stability_days=7.0, session_count=2, last_seen_at="2026-08-30T00:00:00",
            )
        )
        profile.add_misconception("transfer_function", "无法理解零极点", source="seed")
        plan = self.planner.plan(profile, GRAPH, now=NOW)
        top = plan[0]
        self.assertEqual(top.action, "teach")
        # 误解描述提到"零极点" -> 教学目标解析为 poles_zeros 节点
        self.assertEqual(top.target_id, "poles_zeros")


class TestSummarizeState(unittest.TestCase):
    def test_grouping(self):
        profile = empty_profile()
        profile.set_state(
            KnowledgeState(
                node_id="laplace", mastery=0.85, confidence=0.8,
                stability_days=14.0, session_count=5, status="stable",
            )
        )
        profile.set_state(
            KnowledgeState(
                node_id="transfer_function", mastery=0.4, confidence=0.3,
                stability_days=7.0, session_count=2, last_seen_at="2026-08-30T00:00:00",
            )
        )
        s = summarize_state(profile, GRAPH)
        self.assertIn("拉普拉斯变换", s["mastered"])
        self.assertIn("传递函数", s["weak"])
        self.assertIn("零极点", s["unseen"])


if __name__ == "__main__":
    unittest.main()
