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
            legacy.execute("""INSERT INTO attempts(id,qkey,answer_text,label,view_token,eligible,origin,created_at)
                VALUES('old-random',?,'北京','A',?,1,'random','2026-11-11')""", (q().key, q().view_token))
            legacy.execute("""CREATE TABLE answers(
                qkey TEXT PRIMARY KEY, scope TEXT NOT NULL, stem TEXT NOT NULL,
                options_json TEXT NOT NULL, answer_text TEXT, status TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)""")
            legacy.execute("""INSERT INTO answers(qkey,scope,stem,options_json,answer_text,status,updated_at)
                VALUES('preserved','test','Old question','{}','Old answer','verified','old-date')""")
            legacy.commit()
            legacy.close()
            for _ in range(2):
                store = QuizStore(path)
                try:
                    row = store.db.execute("SELECT id,submitted_at,instance_token,scope,stem,options_json FROM attempts WHERE id='old'").fetchone()
                    self.assertEqual(tuple(row), ("old", "2026-11-11", "", "", "", ""))
                    preserved = store.db.execute("SELECT answer_text,status,updated_at FROM answers WHERE qkey='preserved'").fetchone()
                    self.assertEqual(tuple(preserved), ("Old answer", "verified", "old-date"))
                    if store.db.execute("SELECT outcome FROM attempts WHERE id='old-random'").fetchone()[0] == "pending":
                        store.feedback("old-random", "correct", "Missing historical metadata")
                    self.assertEqual(store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 1)
                    self.assertEqual(store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)
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
    def test_mixed_percentage_boundaries_and_no_provider(self):
        candidate = self.store.resolve(q(), self.ai)
        self.store.feedback(candidate.attempt_id, "correct", "Verified fixture")
        with patch("quiz_core.random.randrange") as strategy, patch("quiz_core.random.choice", return_value=q().options[3]) as choose:
            bank = self.store.resolve(q(instance="bank"), self.ai, random_percentage=0,
                                      refresh=True, max_daily_calls=0)
            random = self.store.resolve(q(instance="random"), self.ai, random_percentage=100)
        self.assertEqual((bank.origin, bank.status, bank.label, bank.can_submit), ("bank", "verified", "A", True))
        self.assertEqual((random.origin, random.label, random.can_submit), ("random", "D", True))
        strategy.assert_not_called()
        self.assertEqual(choose.call_count, 1)
        self.assertEqual(self.ai.calls, 1)

    def test_mixed_percentage_validation(self):
        for value in (-1, 101, True, False, 1.0, "50"):
            with self.subTest(value=value), self.assertRaises(QuizError):
                self.store.resolve(q(), self.ai, random_percentage=value)
        self.assertEqual(self.ai.calls, 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

    def test_mixed_percentage_is_strategy_probability_not_correctness(self):
        candidate = self.store.resolve(q(), self.ai)
        self.store.feedback(candidate.attempt_id, "correct", "Verified fixture")
        with patch("quiz_core.random.randrange", side_effect=range(100)) as strategy, patch("quiz_core.random.choice", return_value=q().options[0]) as choose:
            decisions = [self.store.resolve(q(instance=str(i)), self.ai, random_percentage=50) for i in range(100)]
        self.assertEqual(sum(d.origin == "random" for d in decisions), 50)
        self.assertEqual(sum(d.origin == "bank" for d in decisions), 50)
        self.assertTrue(all(d.answer_text == "北京" for d in decisions))
        self.assertEqual(strategy.call_count, 100)
        self.assertEqual(choose.call_count, 50)
        self.assertEqual(self.ai.calls, 1)

    def test_mixed_fallback_for_missing_unverified_or_unmappable_bank(self):
        with patch("quiz_core.random.choice", return_value=q().options[1]):
            missing = self.store.resolve(q(instance="missing"), self.ai, random_percentage=0)
        self.assertEqual(missing.origin, "random")
        candidate = self.store.resolve(q(), self.ai)
        for status, answer in (("candidate", "北京"), ("needs_review", "北京"), ("invalid", "北京"),
                               ("abstain", None), ("asking", None), ("verified", "天津")):
            with self.subTest(status=status), patch("quiz_core.random.choice", return_value=q().options[1]):
                self.store.db.execute("UPDATE answers SET status=?,answer_text=? WHERE qkey=?", (status, answer, candidate.question_key))
                decision = self.store.resolve(q(instance=status), self.ai, random_percentage=0, allow_trial=True)
                self.assertEqual((decision.origin, decision.label, decision.can_submit), ("random", "B", True))
        self.assertEqual(self.ai.calls, 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)

    def test_mixed_repeated_random_and_bank_decisions_do_not_reroll(self):
        with patch("quiz_core.random.choice", return_value=q().options[3]) as choose:
            first = self.store.resolve(q(), random_answer=True)
            with patch("quiz_core.random.randrange") as strategy:
                again = self.store.resolve(q(), self.ai, random_percentage=0)
            self.assertEqual((again.attempt_id, again.answer_text, again.origin), (first.attempt_id, "深圳", "random"))
        strategy.assert_not_called()
        self.assertEqual(choose.call_count, 1)
        self.store.feedback(first.attempt_id, "correct", "Verified random fixture")
        current = q(instance="bank")
        bank = self.store.resolve(current, random_percentage=0)
        with patch("quiz_core.random.randrange") as strategy, patch("quiz_core.random.choice") as choose:
            repeated = self.store.resolve(current, self.ai, random_percentage=100)
        self.assertEqual(repeated, bank)
        strategy.assert_not_called()
        choose.assert_not_called()
        self.store.invalidate(current, "Recheck fixture")
        invalid = self.store.resolve(current, random_percentage=100)
        self.assertEqual((invalid.attempt_id, invalid.answer_text, invalid.origin), (bank.attempt_id, bank.answer_text, "bank"))
        self.assertFalse(invalid.can_submit)
        self.assertFalse(self.store.claim_submission(invalid, current))
        self.assertEqual(self.ai.calls, 0)

    def test_mixed_bank_claim_requires_bank_to_still_be_verified(self):
        candidate = self.store.resolve(q(), self.ai)
        self.store.feedback(candidate.attempt_id, "correct", "Verified fixture")
        question = q(instance="bank")
        bank = self.store.resolve(question, random_percentage=0)
        self.store.db.execute("UPDATE answers SET status='candidate'")
        self.assertFalse(self.store.claim_submission(bank, question))
        self.store.db.execute("UPDATE answers SET status='verified'")
        self.assertTrue(self.store.claim_submission(bank, question))
        self.assertFalse(self.store.claim_submission(self.store.resolve(question, random_percentage=100), question))

    def test_mixed_repeated_random_survives_store_reopen_without_new_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "quiz.sqlite3"
            store = QuizStore(path)
            with patch("quiz_core.random.choice", return_value=q().options[3]):
                first = store.resolve(q(), random_percentage=100)
            store.close()
            store = QuizStore(path)
            try:
                with patch("quiz_core.random.randrange") as strategy, patch("quiz_core.random.choice") as choose:
                    repeated = store.resolve(q(), self.ai, random_percentage=0)
                self.assertEqual(repeated, first)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
                strategy.assert_not_called()
                choose.assert_not_called()
            finally:
                store.close()

    def test_new_attempts_snapshot_actual_question_and_options(self):
        candidate = self.store.resolve(q(), self.ai)
        self.store.feedback(candidate.attempt_id, "correct", "Verified fixture")
        self.store.resolve(q(instance="bank"), random_percentage=0)
        self.store.resolve(q(instance="random"), random_percentage=100)
        for row in self.store.db.execute("SELECT scope,stem,options_json FROM attempts"):
            self.assertEqual((row["scope"], row["stem"], json.loads(row["options_json"])),
                             (q().scope, "中国的首都是?", dict(q().options)))

    def test_correct_random_feedback_learns_actual_text_and_remaps_options(self):
        for source in ("manual", "game_ui"):
            with self.subTest(source=source):
                question = q(texts=(" 北京 ", "上海", "广州", "深圳"), scope=source)
                with patch("quiz_core.random.choice", return_value=question.options[0]):
                    decision = self.store.resolve(question, self.ai, random_percentage=100)
                if source == "game_ui":
                    self.assertTrue(self.store.claim_submission(decision, question))
                self.store.feedback(decision.attempt_id, "correct", "Explicit correct result", source=source)
                row = self.store.db.execute("SELECT * FROM answers WHERE qkey=?", (question.key,)).fetchone()
                self.assertEqual((row["scope"], row["stem"], json.loads(row["options_json"]), row["answer_text"], row["status"]),
                                 (source, "中国的首都是?", dict(question.options), " 北京 ", "verified"))
                shuffled = q(texts=("上海", "广州", "深圳", " 北京 "), scope=source, instance="next")
                bank = self.store.resolve(shuffled, self.ai, random_percentage=0)
                self.assertEqual((bank.origin, bank.label, bank.can_submit), ("bank", "D", True))
        self.assertEqual(self.ai.calls, 0)

    def test_random_wrong_and_unknown_do_not_mutate_any_bank_record(self):
        candidate = self.store.resolve(q(), self.ai)
        self.store.feedback(candidate.attempt_id, "correct", "Verified fixture")
        before = dict(self.store.db.execute("SELECT * FROM answers").fetchone())
        for outcome in ("wrong", "unknown"):
            for scope in (q().scope, "empty-" + outcome):
                with self.subTest(outcome=outcome, scope=scope), patch("quiz_core.random.choice", return_value=q().options[3]):
                    decision = self.store.resolve(q(instance=outcome, scope=scope), random_percentage=100)
                    self.store.feedback(decision.attempt_id, outcome, "Explicit feedback")
                    self.assertEqual(dict(self.store.db.execute("SELECT * FROM answers").fetchone()), before)
                    self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 1)
                    self.assertEqual(self.store.db.execute("SELECT outcome FROM attempts WHERE id=?", (decision.attempt_id,)).fetchone()[0], outcome)

    def test_random_wrong_does_not_reject_or_conflict_with_learned_answer(self):
        with patch("quiz_core.random.choice", return_value=q().options[0]):
            wrong = self.store.resolve(q(), random_percentage=100)
            self.store.feedback(wrong.attempt_id, "wrong", "Wrong random fixture")
            correct = self.store.resolve(q(instance="correct"), random_percentage=100)
            self.store.feedback(correct.attempt_id, "correct", "Correct random fixture")
        self.assertEqual(self.store.resolve(q()).status, "verified")
        self.store.invalidate(q(), "Exercise legacy AI refresh")
        answer = self.store.resolve(q(), self.ai, refresh=True)
        self.assertEqual(self.ai.rejected, [])
        self.store.feedback(answer.attempt_id, "correct", "Correct AI fixture")
        self.assertEqual(self.store.resolve(q()).status, "verified")

    def test_random_correct_requires_valid_trusted_snapshot(self):
        corruptions = (("scope", ""), ("stem", ""), ("stem", "Different question"),
                       ("options_json", ""), ("options_json", "[]"),
                       ("options_json", '{"A":"北京","B":null}'),
                       ("options_json", '{"A":"北京","B":"北京"}'),
                       ("answer_text", "天津"), ("label", "D"), ("qkey", "not-the-question"))
        for index, (column, value) in enumerate(corruptions):
            with self.subTest(column=column, value=value), patch("quiz_core.random.choice", return_value=q().options[0]):
                decision = self.store.resolve(q(scope=str(index)), random_percentage=100)
                self.store.db.execute(f"UPDATE attempts SET {column}=? WHERE id=?", (value, decision.attempt_id))
                self.store.feedback(decision.attempt_id, "correct", "Unlearnable fixture")
                self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
        fake = q(stem="第1/10题:随机答题", texts=("选项A", "选项B"))
        with patch("quiz_core.random.choice", return_value=fake.options[0]):
            decision = self.store.resolve(fake, random_answer=True)
        self.store.feedback(decision.attempt_id, "correct", "Legacy fake-reader fixture")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)

    def test_random_correct_conflicting_verified_answer_preserves_record(self):
        candidate = self.store.resolve(q(), self.ai)
        self.store.feedback(candidate.attempt_id, "correct", "Verified fixture")
        before = dict(self.store.db.execute("SELECT * FROM answers").fetchone())
        with patch("quiz_core.random.choice", return_value=q().options[3]):
            for instance in ("conflict", "conflict-again"):
                decision = self.store.resolve(q(instance=instance), random_percentage=100)
                self.store.feedback(decision.attempt_id, "correct", "Different explicit correct result")
                after = dict(self.store.db.execute("SELECT * FROM answers").fetchone())
                self.assertEqual(after["status"], "needs_review")
                for field in ("qkey", "scope", "stem", "options_json", "answer_text", "model"):
                    self.assertEqual(after[field], before[field])

    def test_random_correct_same_verified_answer_preserves_metadata(self):
        candidate = self.store.resolve(q(), self.ai)
        self.store.feedback(candidate.attempt_id, "correct", "Verified fixture")
        spaced = q(texts=(" 北 京 ", "上海", "广州", "深圳"), instance="same")
        with patch("quiz_core.random.choice", return_value=spaced.options[0]):
            decision = self.store.resolve(spaced, random_percentage=100)
        self.store.feedback(decision.attempt_id, "correct", "Same explicit correct result")
        row = self.store.db.execute("SELECT * FROM answers").fetchone()
        self.assertEqual((row["status"], row["answer_text"], row["model"]), ("verified", "北京", "mock"))

    def test_random_learning_respects_authoritative_wrong_conflict_and_inflight_request(self):
        candidate = self.store.resolve(q(), self.ai)
        self.store.feedback(candidate.attempt_id, "wrong", "Wrong AI fixture")
        with patch("quiz_core.random.choice", return_value=q().options[0]):
            decision = self.store.resolve(q(instance="conflict"), random_percentage=100)
        self.store.feedback(decision.attempt_id, "correct", "Conflicting random fixture")
        self.assertEqual(self.store.resolve(q()).status, "needs_review")
        self.store.db.execute("UPDATE answers SET status='asking',answer_text=NULL")
        before = dict(self.store.db.execute("SELECT * FROM answers").fetchone())
        with patch("quiz_core.random.choice", return_value=q().options[3]):
            inflight = self.store.resolve(q(instance="inflight"), random_percentage=0)
        self.store.feedback(inflight.attempt_id, "correct", "Correct during AI request")
        self.assertEqual(dict(self.store.db.execute("SELECT * FROM answers").fetchone()), before)

    def test_revealed_answer_after_wrong_random_choice_learns_correct_option(self):
        with patch("quiz_core.random.choice", return_value=q().options[0]):
            wrong = self.store.resolve(q(), random_percentage=100)
        self.store.feedback(wrong.attempt_id, "wrong", "Wrong selected option")
        self.store.learn_revealed_answer(q(), "上海", "Unique green tick on gold B in the same view")
        bank = self.store.resolve(q(texts=("上海", "广州", "深圳", "北京"), instance="next"), random_percentage=0)
        self.assertEqual((bank.origin, bank.status, bank.answer_text, bank.label), ("bank", "verified", "上海", "A"))
        self.assertEqual(self.store.db.execute("SELECT outcome FROM attempts WHERE id=?", (wrong.attempt_id,)).fetchone()[0], "wrong")

    def test_revealed_answer_replaces_wrong_bank_or_unverified_candidate(self):
        for status in ("verified", "candidate"):
            with self.subTest(status=status):
                question = q(scope=status)
                candidate = self.store.resolve(question, self.ai)
                if status == "verified":
                    self.store.feedback(candidate.attempt_id, "correct", "Verified old fixture")
                    selected = self.store.resolve(question, random_percentage=0)
                    self.assertTrue(self.store.claim_submission(selected, question))
                    self.store.feedback(selected.attempt_id, "wrong", "Old bank answer was wrong", source="game_ui")
                self.store.learn_revealed_answer(question, "上海", "Unique green tick on gold B")
                bank = self.store.resolve(q(scope=status, instance="next"), random_percentage=0)
                self.assertEqual((bank.origin, bank.status, bank.answer_text, bank.label), ("bank", "verified", "上海", "B"))
                row = self.store.db.execute("SELECT model,options_json FROM answers WHERE qkey=?", (question.key,)).fetchone()
                self.assertEqual((row["model"], json.loads(row["options_json"])), ("", dict(question.options)))
                self.assertEqual(self.store.db.execute("SELECT answer_text FROM attempts WHERE id=?", (candidate.attempt_id,)).fetchone()[0], "北京")

    def test_revealed_answer_validation_and_conflict_preserve_record(self):
        for answer, evidence in (("天津", "Actual reveal"), (None, "Actual reveal"), ("北京", ""), ("北京", None)):
            with self.subTest(answer=answer, evidence=evidence), self.assertRaises(QuizError):
                self.store.learn_revealed_answer(q(), answer, evidence)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
        self.store.learn_revealed_answer(q(), " 北 京 ", "Exact reveal")
        before = dict(self.store.db.execute("SELECT * FROM answers").fetchone())
        self.assertEqual(before["answer_text"], "北京")
        self.store.learn_revealed_answer(q(), "上海", "Conflicting reveal")
        after = dict(self.store.db.execute("SELECT * FROM answers").fetchone())
        self.assertEqual((after["answer_text"], after["status"]), ("北京", "needs_review"))
        for field in ("scope", "stem", "options_json", "model"):
            self.assertEqual(after[field], before[field])

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
