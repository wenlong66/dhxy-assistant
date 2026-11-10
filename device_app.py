"""Windows/ADB CLI. Read-only by default; at most one answer click per invocation."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from maa_adapter import ROOT, MaaSession, load_config, save_image
from quiz_core import OpenAIProvider, QuizError, QuizStore
from quiz_vision import DeviceLock, Layout, QuizReader, answer_once, parse_fields, write_json


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="DHXY MaaFramework/ADB adapter — read-only unless --execute is explicit")
    parser.add_argument("--config", type=Path, help="Local JSON config; defaults to config/local.json when present")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("init-config", help="Create config/local.json without overwriting it")
    subs.add_parser("devices", help="List discovered Android devices/emulators")
    capture = subs.add_parser("capture", help="Save raw screenshot and ROI overlay locally")
    capture.add_argument("--output", type=Path, default=ROOT / "data/capture.png")
    subs.add_parser("guard", help="Run the read-only Maa DaliSceneGuard pipeline")
    subs.add_parser("inspect", help="OCR current question; write local diagnostics, never call AI")
    answer = subs.add_parser("answer", help="Resolve current question, optionally submit ONE answer")
    answer.add_argument("--ai", action="store_true", help="Permit a paid OpenAI request on a cache miss")
    answer.add_argument("--model", default=os.environ.get("OPENAI_MODEL"))
    answer.add_argument("--prompt-key", action="store_true", help="Read key locally with hidden input, never save it")
    answer.add_argument("--allow-trial", action="store_true", help="Allow an unverified AI candidate")
    answer.add_argument("--execute", action="store_true", help="Permit ONE game click after repeated scene verification")
    answer.add_argument("--layout-confirmed", action="store_true", help="I inspected the overlay and OCR on this device")
    answer.add_argument("--max-daily-calls", type=int, default=30)
    args = parser.parse_args(argv)
    session = None
    store = None
    lock = None
    try:
        if args.command == "init-config":
            target = args.config or ROOT / "config/local.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as stream:
                stream.write((ROOT / "config/connection.example.json").read_text(encoding="utf-8"))
            emit({"created": str(target), "note": "Local-only settings; do not commit account-specific data."})
            return 0
        default_local = ROOT / "config/local.json"
        config_path = args.config or (default_local if default_local.exists() else None)
        cfg = load_config(config_path)
        execute = getattr(args, "execute", False)
        if execute and not args.layout_confirmed:
            raise QuizError("Run capture and inspect first; --execute also requires --layout-confirmed.")
        if args.command == "answer":
            if not 1 <= args.max_daily_calls <= 1000:
                raise QuizError("max-daily-calls must be in 1..1000.")
            if args.ai and not args.model:
                raise QuizError("Set --model or OPENAI_MODEL to a model enabled for your account.")
            if args.ai and args.prompt_key and not os.environ.get("OPENAI_API_KEY"):
                os.environ["OPENAI_API_KEY"] = getpass.getpass("OpenAI API key (local hidden input, not saved): ").strip()
        lock_path = cfg["database"].with_suffix(".device.lock")
        lock = DeviceLock(lock_path)
        lock.__enter__()
        session = MaaSession(cfg, execute=execute)
        if args.command == "devices":
            emit({"devices": session.device_list(), "click_sent": False})
            return 0
        session.connect()
        layout = Layout.load(cfg["layout"])
        if args.command == "capture":
            frame = session.capture()
            save_image(frame, args.output)
            info = {"screenshot": str(args.output), "size": [frame.shape[1], frame.shape[0]], "click_sent": False}
            try:
                rois = layout.scaled(frame.shape[1], frame.shape[0])
                overlay = args.output.with_name(args.output.stem + "-overlay.png")
                save_image(frame, overlay, rois)
                info["overlay"] = str(overlay)
            except QuizError as exc:
                info["layout_error"] = str(exc)
            emit(info)
            return 0
        if args.command == "guard":
            frame = session.capture()
            session.guard_scene(layout.scaled(frame.shape[1], frame.shape[0])["title"])
            emit({"pipeline": "DaliSceneGuard", "matched": True, "click_sent": False})
            return 0
        reader = QuizReader(session, layout, cfg["account_scope"])
        if args.command == "inspect":
            frame, captured, fields, rois = reader.inspect()
            folder = cfg["runtime_dir"] / "inspection"
            save_image(frame, folder / "screen.png")
            save_image(frame, folder / "overlay.png", rois)
            result = {"fields": {k: [asdict(b) for b in v] for k, v in fields.items()},
                      "click_sent": False, "ai_called": False, "valid": False}
            try:
                observation = parse_fields(fields, layout, (frame.shape[1], frame.shape[0]),
                                           session.address, cfg["account_scope"], captured)
                result.update(valid=True, question={"stem": observation.question.stem,
                              "options": dict(observation.question.options)})
            except QuizError as exc:
                result["error"] = str(exc)
            write_json(folder / "ocr.json", result)
            result["diagnostics"] = str(folder)
            emit(result)
            return 0 if result["valid"] else 2
        store = QuizStore(cfg["database"])
        provider = OpenAIProvider(args.model) if args.ai else None
        result = answer_once(reader, session, store, provider=provider, execute=execute,
                             layout_confirmed=args.layout_confirmed, allow_trial=args.allow_trial,
                             max_daily_calls=args.max_daily_calls,
                             receipt_dir=cfg["runtime_dir"] / "receipts")
        emit(result)
        return 0
    except KeyboardInterrupt:
        print("Stopped by user; no automatic retry.", file=sys.stderr)
        return 130
    except (QuizError, OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        print(f"Paused: {exc}", file=sys.stderr)
        return 2
    finally:
        if session is not None:
            session.close()
        if store is not None:
            store.close()
        if lock is not None:
            lock.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
