"""Strict Dali scene parsing and one-question execution. No automatic correctness claims."""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import time
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np

from local_vision import contains
from quiz_core import OCR_SIGNIFICANT_TEXT, Question, QuizError, QuizStore, digest, game_day, normalize
from quiz_feedback import correct_choice

INPUT_PAUSE_SECONDS = (0.3, 1.0)
QUESTION_ICON_COLOR_GAP = 15
QUESTION_ICON_MINIMUM_COVERAGE = .02
QUESTION_LINE_CENTER_TOLERANCE = .45
UNREADABLE_BUTTON_PIXEL_TOLERANCE = 8
UNREADABLE_CHARACTERS = re.compile(r"[�□▯]")


class ReadError(QuizError):
    """An unreadable or changing frame that can be retried before any click."""


@dataclass(frozen=True)
class TextBlock:
    text: str
    score: float
    box: tuple[int, int, int, int]


def join_blocks(blocks: list[TextBlock], minimum: float, *, mask_unreadable=False) -> str:
    if not blocks or (not mask_unreadable and any(
            not b.text.strip() or not math.isfinite(b.score) or b.score < minimum for b in blocks)):
        raise QuizError("Missing/low-confidence OCR text; inspect the ROI, do not guess.")
    if any(len(b.box) != 4 or b.box[2] <= 0 or b.box[3] <= 0 for b in blocks):
        raise QuizError("Invalid OCR bounding box.")
    def render(block):
        if mask_unreadable and (not block.text.strip() or not math.isfinite(block.score) or block.score < minimum):
            return "*"
        return UNREADABLE_CHARACTERS.sub("*", block.text.strip()) if mask_unreadable else block.text.strip()
    # Group slightly skewed fragments of the same line, then order left-to-right.
    rows = []
    for b in sorted(blocks, key=lambda b: b.box[1] + b.box[3] / 2):
        center = b.box[1] + b.box[3] / 2
        if rows and abs(center - rows[-1][0]) <= min(b.box[3], rows[-1][1]) * 0.45:
            rows[-1][2].append(b)
        else:
            rows.append([center, b.box[3], [b]])
    return "\n".join("".join(render(b) for b in sorted(row[2], key=lambda b: b.box[0])) for row in rows)


def question_icon_visible(frame, icon_roi):
    x, y, w, h = icon_roi
    icon = frame[y:y + h, x:x + w].astype(int)
    blue, green, red = (icon[:, :, i] for i in range(3))
    colored = ((green - red > QUESTION_ICON_COLOR_GAP) | (blue - red > QUESTION_ICON_COLOR_GAP))
    return float(colored.mean()) >= QUESTION_ICON_MINIMUM_COVERAGE


def join_question_blocks(blocks, minimum, frame, icon_roi, *, mask_unreadable=False):
    if icon_roi is not None:
        kept = [b for b in blocks if not contains(icon_roi, b.box)]
        if kept and len(kept) != len(blocks) and question_icon_visible(frame, icon_roi):
            blocks = kept
    if not mask_unreadable and any("*" in b.text or UNREADABLE_CHARACTERS.search(b.text) for b in blocks):
        raise QuizError("Question contains unrecognized characters.")
    return join_blocks(blocks, minimum, mask_unreadable=mask_unreadable)


def read_question_blocks(session, frame, roi, minimum, icon_roi=None):
    blocks = session.ocr(frame, roi)
    try:
        join_question_blocks(blocks, minimum, frame, icon_roi)
        return blocks
    except QuizError:
        pass
    if not blocks or any(len(b.box) != 4 or min(b.box[2:]) <= 0 or not contains(roi, b.box) for b in blocks):
        return blocks
    trusted = [b for b in blocks if b.text.strip() and math.isfinite(b.score) and b.score >= minimum]
    if not trusted:
        return blocks
    line = max(trusted, key=lambda b: b.box[2])
    center = line.box[1] + line.box[3] / 2
    if any(abs(b.box[1] + b.box[3] / 2 - center)
           > min(b.box[3], line.box[3]) * QUESTION_LINE_CENTER_TOLERANCE for b in blocks):
        return blocks
    left, top = min(b.box[0] for b in blocks), min(b.box[1] for b in blocks)
    right, bottom = max(b.box[0] + b.box[2] for b in blocks), max(b.box[1] + b.box[3] for b in blocks)
    reread = session.ocr(frame, (left, top, right - left, bottom - top), single_line=True)
    try:
        text = join_question_blocks(reread, minimum, frame, icon_roi)
    except QuizError:
        return blocks
    if any(not contains(roi, b.box) for b in reread):
        return blocks
    observed_tokens = Counter(token for block in blocks
                              for token in OCR_SIGNIFICANT_TEXT.findall(normalize(block.text)))
    if Counter(OCR_SIGNIFICANT_TEXT.findall(normalize(text))) - observed_tokens:
        return blocks
    for block in trusted:
        remaining = iter(OCR_SIGNIFICANT_TEXT.findall(normalize(text)))
        if any(not any(token == current for current in remaining)
               for token in OCR_SIGNIFICANT_TEXT.findall(normalize(block.text))):
            return blocks
    return reread


