"""200-ring navigation and continuation; no combat or unknown NPC input."""
from __future__ import annotations

import random
import re
import time

from daily_tasks import DailyRunner, DailyStopped, compact
from quiz_core import QuizError
from quiz_vision import INPUT_PAUSE_SECONDS, join_blocks
from puzzle_tasks import PuzzleSolver, puzzle_visible

RING_NAME = "200环任务链"
RING_TRACKER = (677, 104, 151, 210)
ACTIVITY_GRID = (250, 110, 462, 240)
MAX_SCROLLS = 6
SWIPE_MS = 400
STALL_SECONDS = 180
MONSTER_NAME = "夺魄妖王"
MONSTER_TITLE = (575, 170, 150, 40)
MONSTER_OPTIONS = (535, 275, 245, 115)


def monster_dialog(view):
    if compact(view.text(MONSTER_TITLE)) != MONSTER_NAME:
        return None
    view.find(MONSTER_NAME, MONSTER_TITLE)
    view.find("再见", MONSTER_OPTIONS)
    return view.find("看你往哪里跑。", MONSTER_OPTIONS)


def ring_tracker(view):
    blocks = view.inside(RING_TRACKER)
    if not blocks:
        return None
    anchor = min(blocks, key=lambda b: b.box[1] + b.box[3] / 2)
    row = [b for b in blocks if abs(b.box[1] + b.box[3] / 2 - anchor.box[1] - anchor.box[3] / 2)
           <= min(b.box[3], anchor.box[3]) * .45]
    text = compact(join_blocks(row, .8))
    if not text.startswith(RING_NAME):
        return None
    match = re.fullmatch(re.escape(RING_NAME) + r"\(([1-9]\d{0,2})/200\)?", text)
    if not match or int(match[1]) > 200:
        raise QuizError("200环任务进度无法确认，未点击。")
    left, top = min(b.box[0] for b in row), min(b.box[1] for b in row)
    right = max(b.box[0] + b.box[2] for b in row)
    bottom = max(b.box[1] + b.box[3] for b in row)
    return int(match[1]), (left, top, right - left, bottom - top)


def world_visible(view):
    return "日常" in view.text((680, 78, 60, 28)) and "活动" in view.text((755, 78, 65, 28))


def quiz_visible(view):
    # Detect even a partial popup, so unreadable choices cannot fall through to a tracker click.
    labels = [view.text(roi) for roi in ((178, 271, 28, 32), (420, 271, 30, 32), (178, 332, 28, 32))]
    return any(text in ("A", "B", "C") for text in labels) or bool(
        re.search(r"\d+秒", view.text((600, 207, 65, 35))))


