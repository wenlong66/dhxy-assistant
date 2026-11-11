"""Strict Dali scene parsing and one-question execution. No automatic correctness claims."""
from __future__ import annotations

import json
import math
import os
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

from quiz_core import Question, QuizError, QuizStore, digest, game_day, normalize


@dataclass(frozen=True)
class TextBlock:
    text: str
    score: float
    box: tuple[int, int, int, int]


def join_blocks(blocks: list[TextBlock], minimum: float) -> str:
    if not blocks or any(not b.text.strip() or not math.isfinite(b.score) or b.score < minimum for b in blocks):
        raise QuizError("Missing/low-confidence OCR text; inspect the ROI, do not guess.")
    if any(len(b.box) != 4 or b.box[2] <= 0 or b.box[3] <= 0 for b in blocks):
        raise QuizError("Invalid OCR bounding box.")
    # Group slightly skewed fragments of the same line, then order left-to-right.
    rows = []
    for b in sorted(blocks, key=lambda b: b.box[1] + b.box[3] / 2):
        center = b.box[1] + b.box[3] / 2
        if rows and abs(center - rows[-1][0]) <= min(b.box[3], rows[-1][1]) * 0.45:
            rows[-1][2].append(b)
        else:
            rows.append([center, b.box[3], [b]])
    return "\n".join("".join(b.text.strip() for b in sorted(row[2], key=lambda b: b.box[0])) for row in rows)


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

    @property
    def signature(self):
        return digest([self.question.view_token, self.size, self.buttons])


def option_labels(fields, minimum):
    # Count through the last visible option; gaps and partially read answers still fail validation.
    labels = "AB"
    for k in "CD":
        text = any(b.text.strip() for b in fields.get(f"option_{k}", []))
        label = any(b.text.strip() and (b.score >= minimum or re.search(r"\w", b.text))
                    for b in fields.get(f"label_{k}", []))
        if text or label:
            labels = "ABCD"[:"ABCD".index(k) + 1]
    return labels


def parse_fields(fields, layout: Layout, size, device_id: str, scope: str,
                 captured_at: float, frame=None) -> Observation:
    labels = option_labels(fields, layout.minimum)
    names = ["question", *[f"label_{k}" for k in labels], *[f"option_{k}" for k in labels]]
    text = {}
    for name in names:
        try:
            text[name] = join_blocks(fields.get(name, []), layout.minimum)
        except QuizError as exc:
            raise QuizError(f"OCR 区域 {name} 未通过：{exc}") from exc
    for label in labels:
        if normalize(text[f"label_{label}"]).upper() != label:
            raise QuizError(f"Option {label} label is missing/misaligned; inspect the layout.")
    stem = unicodedata.normalize("NFKC", text["question"])
    match = re.match(r"^第\s*(\d+)\s*/\s*(\d+)\s*题\s*:\s*(.+)$", stem, re.S)
    if not match or not 1 <= int(match[1]) <= int(match[2]) <= 100:
        raise QuizError("Question number or full question text was not recognized.")
    # Stable across process restarts: same account/day/instance/question number cannot be re-clicked.
    instance = digest([game_day(), scope, device_id, int(match[1]), int(match[2])])
    options = tuple((k, re.sub(r"\s*\n\s*", "", text[f"option_{k}"])) for k in labels)
    question = Question(stem, options, instance, scope=scope)
    buttons = layout.scaled(*size, group="buttons")
    return Observation(question, tuple(size), {k: buttons[k] for k in labels}, captured_at, frame)


class QuizReader:
    def __init__(self, session, layout: Layout, scope: str):
        self.session, self.layout, self.scope = session, layout, scope

    def inspect(self):
        frame = self.session.capture()
        captured_at = time.monotonic()
        height, width = frame.shape[:2]
        rois = self.layout.scaled(width, height)
        fields = {name: self.session.ocr(frame, roi, single_line=name.startswith("label_"))
                  for name, roi in rois.items()}
        labels = option_labels(fields, self.layout.minimum)
        if len(labels) < 4:
            # Two/three-choice questions have no icons; include text next to the letter.
            for label in labels:
                lx, _, lw, _ = rois[f"label_{label}"]
                x, y, w, h = rois[f"option_{label}"]
                left = lx + lw + round(5 * width / self.layout.width)
                rois[f"option_{label}"] = (left, y, x + w - left, h)
                fields[f"option_{label}"] = self.session.ocr(frame, rois[f"option_{label}"])
        return frame, captured_at, fields, rois

    def read(self):
        frame, captured, fields, rois = self.inspect()
        return parse_fields(fields, self.layout, (frame.shape[1], frame.shape[0]),
                            self.session.address, self.scope, captured, frame)


def assert_same(first: Observation, current: Observation, max_age: float = 20):
    age = time.monotonic() - current.captured_at
    if current.signature != first.signature or age < 0 or age > max_age:
        raise QuizError("Question/layout changed or screenshot is stale; no click performed.")


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


def answer_once(reader, session, store: QuizStore, *, provider=None, execute=False,
                layout_confirmed=False, allow_trial=False, max_daily_calls=30, random_answer=False,
                receipt_dir: Path = Path("data/receipts"), sleep=time.sleep,
                should_stop=None, expected_view_token=None):
    def require_active():
        if should_stop is not None and should_stop():
            raise QuizError("Operation cancelled; no game click sent.")

    require_active()
    if execute and not layout_confirmed:
        raise QuizError("Inspect the overlay/OCR first, then explicitly pass --layout-confirmed.")
    first = reader.read()
    sleep(0.25)
    stable = reader.read()
    assert_same(first, stable)
    require_active()
    if expected_view_token is not None and stable.question.view_token != expected_view_token:
        raise QuizError("题目已切换，自动提交已暂停；未调用 AI 或点击游戏。")
    decision = store.resolve(stable.question, provider, allow_trial=allow_trial,
                             max_daily_calls=max_daily_calls, random_answer=random_answer)
    require_active()  # Especially important after the potentially slow, paid API request.
    result = {"decision": decision.to_dict(), "click_sent": False, "outcome": "not_submitted"}
    if not execute or not decision.can_submit:
        return result
    require_active()
    # AI may take time; never use an old frame or letter-to-button mapping.
    current = reader.read()
    assert_same(stable, current)
    sleep(0.25)
    final = reader.read()
    assert_same(current, final)
    label = final.question.label_for(decision.answer_text)
    if label is None or label != decision.label:
        raise QuizError("Answer is not uniquely mapped to the current option.")
    x, y, w, h = final.buttons[label]
    point = (x + w // 2, y + h // 2)
    if not (0 <= point[0] < final.size[0] and 0 <= point[1] < final.size[1]):
        raise QuizError("Target outside screenshot.")
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
    # A successful input job is NOT a correct answer. No speculative auto-feedback.
    return result
