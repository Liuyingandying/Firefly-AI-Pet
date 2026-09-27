"""Evaluator 单元测试：确定性判定（精确/部分/错误）。"""

import unittest

from learning_focus.learner.evaluator import Evaluator, deterministic_judge


class TestDeterministicJudge(unittest.TestCase):
    def test_exact_match_correct(self):
        outcome, errors = deterministic_judge("零点", "零点")
        self.assertEqual(outcome, "correct")
        self.assertEqual(errors, [])

    def test_whitespace_and_punct_insensitive(self):
        outcome, _ = deterministic_judge(" 零点， 极点 ", "零点,极点")
        self.assertEqual(outcome, "correct")

    def test_core_word_partial(self):
        # 期望答案按标点切分为两个分句；用户只答出其中一句 -> partial
        expected = "使分子为零的点称为零点，使分母为零的点称为极点"
        outcome, _ = deterministic_judge("使分子为零的点称为零点", expected)
        self.assertEqual(outcome, "partial")

    def test_empty_answer_incorrect(self):
        outcome, errors = deterministic_judge("", "零点")
        self.assertEqual(outcome, "incorrect")
        self.assertTrue(errors)

    def test_irrelevant_answer_incorrect_with_error(self):
        outcome, errors = deterministic_judge("不知道", "零点")
        self.assertEqual(outcome, "incorrect")
        self.assertTrue(errors)


class TestEvaluator(unittest.TestCase):
    def test_evaluate_produces_evidence(self):
        ev = Evaluator().evaluate("n1", "零点", "零点", detail="q1")
        self.assertEqual(ev.node_id, "n1")
        self.assertEqual(ev.outcome, "correct")
        self.assertIn("at", ev.to_dict())

    def test_custom_judge_adapter(self):
        judge = lambda u, e: ("correct", []) if u == "ok" else ("incorrect", ["x"])
        ev = Evaluator(judge=judge).evaluate("n1", "bad", "ok")
        self.assertEqual(ev.outcome, "incorrect")
        self.assertEqual(ev.misconceptions, ["x"])


if __name__ == "__main__":
    unittest.main()