class RingRunner(DailyRunner):
    def __init__(self, session, runtime_dir, should_stop, on_result=None, *, answer_quiz=None, vision_options=None):
        super().__init__(session, runtime_dir, should_stop, on_result, vision_options=vision_options)
        self.folder = runtime_dir / "ring200"
        self.answer_quiz = answer_quiz

    def scroll(self, view):
        self.check_stop()
        if not view.activity_panel or view.sweep_panel:
            raise QuizError("未确认活动列表，未滑动。")
        self.sleep(random.uniform(*INPUT_PAUSE_SECONDS))
        age = time.monotonic() - view.captured_at
        if (age < 0 or age > 10 or getattr(view, "_click_used", False)
                or getattr(view, "generation", self.reader.generation) != self.reader.generation):
            raise QuizError("200环滑动截图已过期或失效，未滑动。")
        view._click_used = True
        try:
            # shortcut: swipe stays inside this calibrated 16:9 card grid; recalibrate if the layout moves.
            self.session.swipe(round(600 * view.width / 834), round(325 * view.height / 470),
                               round(600 * view.width / 834), round(140 * view.height / 470), SWIPE_MS)
        finally:
            self.reader.invalidate()
        self.check_stop()

    def tap_first_ring(self, view, tracked):
        self.check_stop()
        self._check_tap_view(view)
        self.sleep(random.uniform(*INPUT_PAUSE_SECONDS))
        self.check_stop()
        current = self.read()
        fresh = ring_tracker(current)
        if (quiz_visible(current) or not world_visible(current) or fresh is None
                or fresh[0] != tracked[0] or (current.width, current.height) != (view.width, view.height)
                or "自动寻路" in current.text((275, 350, 280, 55))):
            raise QuizError("右侧第一项或页面已变化，未点击200环，请重新检查。")
        self.tap(current, fresh[1], urgent=True)  # The ordinary pause already preceded this fresh localization.

    def open_ring(self, view):
        if view.sweep_panel:
            self.tap(view, view.find("返回", (700, 430, 65, 38)))
            view = self.until(lambda v: v.activity_panel, "尚未退出扫帚列表。", 10)
        if not view.activity_panel:
            if not world_visible(view):
                raise QuizError("请从游戏主界面、活动面板或200环答题界面启动。")
            self.tap(view, (8, 108, 42, 42))
            view = self.until(lambda v: v.activity_panel, "未打开活动面板，未重复点击。", 10)
        if RING_NAME not in view.text(ACTIVITY_GRID):
            self.tap(view, view.find("周月常", (250, 50, 462, 65)))
            self.sleep(1)
            view = self.read()
        for attempt in range(MAX_SCROLLS + 1):
            if not view.activity_panel or view.sweep_panel:
                raise QuizError("活动面板已变化，未继续滑动或点击。")
            titles = [b for b in view.inside(ACTIVITY_GRID) if compact(b.text) == RING_NAME]
            if len(titles) > 1:
                raise QuizError("无法唯一识别200环任务卡片。")
            if titles and titles[0].box[1] + 60 <= 350:
                title = titles[0].box
                left = 250 if title[0] < 480 else 483
                buttons = [b for b in view.inside((left + 155, title[1] + 18, 74, 42))
                           if compact(b.text) in ("前往", "前注")]
                if len(buttons) != 1:
                    raise QuizError("200环前往按钮无法确认，未点击；无按钮不代表任务完成。")
                self.emit("已找到200环任务链，点击前往。")
                self.tap(view, buttons[0].box)
                return self.until(lambda v: monster_dialog(v) or puzzle_visible(v) or quiz_visible(v) or (world_visible(v) and ring_tracker(v)),
                                  "前往后未确认200环追踪，请提供领取NPC截图。")
            if attempt == MAX_SCROLLS:
                break
            self.emit(f"在周月常中向上滑动寻找200环（{attempt + 1}/{MAX_SCROLLS}）。")
            self.scroll(view)
            self.sleep(1)
            view = self.read()
        raise QuizError("滑动后仍未找到完整的200环卡片，已暂停，请提供当前活动列表截图。")

    def start_monster_battle(self, view):
        self.check_stop()
        self._check_tap_view(view)
        if monster_dialog(view) is None:
            raise QuizError("未确认战斗怪对话，未点击。")
        self.emit("检测到夺魄妖王，确认后点击「看你往哪里跑。」开始战斗。")
        self.sleep(random.uniform(*INPUT_PAUSE_SECONDS))
        self.check_stop()
        current = self.read()
        button = monster_dialog(current)
        if button is None or (current.width, current.height) != (view.width, view.height):
            raise QuizError("战斗怪对话在等待期间变化，未点击。")
        self.tap(current, button, urgent=True)
        return self.until(lambda v: monster_dialog(v) is None,
                          "开战后对话仍未关闭，已暂停，未重复点击。", 10)

    def answer_current_quiz(self):
        if self.answer_quiz is None:
            raise QuizError("检测到200环答题，但尚未配置答题处理，已暂停。")
        result = self.answer_quiz()
        self.reader.invalidate()
        self.check_stop()
        if not result.get("click_sent"):
            reason = (result.get("decision") or {}).get("reason", "请检查题库或答题设置")
            raise QuizError("200环本题没有可提交答案，已暂停：" + str(reason))
        self.emit("本题已提交一次；可靠正确标记已交给统一答题逻辑处理。")
        return self.until(lambda v: not quiz_visible(v), "答题后弹窗仍未关闭，未重复提交。", 10)

    def handle_auto_step(self, view, progress):
        tracked = ring_tracker(view)
        current_ring = tracked[0] if tracked else progress
        if monster_dialog(view):
            view = self.start_monster_battle(view)
            self.emit("已点击开战，等待自动战斗与下一环，不操作战斗。")
        else:
            view = PuzzleSolver(self).run(view)
        if current_ring is None:
            resumed = ring_tracker(view)
            current_ring = resumed[0] if resumed else None
        return view, current_ring

    def pause(self, progress):
        message = ("当前为第200环，尚无已校准的完成凭据，请人工核对。" if progress == 200 else
                   "200环进度超过3分钟未变化，可能在战斗、答题或NPC处停留，请检查现场。")
        self.save(message)
        self.emit(message)
        return {"reason": "needs_attention", "progress": progress, "message": message,
                "diagnostics": str(self.folder)}

    def run(self):
        progress = last_clicked = previous = None
        deadline = time.monotonic() + STALL_SECONDS
        try:
            view = self.read()
            if not (monster_dialog(view) or puzzle_visible(view) or quiz_visible(view)) and not (world_visible(view) and ring_tracker(view)):
                view = self.open_ring(view)
            while True:
                self.check_stop()
                tracked = ring_tracker(view)
                if tracked:
                    current = tracked[0]
                    if progress is not None and current < progress:
                        raise QuizError("200环进度倒退或识别不稳定，已暂停，未点击。")
                    if current != progress:
                        progress = current
                        deadline = time.monotonic() + STALL_SECONDS
                        self.emit(f"200环当前进度 {progress}/200；战斗和寻路不操作。")
                if monster_dialog(view) or puzzle_visible(view):
                    view, last_clicked = self.handle_auto_step(view, progress)
                    previous = None
                    deadline = time.monotonic() + STALL_SECONDS
                    continue
                if quiz_visible(view):
                    view = self.answer_current_quiz()
                    last_clicked = progress
                    previous = None
                    continue
                if (tracked and world_visible(view) and tracked[0] == previous and tracked[0] != last_clicked
                        and "自动寻路" not in view.text((275, 350, 280, 55))):
                    self.emit(f"点击右侧200环任务，继续第 {progress} 环。")
                    self.tap_first_ring(view, tracked)
                    last_clicked = progress
                    deadline = time.monotonic() + STALL_SECONDS
                previous = tracked[0] if tracked and world_visible(view) else None
                if time.monotonic() >= deadline:
                    return self.pause(progress)
                self.sleep(self.options.probe_seconds)
                view = self.read()
        except DailyStopped:
            return {"reason": "stopped", "progress": progress}
        except QuizError as exc:
            self.save(str(exc))
            raise
        finally:
            self.save_metrics()
