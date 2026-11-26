"""200-ring navigation and continuation; no combat or unknown NPC input."""
from __future__ import annotations

import random
import re
import time

from daily_tasks import DailyRunner, DailyStopped, compact
from local_vision import scaled_roi
from quiz_core import QuizError
from quiz_vision import INPUT_PAUSE_SECONDS, ReadError, join_blocks
from puzzle_tasks import PuzzleSolver, puzzle_visible

RING_NAME = "200环任务链"
RING_TRACKER = (677, 104, 151, 210)
ACTIVITY_GRID = (250, 110, 462, 240)
CLAIM_NPC = "护园大将"
CLAIM_NPC_NAME = (585, 172, 160, 33)
CLAIM_NPC_OPTIONS = (544, 278, 244, 93)
CLAIM_OPTION = "有什么可以帮助你的(领取200环)"
MAX_SCROLLS = 6
SWIPE_MS = 400
STALL_SECONDS = 180
QUIZ_RETRY_PAUSE_SECONDS = (1.0, 2.0)
MONSTER_ACTIONS = {"夺魄妖王": "看你往哪里跑。", "斗志幻影": "切磋一番"}
MONSTER_TITLE = (575, 170, 150, 40)
MONSTER_OPTIONS = (535, 275, 245, 115)
QUIZ_FAILURE_NAME = (430, 290, 160, 30)
QUIZ_FAILURE_DISMISS = (260, 395, 290, 55)
DAILY_TAB = (680, 78, 60, 28)
EVENTS_TAB = (755, 78, 65, 28)
TAB_BRIGHTNESS = 150


def daily_tracker_selected(view):
    if not world_visible(view):
        return None
    # shortcut: the selected tab has a pale upper strip in this 16:9 skin; recalibrate if the skin changes.
    regions = [scaled_roi(roi, view.width, view.height) for roi in ((688, 83, 55, 3), (760, 83, 55, 3))]
    bright = [float((view.frame[y:y+h, x:x+w].mean(axis=2) > TAB_BRIGHTNESS).mean()) for x, y, w, h in regions]
    if bright[0] > .8 and bright[1] < .2:
        return True
    if bright[1] > .8 and bright[0] < .2:
        return False
    return None


def quiz_failure_visible(view):
    return compact(view.text(QUIZ_FAILURE_NAME)) in ("诗词鬼", "歌王", "问题鬼")


def monster_dialog(view):
    name = compact(view.text(MONSTER_TITLE))
    if name not in MONSTER_ACTIONS:
        return None
    view.find(name, MONSTER_TITLE)
    view.find("再见", MONSTER_OPTIONS)
    return view.find(MONSTER_ACTIONS[name], MONSTER_OPTIONS)


def ring_tracker(view):
    """Return the first ring title's count and box; a None count means wait, not an input target."""
    blocks = view.inside(RING_TRACKER)
    if not blocks:
        return None
    anchor = min(blocks, key=lambda b: b.box[1] + b.box[3] / 2)
    row = [b for b in blocks if abs(b.box[1] + b.box[3] / 2 - anchor.box[1] - anchor.box[3] / 2)
           <= min(b.box[3], anchor.box[3]) * .45]
    text = compact(join_blocks(row, .8))
    if not text.startswith(RING_NAME):
        return None
    # shortcut: the total is fixed at 200; require '/' to distinguish a complete current count from clipped digits.
    match = re.match(re.escape(RING_NAME) + r"\(([0-9]+)/", text)
    current = int(match[1]) if match else None
    if match and (not 1 <= current <= 200 or match[1] != str(current)):
        raise QuizError("200环任务进度无法确认，未点击。")
    left, top = min(b.box[0] for b in row), min(b.box[1] for b in row)
    right = max(b.box[0] + b.box[2] for b in row)
    bottom = max(b.box[1] + b.box[3] for b in row)
    return current, (left, top, right - left, bottom - top)