class Layout:
    def __init__(self, data: dict):
        self.data = data
        self.width, self.height = data["reference_size"]
        if self.width <= self.height or self.height <= 0:
            raise QuizError("Layout reference_size must be landscape.")
        self.minimum = float(data["min_ocr_score"])
        self.tolerance = float(data["aspect_tolerance"])
        if not 0.5 <= self.minimum <= 1 or not 0 <= self.tolerance <= 0.1:
            raise QuizError("Unsafe OCR threshold or aspect tolerance.")
        required = {"question", *[f"label_{k}" for k in "ABCD"], *[f"option_{k}" for k in "ABCD"]}
        if set(data["regions"]) not in (required, required | {"title"}) or set(data["buttons"]) != set("ABCD"):
            raise QuizError("Layout needs question and all four option/label/button regions.")
        for rect in [*data["regions"].values(), *data["buttons"].values()]:
            if len(rect) != 4 or any(type(v) is not int for v in rect):
                raise QuizError("Regions must contain four integers: x,y,w,h.")
            x, y, w, h = rect
            if min(x, y) < 0 or min(w, h) <= 0 or x + w > self.width or y + h > self.height:
                raise QuizError("Layout region is outside the reference frame.")

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8-sig")))

    def scaled(self, width: int, height: int, group="regions"):
        if min(width, height) <= 0 or abs((width / height) / (self.width / self.height) - 1) > self.tolerance:
            raise QuizError("Screenshot aspect ratio differs from the calibrated layout; stop and recalibrate.")
        output = {}
        for name, (x, y, w, h) in self.data[group].items():
            if group == "regions" and name == "title":
                continue  # Older local layouts may retain the unused title region.
            left, top = round(x * width / self.width), round(y * height / self.height)
            right, bottom = round((x + w) * width / self.width), round((y + h) * height / self.height)
            output[name] = (left, top, max(1, right - left), max(1, bottom - top))
        return output


@dataclass
class Observation:
    question: Question
    size: tuple[int, int]
    buttons: dict[str, tuple[int, int, int, int]]
    captured_at: float
    frame: object = None
    question_roi: tuple[int, int, int, int] | None = None
    option_rois: dict[str, tuple[int, int, int, int]] | None = None
    question_icon_roi: tuple[int, int, int, int] | None = None

    @property
    def signature(self):
        return digest([self.question.view_token, self.size, self.buttons])


class QuestionReadError(ReadError):
    def __init__(self, message, observation):
        super().__init__(message)
        self.observation = observation


def read_for_answer(reader):
    try:
        return reader.read()
    except QuestionReadError as exc:
        return exc.observation


def option_labels(fields, minimum):
    # Count through the last visible option; gaps and partially read answers still fail validation.
    labels = "AB"
    for k in "CD":
        # Empty gold regions can produce a weak dash; retain real text and trusted punctuation.
        text = any(b.text.strip() and (b.score >= minimum or re.search(r"\w", b.text)
                                      or not re.fullmatch(r"[-－—–]+", b.text.strip()))
                   for b in fields.get(f"option_{k}", []))
        label = any(b.text.strip() and (b.score >= minimum or re.search(r"\w", b.text))
                    for b in fields.get(f"label_{k}", []))
        if text or label:
            labels = "ABCD"[:"ABCD".index(k) + 1]
    return labels


