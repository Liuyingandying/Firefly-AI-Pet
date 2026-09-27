"""StateEngine 单元测试：证据折算、可提取性、复习到期、状态派生。"""

import unittest

from learning_focus.learner.state import (
    KnowledgeState,
    REVIEW_RETRIEVABILITY_THRESHOLD,
    StateEngine,
)


class TestRetrievability(unittest.TestCase):
    def setUp(self):
        self.engine = StateEngine()

    def test_immediately_after_review_full(self):
        r = self.engine.retrievability("2026-09-01T00:00:00", 7.0, "2026-09-01T00:00:00")
        self.assertAlmostEqual(r, 1.0, places=6)

    def test_decays_over_time(self):
        r7 = self.engine.retrievability("2026-09-01T00:00:00", 7.0, "2026-09-08T00:00:00")
        self.assertAlmostEqual(r7, 0.9, places=6)

    def test_stability_slows_decay(self):
        r_long = self.engine.retrievability("2026-09-01T00:00:00", 28.0, "2026-09-08T00:00:00")
        r_short = self.engine.retrievability("2026-09-01T00:00:00", 7.0, "2026-09-08T00:00:00")
        self.assertGreater(r_long, r_short)

    def test_days_until_review_matches_threshold(self):
        days = self.engine.days_until_review(7.0)
        r = 0.9 ** (days / 7.0)
        self.assertAlmostEqual(r, REVIEW_RETRIEVABILITY_THRESHOLD, places=6)


class TestApplyEvidence(unittest.TestCase):
    def setUp(self):
        self.engine = StateEngine()

    def test_correct_raises_mastery(self):
        st = KnowledgeState(node_id="n", mastery=0.4)
        self.engine.apply_evidence(st, "correct", "2026-09-01T00:00:00")
        # 0.4 + 0.6*(0.85-0.4) = 0.67
        self.assertAlmostEqual(st.mastery, 0.67, places=6)
        self.assertEqual(st.session_count, 1)
        self.assertGreater(st.stability_days, 7.0)

    def test_incorrect_lowers_mastery_and_stability(self):
        st = KnowledgeState(node_id="n", mastery=0.6, stability_days=21.0)
        self.engine.apply_evidence(st, "incorrect", "2026-09-01T00:00:00")
        # 0.6 + 0.6*(0.15-0.6) = 0.33
        self.assertAlmostEqual(st.mastery, 0.33, places=6)
        self.assertAlmostEqual(st.stability_days, 12.6, places=6)

    def test_mastery_stays_in_bounds(self):
        st = KnowledgeState(node_id="n", mastery=0.0)
        for _ in range(20):
            self.engine.apply_evidence(st, "correct", "2026-09-01T00:00:00")
        self.assertLessEqual(st.mastery, 1.0)
        self.assertGreaterEqual(st.mastery, 0.0)

    def test_review_due_at_computed(self):
        st = KnowledgeState(node_id="n", mastery=0.5)
        self.engine.apply_evidence(st, "correct", "2026-09-01T00:00:00")
        self.assertIsNotNone(st.review_due_at)
        self.assertGreater(st.review_due_at, "2026-09-01T00:00:00")


class TestDeriveStatus(unittest.TestCase):
    def setUp(self):
        self.engine = StateEngine()

    def test_unknown_for_fresh_node(self):
        st = KnowledgeState(node_id="n")
        self.assertEqual(self.engine.derive_status(st, "2026-09-01T00:00:00"), "unknown")

    def test_fragile_below_threshold(self):
        st = KnowledgeState(node_id="n", mastery=0.4, session_count=2, last_seen_at="2026-08-01T00:00:00")
        self.assertEqual(self.engine.derive_status(st, "2026-09-01T00:00:00"), "fragile")

    def test_stable_when_high_mastery_confidence(self):
        st = KnowledgeState(node_id="n", mastery=0.9, confidence=0.8, session_count=5, last_seen_at="2026-08-30T00:00:00")
        self.assertEqual(self.engine.derive_status(st, "2026-09-01T00:00:00"), "stable")


if __name__ == "__main__":
    unittest.main()
