"""Headless orchestration behind the PySide6 GUI.

No PySide6 imports here: this layer can be regression-tested without a desktop.
No credentials are persisted, and all device I/O is serialized by the GUI worker.
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from maa_adapter import ROOT, MaaSession, load_config, save_image
from quiz_core import OpenAIProvider, QuizError, QuizStore
from quiz_vision import DeviceLock, Layout, ReadError, answer_once, read_for_answer, write_json
from daily_tasks import DEFAULT_TASK_INTERVAL_MAX_SECONDS, DailyRunner
from ring_tasks import RingRunner
from ring_quiz import UnifiedQuizReader

LOCAL_CONFIG = ROOT / "config/local.json"
USER_SETTINGS = ("adb_path", "address", "account_scope", "layout")
LOCAL_AI_CONFIG = ROOT / "config/ai.local.json"


def ai_settings() -> dict[str, str]:
    if not LOCAL_AI_CONFIG.exists():
        return {"model": "", "api_key": ""}
    try:
        data = json.loads(LOCAL_AI_CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise QuizError("无法读取 config/ai.local.json，请检查文件权限及 JSON 格式。") from None
    if (not isinstance(data, dict) or set(data) != {"model", "api_key"}
            or any(not isinstance(v, str) or not v.strip() for v in data.values())):
        raise QuizError("config/ai.local.json 必须包含非空字符串 model 和 api_key。")
    return {name: value.strip() for name, value in data.items()}


def local_settings() -> dict[str, str]:
    """Safe, local-only configuration. The API secret is NEVER a config field."""
    base = json.loads((ROOT / "config/connection.example.json").read_text(encoding="utf-8"))
    if LOCAL_CONFIG.exists():
        data = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or set(data) - set(base):
            raise QuizError("Config contains unknown keys; edit config/local.json manually.")
        base.update(data)
    return {name: str(base[name]) for name in USER_SETTINGS}


def save_local_settings(data: dict[str, str]):
    if set(data) != set(USER_SETTINGS) or any(not isinstance(v, str) for v in data.values()):
        raise QuizError("Invalid GUI configuration fields.")
    if not data["account_scope"].strip():
        raise QuizError("Account scope is required (not your password).")
    if not data["layout"].strip():
        raise QuizError("Layout path cannot be empty.")
    # Round-trip through the original validation rules before committing the config.
    existing = json.loads((ROOT / "config/connection.example.json").read_text(encoding="utf-8"))
    if LOCAL_CONFIG.exists():
        supplied = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8-sig"))
        if not isinstance(supplied, dict) or set(supplied) - set(existing):
            raise QuizError("Invalid current config; do not overwrite unknown keys.")
        existing.update(supplied)
    existing.update(data)
    target = LOCAL_CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".json.tmp")
    try:
        temp.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        load_config(temp)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)


def preview_png(frame) -> bytes:
    """Convert the copied BGR screenshot to a PNG; no raw screenshot is transmitted."""
    from PIL import Image
    out = io.BytesIO()
    Image.fromarray(frame[:, :, ::-1]).save(out, format="PNG")
    return out.getvalue()


def query_bank(path: Path, scope: str, keyword: str = "", limit: int = 200):
    """Read-only table; only the first 200 records are shown to avoid GUI stalls."""
    if not path.exists():
        return []
    # Use read-only access: opening the UI must not silently modify quiz storage.
    uri = path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        # Parameterized search, escaping SQL wildcard characters.
        search = keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = con.execute("""
            SELECT qkey, status, stem, answer_text, reason, model, updated_at
              FROM answers WHERE scope=?
                AND (stem LIKE ? ESCAPE '\\' OR answer_text LIKE ? ESCAPE '\\')
              ORDER BY updated_at DESC LIMIT ?
        """, (scope, f"%{search}%", f"%{search}%", limit)).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


class DeviceBackend:
    """One connected emulator at a time; called only on one background thread."""

    def __init__(self, session_factory=MaaSession):
        self.session_factory = session_factory
        self.session = None
        self.cfg: dict[str, Any] | None = None
        self.lock: DeviceLock | None = None
        self.connected_address: str = ""

    def _config(self):
        return load_config(LOCAL_CONFIG if LOCAL_CONFIG.exists() else None)

    def devices(self) -> list[dict]:
        # Discovery does not connect and must not acquire the device lock.
        if self.session is not None:
            return self.session.device_list()
        temporary = self.session_factory(self._config(), execute=False)
        try:
            return temporary.device_list()
        finally:
            temporary.close()

    def connect(self):
        if self.session is not None:
            raise QuizError("Already connected. Disconnect before changing devices.")
        cfg = self._config()
        lock = DeviceLock(cfg["database"].with_suffix(".device.lock"))
        lock.__enter__()
        session = None
        try:
            # The class has a dry-run default; explicit single-answer permission is
            # enforced by the worker before enabling it on this session.
            session = self.session_factory(cfg, execute=False)
            session.connect()
            self.session, self.cfg, self.lock = session, cfg, lock
            self.connected_address = session.address
            return {"address": session.address}
        except BaseException:
            if session is not None:
                session.close()
            lock.__exit__(None, None, None)
            raise

    def disconnect(self):
        try:
            if self.session is not None:
                self.session.close()
        finally:
            self.session = None
            self.cfg = None
            self.connected_address = ""
            if self.lock is not None:
                self.lock.__exit__(None, None, None)
                self.lock = None

    def _need_session(self):
        if self.session is None or self.cfg is None:
            raise QuizError("Please connect an Android emulator first.")
        return self.session, self.cfg

    def capture(self, *, save=False, overlay=False) -> dict:
        session, cfg = self._need_session()
        frame = session.capture()
        height, width = frame.shape[:2]
        result = {"png": preview_png(frame), "width": width, "height": height, "address": session.address}
        if save:
            output = cfg["runtime_dir"] / "gui/capture.png"
            save_image(frame, output)
            result["saved"] = str(output)
        if overlay:
            layout = Layout.load(cfg["layout"])
            rois = layout.scaled(width, height)
            output = cfg["runtime_dir"] / "gui/capture-overlay.png"
            save_image(frame, output, rois)
            result["overlay"] = str(output)
        return result

    def inspect(self) -> dict:
        session, cfg = self._need_session()
        folder = cfg["runtime_dir"] / "inspection"
        result = {"valid": False, "ai_called": False, "click_sent": False, "diagnostics": str(folder)}
        try:
            obs = UnifiedQuizReader(session, Layout.load(cfg["layout"]), cfg["account_scope"]).read()
            save_image(obs.frame, folder / "screen.png")
            save_image(obs.frame, folder / "overlay.png", obs.buttons)
            result = {**result, "valid": True, "png": preview_png(obs.frame), "width": obs.size[0],
                      "height": obs.size[1], "overlay": str(folder / "overlay.png"),
                      "question": {"stem": obs.question.stem, "options": dict(obs.question.options)}}
        except QuizError as exc:
            frame = session.capture()
            result = {**result, "error": str(exc), "png": preview_png(frame)}
        write_json(folder / "ocr.json", {k: v for k, v in result.items() if k != "png"})
        return result

    def guard(self) -> dict:
        session, cfg = self._need_session()
        UnifiedQuizReader(session, Layout.load(cfg["layout"]), cfg["account_scope"]).read()
        return {"matched": True, "pipeline": "CalibratedQuizLayout"}

    def answer(self, *, use_ai=False, model="", api_key="", execute=False,
               allow_trial=False, layout_confirmed=False, max_daily_calls=30,
               random_answer=False, should_stop: Callable[[], bool] | None = None,
               expected_view_token=None, random_percentage=0, reader=None) -> dict:
        session, cfg = self._need_session()
        percentage = 100 if random_answer else random_percentage
        use_ai = use_ai and percentage != 100
        if execute and not layout_confirmed:
            raise QuizError("Inspect and explicitly confirm the screenshot layout before submitting.")
        if use_ai and not model.strip():
            raise QuizError("Choose an OpenAI model before permitting paid API calls.")
        if should_stop is not None and should_stop():
            raise QuizError("Cancelled before starting; no API call/click made.")
        session.execute = bool(execute)
        store = QuizStore(cfg["database"])
        try:
            provider = OpenAIProvider(model=model, api_key=api_key or None) if use_ai else None
            if reader is None:
                reader = UnifiedQuizReader(session, Layout.load(cfg["layout"]), cfg["account_scope"])
            return answer_once(reader, session, store, provider=provider, execute=execute,
                               layout_confirmed=layout_confirmed, allow_trial=allow_trial,
                               max_daily_calls=max_daily_calls, random_answer=random_answer,
                               random_percentage=percentage,
                               receipt_dir=cfg["runtime_dir"] / "receipts",
                               should_stop=should_stop, expected_view_token=expected_view_token)
        finally:
            session.execute = False
            store.close()

    def auto_answer(self, *, should_stop: Callable[[], bool], on_result=None, **options) -> dict:
        session, cfg = self._need_session()
        # The automatic reader validates the scene and layout before authorizing a click.
        options = {**options, "execute": True, "layout_confirmed": True,
                   "random_percentage": 100 if options.get("random_answer", False) else options.get("random_percentage", 0),
                   "use_ai": options.get("use_ai", False) and not options.get("random_answer", False)}
        reader = UnifiedQuizReader(session, Layout.load(cfg["layout"]), cfg["account_scope"])
        submitted = last_number = 0
        last_total = last_view = None
        deadline = time.monotonic() + cfg["timeout_seconds"]
        last_error = ""
        while not should_stop():
            if time.monotonic() >= deadline:
                raise QuizError("等待新题或有效题目超时，自动答题已暂停。" + last_error)
            try:
                observation = read_for_answer(reader)
            except QuizError as exc:
                last_error = str(exc)
                time.sleep(0.5)
                continue
            match = re.match(r"^第\s*(\d+)\s*/\s*(\d+)\s*题", observation.question.stem)
            if match is None:
                result = self.answer(should_stop=should_stop, reader=reader,
                                     expected_view_token=observation.question.view_token, **options)
                if not result.get("click_sent"):
                    raise QuizError("当前题没有可提交答案，答题已暂停。")
                if on_result is not None:
                    on_result({**result, "question_number": 1, "question_total": 1})
                return {"submitted": submitted + 1, "reason": "completed"}
            number, total = map(int, match.groups())
            if last_number:
                if total != last_total or number not in (last_number, last_number + 1):
                    raise QuizError("题号跳变或已进入另一轮答题，自动答题已暂停。")
                if number == last_number:
                    if observation.question.view_token != last_view:
                        raise QuizError("同一题号的题目内容发生变化，自动答题已暂停。")
                    time.sleep(0.5)
                    continue
            try:
                result = self.answer(should_stop=should_stop, reader=reader,
                                     expected_view_token=observation.question.view_token, **options)
            except ReadError as exc:
                last_error = str(exc)
                time.sleep(0.5)
                continue
            except QuizError:
                if should_stop():
                    break
                raise
            if not result.get("click_sent"):
                decision = result.get("decision") or {}
                raise QuizError("当前题没有可提交答案，自动答题已暂停：" + str(decision.get("reason") or decision.get("status", "未知原因")))
            submitted += 1
            last_number, last_total, last_view = number, total, observation.question.view_token
            result.update(question_number=number, question_total=total)
            if on_result is not None:
                on_result(result)
            if number == total:
                return {"submitted": submitted, "reason": "completed"}
            last_error = ""
            deadline = time.monotonic() + cfg["timeout_seconds"]
        return {"submitted": submitted, "reason": "stopped"}

    def inspect_daily(self) -> dict:
        session, cfg = self._need_session()
        runner = DailyRunner(session, cfg["runtime_dir"], lambda: False)
        view = runner.read()
        result = {"valid": False, "png": preview_png(view.frame), "diagnostics": str(runner.folder)}
        try:
            cards = view.cards()
            result.update(valid=True, cards=[asdict(c) for c in cards],
                          sweep_available=any(c.action == "立即完成" and not c.done for c in cards))
        except QuizError as exc:
            result["error"] = str(exc)
        runner.save(result.get("error", ""))
        return result

    def auto_daily(self, *, selected, should_stop, on_result=None,
                   task_interval_max=DEFAULT_TASK_INTERVAL_MAX_SECONDS, random_percentage=0,
                   use_ai=False, model="", api_key="", max_daily_calls=30) -> dict:
        session, cfg = self._need_session()
        def answer_quiz(progress):
            result = self.auto_answer(random_percentage=random_percentage, use_ai=use_ai, model=model, api_key=api_key,
                                      max_daily_calls=max_daily_calls, allow_trial=use_ai,
                                      should_stop=should_stop, on_result=progress)
            session.execute = True
            return result
        session.execute = True
        try:
            return DailyRunner(session, cfg["runtime_dir"], should_stop, on_result, answer_quiz=answer_quiz,
                               task_interval_max=task_interval_max).run(selected)
        finally:
            session.execute = False

    def auto_ring(self, *, should_stop, on_result=None, use_ai=False, model="", api_key="",
                  random_answer=False, allow_trial=False, max_daily_calls=30, random_percentage=0) -> dict:
        session, cfg = self._need_session()
        reader = UnifiedQuizReader(session, Layout.load(cfg["layout"]), cfg["account_scope"])
        retry_pending = False

        def answer_quiz():
            nonlocal retry_pending
            expected = None
            try:
                if retry_pending:
                    if should_stop():
                        raise QuizError("已停止，未准备答题重试。")
                    observation = read_for_answer(reader)
                    if should_stop():
                        raise QuizError("已停止，未准备答题重试。")
                    expected = observation.question.view_token
                    store = QuizStore(cfg["database"])
                    try:
                        pending = store.db.execute("SELECT id FROM attempts WHERE qkey=? AND submitted_at IS NOT NULL "
                                                   "AND outcome='pending' ORDER BY rowid DESC LIMIT 1",
                                                   (observation.question.key,)).fetchone()
                        if pending:
                            store.feedback(pending["id"], "unknown",
                                           "已确认答错对话关闭并重新识别同题；未确认正确答案。", source="game_ui")
                    finally:
                        store.close()
                result = self.answer(execute=True, layout_confirmed=True, should_stop=should_stop, reader=reader,
                                     expected_view_token=expected, random_answer=random_answer,
                                     random_percentage=random_percentage, use_ai=use_ai, model=model,
                                     api_key=api_key, allow_trial=allow_trial, max_daily_calls=max_daily_calls)
                retry_pending = False
                return result
            finally:
                session.execute = True

        def confirm_retry():
            nonlocal retry_pending
            if should_stop():
                raise QuizError("已停止，未开放答题重试。")
            reader.confirm_retry()
            retry_pending = True

        session.execute = True
        try:
            return RingRunner(session, cfg["runtime_dir"], should_stop, on_result,
                              answer_quiz=answer_quiz, on_quiz_retry=confirm_retry).run()
        finally:
            session.execute = False

    def bank(self, keyword="") -> list[dict]:
        cfg = self._config()
        return query_bank(cfg["database"], cfg["account_scope"], keyword)

    def feedback(self, attempt_id: str, outcome: str, evidence: str):
        if not attempt_id or outcome not in {"correct", "wrong", "unknown"} or not evidence.strip():
            raise QuizError("Feedback requires an attempt, an outcome and observed evidence.")
        cfg = self._config()
        store = QuizStore(cfg["database"])
        try:
            # GUI only accepts outcomes for attempts actually submitted via our adapter.
            row = store.db.execute("SELECT submitted_at, outcome FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None or row["submitted_at"] is None:
                raise QuizError("This attempt was never submitted by the assistant; cannot confirm in GUI.")
            store.feedback(attempt_id, outcome, evidence, source="manual")
            return {"attempt_id": attempt_id, "outcome": outcome}
        finally:
            store.close()

    def shutdown(self):
        self.disconnect()