def partial_option_buttons_confirmed(frame, buttons):
    if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
        return False
    for x, y, w, h in buttons.values():
        if min(x, y) < 0 or min(w, h) <= 0 or y + h > frame.shape[0] or x + w > frame.shape[1]:
            return False
        pixels = frame[y + h // 4:y + 3 * h // 4, x + w * 55 // 100:x + w * 90 // 100]
        if not pixels.size:
            return False
        blue, green, red = (pixels[:, :, i].astype(int) for i in range(3))
        gold = (red > 180) & (green > 155) & (blue > 110) & (red >= green) & (green >= blue) & (red - blue >= 8)
        if gold.mean() < .5:
            return False
    return True


def parse_fields(fields, layout: Layout, size, device_id: str, scope: str,
                 captured_at: float, frame=None, *, random_answer=False) -> Observation:
    labels = option_labels(fields, layout.minimum)
    names = ["question", *[f"label_{k}" for k in labels], *[f"option_{k}" for k in labels]]
    text = {}
    unreadable_options = []
    for name in names:
        try:
            text[name] = (join_question_blocks(fields.get(name, []), layout.minimum, frame, None,
                                               mask_unreadable=True) if name == "question" else
                          join_blocks(fields.get(name, []), layout.minimum))
            if name.startswith("option_") and ("*" in text[name] or UNREADABLE_CHARACTERS.search(text[name])):
                raise QuizError("Option contains unrecognized characters.")
        except QuizError as exc:
            if not name.startswith("option_") or not isinstance(frame, np.ndarray):
                raise QuizError(f"OCR 区域 {name} 未通过：{exc}") from exc
            label = name[-1]
            unreadable_options.append(label)
            text[name] = f"*{label}*"
    regions = layout.scaled(*size)
    for label in labels:
        blocks = fields.get(f"label_{label}", [])
        if (normalize(text[f"label_{label}"]).upper() != label or
                (unreadable_options and any(not contains(regions[f"label_{label}"], b.box) for b in blocks))):
            raise QuizError(f"Option {label} label is missing/misaligned; inspect the layout.")
    if unreadable_options and (frame.shape != (size[1], size[0], 3) or not partial_option_buttons_confirmed(
            frame, {k: layout.scaled(*size, group="buttons")[k] for k in labels})):
        raise QuizError("无法确认选项按钮画面，未答题。")
    stem = unicodedata.normalize("NFKC", text["question"])
    match = re.match(r"^第\s*(\d+)\s*/\s*(\d+)\s*题\s*:\s*(.+)$", stem, re.S)
    if not match or not 1 <= int(match[1]) <= int(match[2]) <= 100:
        raise QuizError("Question number or full question text was not recognized.")
    # Stable across process restarts: same account/day/instance/question number cannot be re-clicked.
    instance = digest([game_day(), scope, device_id, int(match[1]), int(match[2])])
    options = tuple((k, re.sub(r"\s*\n\s*", "", text[f"option_{k}"])) for k in labels)
    x, y, w, h = layout.scaled(*size)["question"]
    pixels = hashlib.sha256(frame[y:y + h, x:x + w].tobytes()).hexdigest() if isinstance(frame, np.ndarray) else ""
    visual_token = digest([game_day(), scope, device_id, pixels]) if pixels else ""
    question = Question(stem, options, instance, scope=scope, is_readable="*" not in stem and not unreadable_options,
                        visual_token=visual_token, unreadable_options=tuple(unreadable_options))
    buttons = layout.scaled(*size, group="buttons")
    return Observation(question, tuple(size), {k: buttons[k] for k in labels}, captured_at, frame,
                       layout.scaled(*size)["question"],
                       {k: layout.scaled(*size)[f"option_{k}"] for k in labels})


class QuizReader:
    def __init__(self, session, layout: Layout, scope: str, *, random_answer=False):
        self.session, self.layout, self.scope = session, layout, scope
        self.random_answer = random_answer
        self.previous_question = None

    def parse(self, fields, size, captured_at, frame):
        observation = parse_fields(fields, self.layout, size, self.session.address, self.scope, captured_at,
                                   frame, random_answer=self.random_answer)
        previous = self.previous_question
        if (previous is not None and previous.instance_id == observation.question.instance_id
                and previous.matches_content(observation.question.stem, observation.question.options)):
            observation = replace(observation, question=replace(
                previous, is_readable=previous.is_readable and observation.question.is_readable))
        self.previous_question = observation.question
        if not observation.question.is_readable:
            raise QuestionReadError("OCR 区域 question 包含未识别字符（*），使用随机兜底。", observation)
        return observation

    def inspect(self):
        frame = self.session.capture()
        captured_at = time.monotonic()
        height, width = frame.shape[:2]
        rois = self.layout.scaled(width, height)
        fields = {name: (read_question_blocks(self.session, frame, roi, self.layout.minimum)
                         if name == "question" else
                         self.session.ocr(frame, roi, single_line=name.startswith("label_")))
                  for name, roi in rois.items()}
        return frame, captured_at, fields, rois

    def read(self):
        try:
            frame, captured, fields, rois = self.inspect()
            return self.parse(fields, (frame.shape[1], frame.shape[0]), captured, frame)
        except QuestionReadError:
            raise
        except QuizError as exc:
            raise ReadError(str(exc)) from exc


def assert_same(first: Observation, current: Observation, max_age: float = 20):
    age = time.monotonic() - current.captured_at
    if age < 0 or age > max_age:
        raise ReadError(f"Screenshot is stale ({age:.1f}s); no click performed.")
    if current.size != first.size or current.buttons != first.buttons:
        raise ReadError("Question/layout changed: screenshot size or answer buttons differ; no click performed.")
    if current.signature != first.signature:
        raise ReadError("Question/layout changed: question or option identity differs; no click performed.")


def recheck_for_answer(first: Observation, current: Observation) -> Observation:
    unknown = set(first.question.unreadable_options) | set(current.question.unreadable_options)
    if unknown:
        if (first.size != current.size or first.buttons != current.buttons or
                first.question.scope != current.question.scope or
                first.question.submission_token != current.question.submission_token or
                normalize(first.question.stem) != normalize(current.question.stem) or
                not first.option_rois or first.option_rois != current.option_rois or
                not isinstance(first.frame, np.ndarray) or not isinstance(current.frame, np.ndarray)):
            raise ReadError("题目或选项布局已变化，未答题。")
        old, new = dict(first.question.options), dict(current.question.options)
        if (old.keys() != new.keys() or any(normalize(old[k]) != normalize(new[k]) for k in old if k not in unknown)):
            raise ReadError("题目选项已变化，未答题。")
        for label in unknown:
            x, y, w, h = first.buttons[label]
            before = first.frame[y:y+h, x:x+w].astype(np.int16)
            after = current.frame[y:y+h, x:x+w].astype(np.int16)
            if before.shape != after.shape or np.any(np.abs(before - after) > UNREADABLE_BUTTON_PIXEL_TOLERANCE):
                raise ReadError("未识别选项按钮画面已变化，未答题。")
        question = replace(first.question, is_readable=False,
                           options=tuple((k, f"*{k}*" if k in unknown else v) for k, v in first.question.options),
                           unreadable_options=tuple(k for k, _ in first.question.options if k in unknown))
        current = replace(current, question=question)
        assert_same(replace(first, question=question), current)
        return current
    try:
        assert_same(first, current)
    except ReadError:
        if (not first.question.visual_token or not current.question.visual_token
                or first.question_icon_roi is None or first.question_icon_roi != current.question_icon_roi
                or first.question.scope != current.question.scope
                or first.size != current.size or first.buttons != current.buttons
                or not first.option_rois or first.option_rois != current.option_rois
                or first.frame is None or current.frame is None):
            raise
        same_options = first.question.matches_content(first.question.stem, current.question.options)
        same_image = first.question.visual_token == current.question.visual_token
        uncertain_stem = not first.question.is_readable or not current.question.is_readable
        if not same_image and not (same_options and uncertain_stem):
            raise
        if not same_options:
            for x, y, w, h in first.option_rois.values():
                if first.frame[y:y + h, x:x + w].tobytes() != current.frame[y:y + h, x:x + w].tobytes():
                    raise
        current = replace(current, question=replace(first.question, is_readable=False))
        assert_same(first, current)
    if not first.question.is_readable and current.question.is_readable:
        current = replace(current, question=replace(current.question, is_readable=False))
    return current


class DeviceLock:
    """Cross-process fail-closed lock. After a crash, remove manually only after stopping all workers."""
    def __init__(self, path: Path):
        self.path = path
        self.owned = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise QuizError("Another adapter may be running. Do not remove its lock until all workers have stopped.") from None
        self.owned = True
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid()}, stream)
        return self

    def __exit__(self, *args):
        if self.owned:
            self.path.unlink(missing_ok=True)
            self.owned = False


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def random_click_point(box):
    x, y, w, h = box
    inset_x = min(max(1, w // 10), (w - 1) // 2)
    inset_y = min(max(1, h // 10), (h - 1) // 2)
    return (random.randint(x + inset_x, x + w - 1 - inset_x),
            random.randint(y + inset_y, y + h - 1 - inset_y))


def record_revealed_feedback(session, store, observation, attempt_id, selected_label, *, sleep, require_active):
    if observation.frame is None or observation.question_roi is None or observation.option_rois is None:
        return "awaiting_manual_feedback"
    if not observation.question.is_readable:
        return "awaiting_manual_feedback"
    if any(correct_choice(observation.frame, box) for box in observation.buttons.values()):
        return "awaiting_manual_feedback"
    for _ in range(5):
        require_active()
        frame = session.capture()
        require_active()
        if tuple(frame.shape[:2][::-1]) != observation.size:
            return "awaiting_manual_feedback"
        try:
            blocks = read_question_blocks(session, frame, observation.question_roi, .8,
                                          observation.question_icon_roi)
            stem = join_question_blocks(blocks, .8, frame, observation.question_icon_roi)
        except QuizError:
            return "awaiting_manual_feedback"
        if normalize(stem) != normalize(observation.question.stem):
            return "awaiting_manual_feedback"
        labels = [label for label, box in observation.buttons.items() if correct_choice(frame, box)]
        if len(labels) > 1:
            return "awaiting_manual_feedback"
        if labels:
            label = labels[0]
            answer = dict(observation.question.options)[label]
            try:
                current_answer = join_blocks(session.ocr(frame, observation.option_rois[label]), .8)
            except QuizError:
                return "awaiting_manual_feedback"
            if normalize(current_answer.replace("\n", "")) != normalize(answer):
                return "awaiting_manual_feedback"
            require_active()
            outcome = "correct" if label == selected_label else "wrong"
            evidence = f"同一题目出现唯一正确对勾：选项{label}。"
            store.feedback(attempt_id, outcome, evidence, source="game_ui")
            store.learn_revealed_answer(observation.question, answer, evidence)
            return outcome
        sleep(.1)
    return "awaiting_manual_feedback"


def answer_once(reader, session, store: QuizStore, *, provider=None, execute=False,
                layout_confirmed=False, allow_trial=False, max_daily_calls=30, random_answer=False,
                receipt_dir: Path = Path("data/receipts"), sleep=time.sleep,
                should_stop=None, expected_view_token=None, random_percentage=None):
    def require_active():
        if should_stop is not None and should_stop():
            raise QuizError("Operation cancelled; no game click sent.")

    require_active()
    if execute and not layout_confirmed:
        raise QuizError("Inspect the overlay/OCR first, then explicitly pass --layout-confirmed.")
    first = read_for_answer(reader)
    sleep(0.25)
    stable = recheck_for_answer(first, read_for_answer(reader))
    require_active()
    if expected_view_token is not None and stable.question.view_token != expected_view_token:
        raise ReadError("题目已切换，当前提交取消；未调用 AI 或点击游戏。")
    decision = store.resolve(stable.question, provider, allow_trial=allow_trial,
                             max_daily_calls=max_daily_calls, random_answer=random_answer,
                             random_percentage=random_percentage)
    require_active()  # Especially important after the potentially slow, paid API request.
    result = {"decision": decision.to_dict(), "click_sent": False, "outcome": "not_submitted"}
    if not execute or not decision.can_submit:
        return result
    require_active()
    # AI may take time; never use an old frame or letter-to-button mapping.
    current = recheck_for_answer(stable, read_for_answer(reader))
    remaining = random.uniform(*INPUT_PAUSE_SECONDS)
    while remaining > 0:
        require_active()
        interval = min(.1, remaining)
        sleep(interval)
        remaining -= interval
    require_active()
    final = recheck_for_answer(current, read_for_answer(reader))
    if not final.question.is_readable and decision.origin not in {"random_unreadable", "bank_unreadable"}:
        decision = store.resolve(final.question, random_percentage=100)
        result = {**result, "decision": decision.to_dict()}
    label = final.question.label_for(decision.answer_text)
    if label is None or label != decision.label:
        raise QuizError("Answer is not uniquely mapped to the current option.")
    x, y, w, h = final.buttons[label]
    if min(w, h) <= 0 or min(x, y) < 0 or x + w > final.size[0] or y + h > final.size[1]:
        raise QuizError("Answer button is outside screenshot.")
    if any(correct_choice(final.frame, box) for box in final.buttons.values()):
        raise ReadError("当前题目已显示正确答案，未再次点击。")
    point = random_click_point(final.buttons[label])
    receipt = receipt_dir / f"{decision.attempt_id}.json"
    result.update(target=list(point), outcome="prepared", question_key=final.question.key)
    # Persist intent before claiming or touching the game; disk errors must prevent a click.
    write_json(receipt, result)
    require_active()
    if not store.claim_submission(decision, final.question):
        raise QuizError("Submission guard rejected this view: already submitted, stale or awaiting feedback.")
    try:
        require_active()
        session.click(*point)
    except BaseException:
        store.feedback(decision.attempt_id, "unknown", "Click interrupted/failed; delivery cannot be determined.", source="game_ui")
        result.update(outcome="unknown")
        write_json(receipt, result)
        raise
    result.update(click_sent=True, outcome="awaiting_manual_feedback", receipt=str(receipt))
    write_json(receipt, result)
    result = {**result, "outcome": record_revealed_feedback(session, store, final, decision.attempt_id, label,
                                                          sleep=sleep, require_active=require_active)}
    write_json(receipt, result)
    return result