def ring_activity_entry(view):
    if not view.activity_panel or view.sweep_panel:
        raise QuizError("活动面板已变化，未继续滑动或点击。")
    titles = [b for b in view.inside(ACTIVITY_GRID) if compact(b.text) == RING_NAME]
    if len(titles) > 1:
        raise QuizError("无法唯一识别200环任务卡片。")
    if not titles or titles[0].box[1] + 60 > 350:
        return None
    x, y, _, _ = titles[0].box
    left = 250 if x < 480 else 483
    blocks = view.inside((left + 56, y + 18, 99, 42))
    lines = tuple(map(compact, join_blocks(blocks, .8).splitlines())) if blocks else ()
    counts = [re.fullmatch(r"已领取:([0-9]{1,3})/([0-9]{1,3})", line) for line in lines if line.startswith("已领取")]
    if len(counts) != 1 or counts[0] is None:
        raise QuizError("200环卡片的已领取次数无法确认，未点击前往。")
    claimed, total = map(int, counts[0].groups())
    if total == 0 or claimed > total:
        raise QuizError("200环卡片的已领取次数无效，未点击前往。")
    if claimed == total and not any(re.fullmatch(r"环数:([1-9][0-9]?|1[0-9]{2})/200", line) for line in lines):
        raise QuizError(f"200环已领取 {claimed}/{total}，没有可领取的新任务，也未确认有进行中的任务。")
    buttons = [b for b in view.inside((left + 155, y + 18, 74, 42))
               if compact(b.text) in ("前往", "前注")]
    if len(buttons) != 1:
        raise QuizError("200环前往按钮无法确认，未点击；无按钮不代表任务完成。")
    return (claimed, total), buttons[0].box


def claim_npc_visible(view):
    return view.text(CLAIM_NPC_NAME) == CLAIM_NPC


def ring_started(view):
    return (monster_dialog(view) or puzzle_visible(view) or quiz_visible(view) or quiz_failure_visible(view)
            or (world_visible(view) and ring_tracker(view)))


def final_tracker_missing(view):
    return (daily_tracker_selected(view) is True and not view.activity_panel
            and not re.search(r"200环|环任务链", view.text(RING_TRACKER))
            and "自动寻路" not in view.text((275, 350, 280, 55)))


def world_visible(view):
    return "日常" in view.text(DAILY_TAB) and "活动" in view.text(EVENTS_TAB)


def quiz_visible(view):
    # Detect even a partial popup, so unreadable choices cannot fall through to a tracker click.
    labels = [view.text(roi) for roi in ((178, 271, 28, 32), (420, 271, 30, 32), (178, 332, 28, 32))]
    return any(text in ("A", "B", "C") for text in labels) or bool(
        re.search(r"\d+秒", view.text((600, 207, 65, 35))))


