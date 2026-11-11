from __future__ import annotations
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path
from quiz_core import AIAnswer, OpenAIProvider, Question, QuizError, QuizStore, game_day


def q(stem="第1/10题：中国的首都是？", texts=("北京", "上海", "广州", "深圳"), instance="frame-1", scope="test-v1"):
    return Question(stem, tuple(zip("ABCD", texts)), instance, scope)


class FakeProvider:
    def __init__(self, answer="北京", decision="answer"):
        self.text, self.decision = answer, decision
        self.calls = 0
        self.rejected = []
    def answer(self, question, rejected, image=None):
        self.calls += 1
        self.rejected = rejected
        return AIAnswer(self.decision, self.text, "Synthetic fixture only.", "mock", "mock-id", 30, 10)


class QuestionTests(unittest.TestCase):
    def test_two_and_three_options_supported_but_gaps_rejected(self):
        question = Question.from_dict({"stem": "第2/10题：测试？", "options": {"A": "甲", "B": "乙"}, "instance_id": "two"})
        self.assertEqual(tuple(dict(question.options)), ("A", "B"))
        three = Question.from_dict({"stem": "第3/10题：测试？", "options": {"A": "甲", "B": "乙", "C": "丙"}, "instance_id": "three"})
        self.assertEqual(tuple(dict(three.options)), ("A", "B", "C"))
        with self.assertRaises(QuizError):
            Question("测试？", (("A", "甲"), ("B", "乙"), ("D", "丁")), "gap")
    def test_order_invariant(self):
        self.assertEqual(q().key, q(texts=("深圳", "北京", "上海", "广州")).key)
    def test_ordinal_ignored(self):
        self.assertEqual(q().key, q(stem="第9/10题：中国的首都是？").key)
    def test_number_not_ignored(self):
        self.assertNotEqual(q(stem="34级可用？").key, q(stem="35级可用？").key)
    def test_negation_not_ignored(self):
        self.assertNotEqual(q(stem="正确的是？").key, q(stem="不正确的是？").key)
    def test_scope_separation(self):
        self.assertNotEqual(q().key, q(scope="test-v2").key)
    def test_positional_options_not_permuted(self):
        a = q(texts=("北京", "上海", "选项A和B", "都不是"))
        b = q(texts=("上海", "北京", "选项A和B", "都不是"))
        self.assertNotEqual(a.key, b.key)
    def test_duplicate_options_rejected(self):
        with self.assertRaises(QuizError): q(texts=("北京", "北 京", "上海", "广州"))
    def test_non_quiz_scene_rejected(self):
        with self.assertRaises(QuizError):
            Question("test", tuple(zip("ABCD", ["1", "2", "3", "4"])), "1", scene="login_verification")
    def test_game_day_0500_cn(self):
        self.assertEqual(game_day(datetime(2026, 11, 9, 20, 59, tzinfo=timezone.utc)), "2026-11-09")
        self.assertEqual(game_day(datetime(2026, 11, 9, 21, 0, tzinfo=timezone.utc)), "2026-11-10")
    def test_view_instance_changes(self):
        self.assertEqual(q().key, q(instance="another").key)
        self.assertNotEqual(q().view_token, q(instance="another").view_token)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = QuizStore(":memory:")
        self.ai = FakeProvider()
    def tearDown(self): self.store.close()
    def test_cache_miss_without_ai(self):
        self.assertEqual(self.store.resolve(q()).status, "miss")
    def test_switching_mode_cannot_resubmit_same_question_instance(self):
        normal = q()
        first = self.store.resolve(normal, self.ai, allow_trial=True)
        self.assertTrue(self.store.claim_submission(first, normal))
        simplified = Question("第1/10题:随机答题", (("A", "选项A"), ("B", "选项B")), normal.instance_id, normal.scope)
        second = self.store.resolve(simplified, random_answer=True)
        self.assertFalse(self.store.claim_submission(second, simplified))

    def test_legacy_attempts_migrate_without_losing_records(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.execute("""CREATE TABLE attempts(
                id TEXT PRIMARY KEY, qkey TEXT, answer_text TEXT, label TEXT,
                view_token TEXT, eligible INTEGER, origin TEXT, created_at TEXT,
                submitted_at TEXT, outcome TEXT DEFAULT 'pending',
                evidence TEXT DEFAULT '', feedback_source TEXT DEFAULT '')""")
            legacy.execute("INSERT INTO attempts(id,qkey,submitted_at) VALUES('old','old-key','2026-11-11')")
            legacy.commit()
            legacy.close()
            store = QuizStore(path)
            try:
                row = store.db.execute("SELECT id,submitted_at,instance_token FROM attempts").fetchone()
                self.assertEqual(tuple(row), ("old", "2026-11-11", ""))
            finally:
                store.close()
    def test_random_does_not_change_existing_bank_or_use_provider(self):
        original = self.store.resolve(q(), self.ai)
        with patch("quiz_core.random.choice", return_value=q().options[0]):
            decision = self.store.resolve(q(), self.ai, random_answer=True)
        self.assertEqual(decision.origin, "random")
        self.assertTrue(self.store.claim_submission(decision, q()))
        self.store.feedback(decision.attempt_id, "wrong", "Random answer test")
        bank = self.store.resolve(q())
        self.assertEqual((bank.status, bank.answer_text), (original.status, original.answer_text))
        self.assertEqual(self.ai.calls, 1)

    def test_new_question_gets_new_random_choice(self):
        with patch("quiz_core.random.choice", side_effect=[q().options[0], q().options[3]]) as choose:
            first = self.store.resolve(q(), random_answer=True)
            again = self.store.resolve(q(), random_answer=True)
            next_view = self.store.resolve(q(instance="frame-2"), random_answer=True)
        self.assertEqual(first.label, again.label)
        self.assertEqual(next_view.label, "D")
        self.assertEqual(choose.call_count, 2)
    def test_candidate_cached_no_second_call(self):
        a = self.store.resolve(q(), self.ai)
        b = self.store.resolve(q(), self.ai)
        self.assertEqual(a.status, "candidate")
        self.assertFalse(a.can_submit)
        self.assertEqual(b.origin, "cache")
        self.assertEqual(self.ai.calls, 1)
    def test_verified_remaps_letter(self):
        a = self.store.resolve(q(), self.ai)
        self.store.feedback(a.attempt_id, "correct", "Synthetic manual confirmation.")
        b = self.store.resolve(q(texts=("上海", "广州", "深圳", "北京")), self.ai)
        self.assertEqual((b.status, b.label, b.can_submit), ("verified", "D", True))
        self.assertEqual(self.ai.calls, 1)
    def test_unknown_not_verified_or_requeried(self):
        a = self.store.resolve(q(), self.ai)
        self.store.feedback(a.attempt_id, "unknown", "Next question appeared, no correctness signal.")
        b = self.store.resolve(q(), self.ai, allow_trial=True)
        self.assertEqual(b.status, "needs_review")
        self.assertFalse(b.can_submit)
        self.assertEqual(self.ai.calls, 1)
    def test_wrong_quarantined(self):
        a = self.store.resolve(q(), self.ai)
        self.store.feedback(a.attempt_id, "wrong", "Synthetic wrong feedback.")
        b = self.store.resolve(q(), self.ai, allow_trial=True)
        self.assertEqual(b.status, "invalid")
        self.assertFalse(b.can_submit)
        self.assertEqual(self.ai.calls, 1)
    def test_explicit_refresh_receives_rejected_options(self):
        a = self.store.resolve(q(), self.ai)
        self.store.feedback(a.attempt_id, "wrong", "Synthetic wrong feedback.")
        alternative = FakeProvider("上海")
        b = self.store.resolve(q(), alternative, refresh=True)
        self.assertEqual(alternative.rejected, ["北京"])
        self.assertEqual(b.answer_text, "上海")
    def test_rejected_option_cannot_return(self):
        a = self.store.resolve(q(), self.ai)
        self.store.feedback(a.attempt_id, "wrong", "Synthetic wrong feedback.")
        with self.assertRaises(QuizError): self.store.resolve(q(), self.ai, refresh=True)
    def test_nonoption_rejected(self):
        with self.assertRaises(QuizError): self.store.resolve(q(), FakeProvider("天津"))
        self.assertEqual(self.store.resolve(q(), self.ai).status, "needs_review")
        self.assertEqual(self.ai.calls, 0)
    def test_abstain_cached(self):
        ai = FakeProvider("", "abstain")
        a = self.store.resolve(q(), ai)
        b = self.store.resolve(q(), ai)
        self.assertEqual(a.status, "abstain")
        self.assertFalse(b.can_submit)
        self.assertEqual(ai.calls, 1)
    def test_daily_limit(self):
        with self.assertRaises(QuizError): self.store.resolve(q(), self.ai, max_daily_calls=0)
        self.assertEqual(self.ai.calls, 0)
    def test_stale_screen_not_claimed(self):
        a = self.store.resolve(q(), self.ai, allow_trial=True)
        self.assertFalse(self.store.claim_submission(a, q(instance="next-question")))
    def test_swapped_layout_not_claimed(self):
        a = self.store.resolve(q(), self.ai, allow_trial=True)
        self.assertFalse(self.store.claim_submission(a, q(texts=("上海", "北京", "广州", "深圳"))))
    def test_claim_is_one_shot(self):
        a = self.store.resolve(q(), self.ai, allow_trial=True)
        b = self.store.resolve(q(), self.ai, allow_trial=True)
        self.assertTrue(self.store.claim_submission(a, q()))
        self.assertFalse(self.store.claim_submission(a, q()))
        self.assertFalse(self.store.claim_submission(b, q()))
    def test_invalidated_decision_not_claimed(self):
        a = self.store.resolve(q(), self.ai, allow_trial=True)
        self.store.invalidate(q(), "OCR text is suspicious.")
        self.assertFalse(self.store.claim_submission(a, q()))
    def test_unresolved_prior_submission_blocks_new_instance(self):
        a = self.store.resolve(q(), self.ai, allow_trial=True)
        self.assertTrue(self.store.claim_submission(a, q()))
        b = self.store.resolve(q(instance="next"), self.ai, allow_trial=True)
        self.assertFalse(self.store.claim_submission(b, q(instance="next")))
    def test_expired_decision_not_claimed(self):
        a = self.store.resolve(q(), self.ai, allow_trial=True)
        self.assertFalse(self.store.claim_submission(a, q(), max_age_seconds=-1))
    def test_game_feedback_requires_submission(self):
        a = self.store.resolve(q(), self.ai)
        with self.assertRaises(QuizError): self.store.feedback(a.attempt_id, "correct", "fixture", source="game_ui")
    def test_duplicate_feedback_not_counted(self):
        a = self.store.resolve(q(), self.ai)
        self.store.feedback(a.attempt_id, "correct", "fixture")
        with self.assertRaises(QuizError): self.store.feedback(a.attempt_id, "correct", "fixture")
    def test_verified_not_silently_overwritten(self):
        a = self.store.resolve(q(), self.ai)
        self.store.feedback(a.attempt_id, "correct", "fixture")
        with self.assertRaises(QuizError): self.store.resolve(q(), self.ai, refresh=True)
    def test_concurrent_resolve_one_request(self):
        entered, release = threading.Event(), threading.Event()
        outer = self
        class Slow:
            def answer(self, question, rejected, image=None):
                entered.set()
                release.wait(3)
                return outer.ai.answer(question, rejected, image)
        errors = []
        def worker():
            try: outer.store.resolve(q(), Slow())
            except Exception as exc: errors.append(exc)
        t = threading.Thread(target=worker)
        t.start()
        self.assertTrue(entered.wait(2))
        b = self.store.resolve(q(), self.ai)
        self.assertEqual(b.status, "asking")
        release.set()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.ai.calls, 1)


