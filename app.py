"""Command-line harness; deliberately does not connect to or click a game."""
from __future__ import annotations
import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from quiz_core import AIAnswer, OpenAIProvider, Question, QuizError, QuizStore


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def load_question(path):
    return Question.from_dict(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="DHXY quiz core — no game control")
    parser.add_argument("--db", default="data/quiz.sqlite3")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("demo", help="Offline test, no API/key/game required")
    solve = subs.add_parser("solve")
    solve.add_argument("--question", required=True)
    solve.add_argument("--ai", action="store_true", help="Allow a paid API call on a cache miss")
    solve.add_argument("--model", default=os.environ.get("OPENAI_MODEL"),
                       help="Model ID enabled for your account; alternatively set OPENAI_MODEL")
    solve.add_argument("--prompt-key", action="store_true", help="Local hidden input if process has no key")
    solve.add_argument("--allow-trial", action="store_true", help="Allow an unverified answer as a candidate for external submission")
    solve.add_argument("--refresh", action="store_true", help="Explicitly replace a non-verified cached result")
    solve.add_argument("--image", type=Path, help="Optional user-cropped quiz-only PNG/JPG/WebP; never sent on cache hit")
    solve.add_argument("--max-daily-calls", type=int, default=30)
    feedback = subs.add_parser("feedback")
    feedback.add_argument("attempt_id")
    feedback.add_argument("outcome", choices=["correct", "wrong", "unknown"])
    feedback.add_argument("--evidence", required=True)
    invalidate = subs.add_parser("invalidate")
    invalidate.add_argument("--question", required=True)
    invalidate.add_argument("--reason", required=True)
    recover = subs.add_parser("recover")
    recover.add_argument("--all-workers-stopped", action="store_true")
    subs.add_parser("stats")
    args = parser.parse_args()

    if args.command == "demo":
        class DemoProvider:
            calls = 0
            def answer(self, q, rejected, image=None):
                self.calls += 1
                return AIAnswer("answer", "北京", "Offline mock, not a real model output.", "offline-test")
        provider = DemoProvider()
        store = QuizStore(":memory:")
        try:
            q = Question("第1/10题：中国的首都是？", tuple(zip("ABCD", ["北京", "上海", "广州", "深圳"])), "demo-1")
            a = store.resolve(q, provider, allow_trial=True)
            claimed = store.claim_submission(a, q)
            store.feedback(a.attempt_id, "correct", "Offline synthetic fixture; no game verification.", source="game_ui")
            shuffled = Question(q.stem, tuple(zip("ABCD", ["上海", "广州", "深圳", "北京"])), "demo-2")
            b = store.resolve(shuffled, provider)
            emit({"mode": "OFFLINE MOCK ONLY", "first_decision": a.to_dict(),
                  "submission_guard_claimed_without_clicking": claimed,
                  "shuffled_cache_result": b.to_dict(), "mock_provider_calls": provider.calls,
                  "note": "No game connection, real AI call or real-game answer was involved."})
            return 0
        finally:
            store.close()

    store = QuizStore(args.db)
    try:
        if args.command == "solve":
            if args.prompt_key and args.ai and not os.environ.get("OPENAI_API_KEY"):
                os.environ["OPENAI_API_KEY"] = getpass.getpass("OpenAI API key (local hidden input, not saved): ").strip()
            q = load_question(args.question)
            provider = OpenAIProvider(args.model) if args.ai else None
            d = store.resolve(q, provider, image=args.image, allow_trial=args.allow_trial,
                              refresh=args.refresh, max_daily_calls=args.max_daily_calls)
            emit({"decision": d.to_dict(), "game_click_performed": False})
        elif args.command == "feedback":
            store.feedback(args.attempt_id, args.outcome, args.evidence, source="manual")
            emit({"saved": True, "source": "manual"})
        elif args.command == "invalidate":
            store.invalidate(load_question(args.question), args.reason)
            emit({"invalidated_if_present": True})
        elif args.command == "recover":
            if not args.all_workers_stopped:
                raise QuizError("Stop all other workers before recovering interrupted requests.")
            emit({"recovered": store.recover_interrupted_requests()})
        elif args.command == "stats":
            emit({"answers": [dict(r) for r in store.db.execute("SELECT status,COUNT(*) AS count FROM answers GROUP BY status")],
                  "requests": [dict(r) for r in store.db.execute("SELECT game_day,COUNT(*) AS requests,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens FROM requests GROUP BY game_day ORDER BY game_day DESC")]})
        return 0
    except (QuizError, ValueError, OSError) as exc:
        print(f"Paused: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