class RingRunner(DailyRunner):
    def __init__(self, session, runtime_dir, should_stop, on_result=None, *, answer_quiz=None,
                 on_quiz_retry=None, vision_options=None):
        super().__init__(session, runtime_dir, should_stop, on_result, vision_options=vision_options)
        self.folder = runtime_dir / "ring200"
        self.answer_quiz = answer_quiz
        self.on_quiz_retry = on_quiz_retry

    def select_daily_tracker(self, view):
        self.check_stop()
        self._check_tap_view(view)
        selected = daily_tracker_selected(view)
        if selected is None:
            raise QuizError("右侧日常／活动页签状态无法确认，未点击。")
        if selected:
            return view
        self.sleep(random.uniform(*INPUT_PAUSE_SECONDS))
        self.check_stop()
        current = self.read()
        if (current.width, current.height) != (view.width, view.height):
            raise QuizError("画面分辨率已变化，未切换日常页签。")
        if monster_dialog(current) or puzzle_visible(current) or quiz_visible(current) or quiz_failure_visible(current):
            return current
        selected = daily_tracker_selected(current)
        if selected is None:
            raise QuizError("右侧页签在等待期间无法确认，未点击。")
        if selected:
            return current
        self.emit("先切换右侧日常页签，再检查首项200环任务。")
        self.tap(current, current.find("日常", DAILY_TAB), urgent=True)
        return self.until(lambda v: quiz_visible(v) or quiz_failure_visible(v) or monster_dialog(v)
                          or puzzle_visible(v) or daily_tracker_selected(v) is True,
                          "右侧日常页签尚未选中，已暂停，未重复点击。", 10)

    def dismiss_quiz_failure(self, view):
        self.check_stop()
        self._check_tap_view(view)
        if not quiz_failure_visible(view):
            raise QuizError("未确认已校准的答错对话，未点击。")
        name = compact(view.text(QUIZ_FAILURE_NAME))
        self.sleep(random.uniform(*INPUT_PAUSE_SECONDS))
        self.check_stop()
        current = self.read()
        if (current.width, current.height) != (view.width, view.height):
            raise QuizError("画面分辨率已变化，未关闭答错对话。")
        if not quiz_failure_visible(current) or compact(current.text(QUIZ_FAILURE_NAME)) != name:
            return current
        self.emit(f"检测到{name}答错对话，关闭后重新识别题目作答。")
        self.tap(current, QUIZ_FAILURE_DISMISS, urgent=True)
        current = self.until(lambda v: not quiz_failure_visible(v),
                             "答错对话尚未关闭，已暂停，未重复点击。", 10)
        self.check_stop()
        if self.on_quiz_retry is not None:
            self.on_quiz_retry()
        return current

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
        if tracked[0] is None:
            return view, False
        self.sleep(random.uniform(*INPUT_PAUSE_SECONDS))
        self.check_stop()
        current = self.read()
        fresh = ring_tracker(current)
        if (current.width, current.height) != (view.width, view.height):
            raise QuizError("画面分辨率已变化，未点击200环，请重新检查。")
        if fresh is not None and fresh[0] is not None and fresh[0] < tracked[0]:
            raise QuizError("200环进度倒退或识别不稳定，未点击。")
        if (monster_dialog(current) or puzzle_visible(current) or quiz_visible(current) or quiz_failure_visible(current)
                or not world_visible(current) or daily_tracker_selected(current) is not True
                or fresh is None or fresh[0] != tracked[0]
                or "自动寻路" in current.text((275, 350, 280, 55))):
            self.emit("点击前任务或页面正在推进，取消本次点击，继续截图检查。")
            return current, False
        self.tap(current, fresh[1], urgent=True)  # The ordinary pause already preceded this fresh localization.
        return current, True

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
            entry = ring_activity_entry(view)
            if entry is not None:
                return self.go_to_ring(view, entry[0])
            if attempt == MAX_SCROLLS:
                break
            self.emit(f"在周月常中向上滑动寻找200环（{attempt + 1}/{MAX_SCROLLS}）。")
            self.scroll(view)
            self.sleep(1)
            view = self.read()
        raise QuizError("滑动后仍未找到完整的200环卡片，已暂停，请提供当前活动列表截图。")

    def go_to_ring(self, view, quota):
        current = self.refresh_ring_target(view)
        entry = ring_activity_entry(current)
        if entry is None or entry[0] != quota:
            raise QuizError("200环卡片或领取次数在等待期间变化，未点击前往。")
        self.emit("已找到200环任务链，点击前往。")
        self.tap(current, entry[1], urgent=True)
        if quota[0] < quota[1]:
            current = self.until(claim_npc_visible, "前往后未确认护园大将对话，未重复点击。")
            return self.claim_ring(current)
        return self.until(ring_started, "前往后未确认200环追踪，未重复点击。")

    def refresh_ring_target(self, view):
        self.check_stop()
        self._check_tap_view(view)
        self.sleep(random.uniform(*INPUT_PAUSE_SECONDS))
        self.check_stop()
        current = self.read()
        if (current.width, current.height) != (view.width, view.height):
            raise QuizError("画面分辨率已变化，未发送200环输入。")
        return current

    def claim_ring(self, view):
        view.find(CLAIM_OPTION, CLAIM_NPC_OPTIONS)
        current = self.refresh_ring_target(view)
        if not claim_npc_visible(current):
            raise QuizError("护园大将对话已变化，未点击领取200环。")
        button = current.find(CLAIM_OPTION, CLAIM_NPC_OPTIONS)
        self.emit("已确认护园大将的领取选项，点击领取新的200环任务。")
        self.tap(current, button, urgent=True)
        return self.until(lambda v: not claim_npc_visible(v) and ring_started(v),
                          "领取后未确认新的200环追踪或任务画面，未重复领取。")

    def start_monster_battle(self, view):
        self.check_stop()
        self._check_tap_view(view)
        if monster_dialog(view) is None:
            raise QuizError("未确认战斗怪对话，未点击。")
        name = compact(view.text(MONSTER_TITLE))
        self.emit(f"检测到{name}，确认后点击「{MONSTER_ACTIONS[name]}」开始战斗。")
        self.sleep(random.uniform(*INPUT_PAUSE_SECONDS))
        self.check_stop()
        current = self.read()
        button = monster_dialog(current)
        if (button is None or compact(current.text(MONSTER_TITLE)) != name
                or (current.width, current.height) != (view.width, view.height)):
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

    def handle_auto_step(self, view):
        if monster_dialog(view):
            view = self.start_monster_battle(view)
            self.emit("已点击开战，等待自动战斗与下一环，不操作战斗。")
            return view
        return PuzzleSolver(self).run(view)

    def pause(self, progress):
        message = "200环环数超过3分钟未变化，疑似任务停住，请检查答题、战斗对话、技能书购买或组队状态；未自动购买或退队。"
        self.save(message)
        self.emit(message)
        return {"reason": "needs_attention", "progress": progress, "message": message,
                "diagnostics": str(self.folder)}

    def completed(self, message):
        self.emit(message)
        return {"reason": "completed", "progress": 200, "message": message}

    def run(self):
        progress = previous = None
        entry_pending = True
        is_occluded = final_missing = False
        deadline = time.monotonic() + STALL_SECONDS
        try:
            view = self.read()
            if not (monster_dialog(view) or puzzle_visible(view) or quiz_visible(view) or quiz_failure_visible(view)):
                if world_visible(view):
                    view = self.select_daily_tracker(view)
                if not (monster_dialog(view) or puzzle_visible(view) or quiz_visible(view) or quiz_failure_visible(view)) \
                        and not (world_visible(view) and ring_tracker(view)):
                    view = self.open_ring(view)
                    entry_pending = False
            while True:
                self.check_stop()
                was_final_missing, final_missing = final_missing, False
                tracked = ring_tracker(view)
                if tracked and tracked[0] is not None:
                    current = tracked[0]
                    if progress is not None and current < progress:
                        raise QuizError("200环进度倒退或识别不稳定，已暂停，未点击。")
                    if current != progress:
                        if progress is not None:
                            entry_pending = False
                        progress = current
                        deadline = time.monotonic() + STALL_SECONDS
                        self.emit(f"200环当前进度 {progress}/200；战斗和寻路不操作。")
                    if progress == 200:
                        return self.completed("200环已完成（200/200），结束助手流程。")
                if quiz_failure_visible(view):
                    view = self.dismiss_quiz_failure(view)
                    entry_pending = world_visible(view) and not quiz_visible(view)
                    if entry_pending:
                        view = self.select_daily_tracker(view)
                    previous = None
                    deadline = time.monotonic() + STALL_SECONDS
                    continue
                if monster_dialog(view) or puzzle_visible(view):
                    view = self.handle_auto_step(view)
                    entry_pending = False
                    previous = None
                    deadline = time.monotonic() + STALL_SECONDS
                    continue
                if quiz_visible(view):
                    entry_pending = False
                    previous = None
                    try:
                        view = self.answer_current_quiz()
                    except ReadError as exc:
                        self.check_stop()
                        self.reader.invalidate()
                        self.emit(f"答题校验未通过，本次未点击，重新截图识题：{exc}")
                        if time.monotonic() >= deadline:
                            raise QuizError("200环答题校验持续失败超时，已暂停：" + str(exc)) from exc
                        self.sleep(random.uniform(*QUIZ_RETRY_PAUSE_SECONDS))
                        view = self.read()
                        continue
                    deadline = time.monotonic() + STALL_SECONDS
                    continue
                if tracked and tracked[0] is None:
                    if not is_occluded:
                        self.emit("200环当前数字被遮挡或未读清，等待重新截图，不点击。")
                    is_occluded = True
                    previous = None
                    self.sleep(self.options.probe_seconds)
                    view = self.read()
                    continue
                if is_occluded:
                    deadline = time.monotonic() + STALL_SECONDS
                    is_occluded = False
                final_missing = progress == 199 and tracked is None and final_tracker_missing(view)
                if final_missing and was_final_missing:
                    return self.completed("第199环后日常追踪已消失，200环已完成。")
                is_world = bool(tracked and world_visible(view))
                pathfinding = is_world and "自动寻路" in view.text((275, 350, 280, 55))
                if pathfinding:
                    entry_pending = False
                if entry_pending and is_world and tracked[0] == previous:
                    self.emit(f"点击右侧200环任务，继续第 {progress} 环。")
                    current, clicked = self.tap_first_ring(view, tracked)
                    if not clicked:
                        view = current
                        entry_pending = False
                        previous = None
                        self.sleep(self.options.poll_seconds)
                        continue
                    entry_pending = False
                    deadline = time.monotonic() + STALL_SECONDS
                previous = tracked[0] if is_world else None
                if not final_missing and time.monotonic() >= deadline:
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