class APIParsingTests(unittest.TestCase):
    def response(self, result):
        return {"status": "completed", "id": "test", "model": "test", "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}],
            "usage": {"input_tokens": 12, "output_tokens": 6}}
    def test_valid_json(self):
        r = OpenAIProvider().parse_response(self.response({"decision": "answer", "answer_text": "北京", "reason_summary": "fixture"}))
        self.assertEqual(r.answer_text, "北京")
        self.assertEqual(r.input_tokens, 12)
    def test_incomplete(self):
        with self.assertRaises(QuizError): OpenAIProvider().parse_response({"status": "incomplete"})
    def test_refusal(self):
        with self.assertRaises(QuizError):
            OpenAIProvider().parse_response({"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "test"}]}]})
    def test_extra_fields_rejected(self):
        with self.assertRaises(QuizError):
            OpenAIProvider().parse_response(self.response({"decision": "answer", "answer_text": "北京", "reason_summary": "fixture", "click": "execute"}))


if __name__ == "__main__": unittest.main()


class ModelConfigurationTests(unittest.TestCase):
    def test_model_from_environment(self):
        with patch.dict("os.environ", {"OPENAI_MODEL": "test-model"}, clear=True):
            self.assertEqual(OpenAIProvider().model, "test-model")

    def test_explicit_model_overrides_environment(self):
        with patch.dict("os.environ", {"OPENAI_MODEL": "environment-model"}, clear=True):
            self.assertEqual(OpenAIProvider("explicit-model").model, "explicit-model")

    def test_missing_model_stops_before_network(self):
        with patch.dict("os.environ", {}, clear=True), patch("quiz_core.request.build_opener") as opener:
            with self.assertRaisesRegex(QuizError, "OPENAI_MODEL"):
                OpenAIProvider().answer(q(), [])
            opener.assert_not_called()

    def test_cache_hit_does_not_require_model(self):
        with patch.dict("os.environ", {}, clear=True):
            store = QuizStore(":memory:")
            try:
                store.resolve(q(), FakeProvider())
                decision = store.resolve(q(), OpenAIProvider())
                self.assertEqual(decision.origin, "cache")
            finally:
                store.close()
