"""Offline contract/safety tests. These do NOT emulate native OCR accuracy or a real game."""
import copy
import hashlib
import json
import random
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from maa_adapter import ROOT, MaaSession, load_config, load_maa, select_device, wait_job
from quiz_core import AIAnswer, QuizError, QuizStore
from quiz_vision import (DeviceLock, Layout, QuizReader, TextBlock, answer_once, assert_same,
                         join_blocks, parse_fields)
from setup_ocr import verify_file, verify_models


def fields():
    text = {"question": "第1/10题：测试等级为34级，不正确的是？"}
    text.update({f"label_{k}": k for k in "ABCD"})
    text.update({f"option_{k}": v for k, v in zip("ABCD", ["甲", "乙", "丙", "丁"])})
    return {k: [TextBlock(v, 0.99, (0, 0, 80, 20))] for k, v in text.items()}


def observation(layout=None, data=None, size=(834, 470), device="local-device"):
    return parse_fields(data or fields(), layout or Layout.load(ROOT / "config/dali.layout.json"),
                        size, device, "test-only-account", time.monotonic())


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.layout = Layout.load(ROOT / "config/dali.layout.json")

    def test_reference_rectangles(self):
        self.assertEqual(self.layout.scaled(834, 470)["question"], (279, 155, 353, 78))

    def test_double_resolution(self):
        self.assertEqual(self.layout.scaled(1668, 940)["question"], (558, 310, 706, 156))

    def test_normal_720p(self):
        self.assertEqual(set(self.layout.scaled(1280, 720)), set(fields()))

    def test_four_option_regions_include_text_without_icons_and_third_line(self):
        regions = self.layout.scaled(1600, 900)
        for name, px, py in (("option_A", 600, 520), ("option_C", 600, 718), ("option_D", 958, 685)):
            with self.subTest(region=name):
                x, y, w, h = regions[name]
                self.assertTrue(x <= px < x + w and y <= py < y + h)

    def test_parse_two_options_without_lower_buttons(self):
        data = fields()
        for label in "CD":
            data[f"label_{label}"] = [TextBlock("-", 0.3, (0, 0, 20, 20))]
            data[f"option_{label}"] = []
        result = observation(data=data)
        self.assertEqual(tuple(dict(result.question.options)), ("A", "B"))
        self.assertEqual(set(result.buttons), {"A", "B"})
        data["option_C"] = [TextBlock("丙", 0.5, (0, 0, 20, 20))]
        with self.assertRaises(QuizError):
            observation(data=data)

    def test_parse_three_options_and_reject_partial_fourth(self):
        data = fields()
        data["label_D"] = [TextBlock("-", 0.3, (0, 0, 20, 20))]
        data["option_D"] = []
        result = observation(data=data)
        self.assertEqual(tuple(dict(result.question.options)), ("A", "B", "C"))
        self.assertEqual(set(result.buttons), {"A", "B", "C"})
        data["option_D"] = [TextBlock("丁", 0.5, (0, 0, 20, 20))]
        with self.assertRaises(QuizError):
            observation(data=data)

    def test_reader_never_recognizes_legacy_title_region(self):
        self.layout.data["regions"]["title"] = [348, 70, 136, 24]
        rois = self.layout.scaled(834, 470)
        self.assertNotIn("title", rois)
        def ocr(frame, roi, *, single_line=False):
            name = next(k for k, rect in rois.items() if rect == roi)
            self.assertNotEqual(name, "title")
            return fields()[name]
        session = NS(capture=lambda: NS(shape=(470, 834, 3)), ocr=ocr, address="test-device")
        self.assertEqual(len(QuizReader(session, self.layout, "test-profile").read().question.options), 4)

    def test_random_reader_only_reads_number_and_existing_button_letters(self):
        rois = self.layout.scaled(834, 470)
        for count in (2, 3, 4):
            with self.subTest(count=count):
                calls = []
                def ocr(frame, roi, *, single_line=False):
                    calls.append(roi)
                    if roi[0:2] == rois["question"][0:2]:
                        return [TextBlock("第5/10题：", .99, (0, 0, 90, 20)),
                                TextBlock("读不清的题干", .1, (0, 30, 90, 20))]
                    label = next(k for k in "ABCD" if roi == rois[f"label_{k}"])
                    return [TextBlock(label if label in "ABCD"[:count] else "-", .99 if label in "ABCD"[:count] else .3, (0, 0, 20, 20))]
                session = NS(capture=lambda: NS(shape=(470, 834, 3)), ocr=ocr, address="test-device")
                result = QuizReader(session, self.layout, "test-profile", random_answer=True).read()
                self.assertEqual(dict(result.question.options), {k: f"选项{k}" for k in "ABCD"[:count]})
                self.assertEqual(set(result.buttons), set("ABCD"[:count]))
                self.assertEqual(len(calls), 5)
                self.assertTrue(all(roi not in [rois[f"option_{k}"] for k in "ABCD"] for roi in calls))

    def test_reject_portrait(self):
        with self.assertRaises(QuizError): self.layout.scaled(720, 1280)

    def test_reject_ultrawide(self):
        with self.assertRaises(QuizError): self.layout.scaled(1600, 720)

    def test_reject_out_of_bounds(self):
        data = copy.deepcopy(self.layout.data)
        data["regions"]["question"] = [830, 10, 30, 30]
        with self.assertRaises(QuizError): Layout(data)

    def test_reject_missing_option_region(self):
        data = copy.deepcopy(self.layout.data)
        del data["regions"]["option_D"]
        with self.assertRaises(QuizError): Layout(data)

    def test_reject_weak_threshold(self):
        data = copy.deepcopy(self.layout.data)
        data["min_ocr_score"] = 0.1
        with self.assertRaises(QuizError): Layout(data)


class ParsingTests(unittest.TestCase):
    def test_parse_four_options(self):
        o = observation()
        self.assertEqual(dict(o.question.options), dict(zip("ABCD", "甲乙丙丁")))
        self.assertIn("34", o.question.stem)
        self.assertIn("不正确", o.question.stem)

    def test_multiline_option_is_joined(self):
        data = fields()
        data["option_D"] = [TextBlock("测试抗性", .95, (0, 0, 50, 20)), TextBlock("卡", .95, (0, 30, 20, 20))]
        self.assertEqual(dict(observation(data=data).question.options)["D"], "测试抗性卡")

    def test_fragment_reading_order(self):
        blocks = [TextBlock("右", .9, (60, 1, 20, 20)), TextBlock("左", .9, (0, 2, 20, 20)),
                  TextBlock("次行", .9, (0, 30, 40, 20))]
        self.assertEqual(join_blocks(blocks, .8), "左右\n次行")

    def test_low_confidence_digit_is_not_dropped(self):
        data = fields()
        data["question"].append(TextBlock("不", .2, (50, 30, 20, 20)))
        with self.assertRaises(QuizError): observation(data=data)

    def test_nan_confidence_rejected(self):
        with self.assertRaises(QuizError): join_blocks([TextBlock("不", float("nan"), (0, 0, 10, 10))], .8)

    def test_missing_field_rejected(self):
        data = fields(); del data["option_C"]
        with self.assertRaises(QuizError): observation(data=data)

    def test_duplicate_option_rejected(self):
        data = fields(); data["option_B"] = data["option_A"]
        with self.assertRaises(QuizError): observation(data=data)

    def test_title_announcements_do_not_affect_question(self):
        data = fields(); data["title"] = [TextBlock("滚动公告", .1, (0, 0, 100, 20))]
        self.assertEqual(len(observation(data=data).question.options), 4)

    def test_wrong_label_rejected(self):
        data = fields(); data["label_D"] = data["label_C"]
        with self.assertRaises(QuizError): observation(data=data)

    def test_question_index_required(self):
        data = fields(); data["question"] = [TextBlock("普通文字不是完整题目", .99, (0, 0, 100, 20))]
        with self.assertRaises(QuizError): observation(data=data)

    def test_index_must_fit_total(self):
        data = fields(); data["question"] = [TextBlock("第11/10题：题目", .99, (0, 0, 100, 20))]
        with self.assertRaises(QuizError): observation(data=data)

    def test_instance_stable_across_reads(self):
        self.assertEqual(observation().question.view_token, observation().question.view_token)

    def test_instances_separated_by_device(self):
        self.assertNotEqual(observation(device="one").question.view_token, observation(device="two").question.view_token)

    def test_freshness_guard(self):
        first = observation(); second = replace(first, captured_at=time.monotonic() - 60)
        with self.assertRaises(QuizError): assert_same(first, second)

    def test_size_change_guard(self):
        first = observation(); second = replace(first, size=(1280, 720))
        with self.assertRaises(QuizError): assert_same(first, second)


class Provider:
    def __init__(self): self.calls = 0
    def answer(self, q, rejected, image=None):
        self.calls += 1
        return AIAnswer("answer", "甲", "Synthetic test fixture", "offline-test")


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.store = QuizStore(":memory:")
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = Provider()
        self.reader = NS(read=MagicMock(side_effect=lambda: observation()))
        self.session = NS(click=MagicMock())
        self.kw = dict(provider=self.provider, receipt_dir=Path(self.tmp.name), sleep=lambda _: None)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def run_answer(self, **extra):
        return answer_once(self.reader, self.session, self.store, **dict(self.kw, **extra))

    def assert_click_inside_button(self, label):
        self.session.click.assert_called_once()
        px, py = self.session.click.call_args.args
        x, y, w, h = observation().buttons[label]
        self.assertTrue(x < px < x + w - 1 and y < py < y + h - 1)

    def test_default_never_clicks(self):
        result = self.run_answer(allow_trial=True)
        self.assertFalse(result["click_sent"])
        self.session.click.assert_not_called()

    def test_cache_miss_no_provider_no_network(self):
        result = self.run_answer(provider=None)
        self.assertEqual(result["decision"]["status"], "miss")
        self.assertEqual(self.provider.calls, 0)

    def test_random_preview_and_submit_reuse_choice_without_ai_or_bank(self):
        with patch("quiz_core.random.choice", return_value=("A", "甲")) as choose:
            preview = self.run_answer(random_answer=True)
            self.assertFalse(preview["click_sent"])
            submitted = self.run_answer(random_answer=True, execute=True, layout_confirmed=True)
        self.assertEqual(choose.call_count, 1)
        self.assertEqual(preview["decision"]["answer_text"], submitted["decision"]["answer_text"])
        self.assertEqual(submitted["decision"]["origin"], "random")
        self.assert_click_inside_button("A")
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)
        self.store.feedback(submitted["decision"]["attempt_id"], "correct", "Test feedback")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM answers").fetchone()[0], 0)

    def test_random_still_requires_layout_and_blocks_repeat_clicks(self):
        with self.assertRaises(QuizError):
            self.run_answer(random_answer=True, execute=True)
        self.reader.read.assert_not_called()
        self.run_answer(random_answer=True, execute=True, layout_confirmed=True)
        with self.assertRaises(QuizError):
            self.run_answer(random_answer=True, execute=True, layout_confirmed=True)
        self.assertEqual(self.session.click.call_count, 1)

    def test_click_points_vary_and_stay_inside_button(self):
        points = set()
        rng = random.Random(23)
        with patch("quiz_core.random.choice", return_value=("A", "甲")), patch(
            "quiz_vision.random.randint", side_effect=rng.randint
        ):
            for _ in range(20):
                store = QuizStore(":memory:")
                self.session.click.reset_mock()
                try:
                    result = answer_once(self.reader, self.session, store, random_answer=True,
                                         execute=True, layout_confirmed=True, **self.kw)
                finally:
                    store.close()
                point = tuple(result["target"])
                self.assertTrue(277 < point[0] < 449 and 250 < point[1] < 308)
                self.session.click.assert_called_once_with(*point)
                points.add(point)
        self.assertGreater(len(points), 1)

    def test_two_and_three_option_random_submission_only_clicks_existing_buttons(self):
        for count in (2, 3):
            with self.subTest(count=count):
                data = fields()
                data["question"] = [TextBlock(f"第{count}/10题：测试选项数量？", .99, (0, 0, 90, 20))]
                for label in "ABCD"[count:]:
                    data[f"label_{label}"] = []
                    data[f"option_{label}"] = []
                self.reader.read.side_effect = lambda: observation(data=data)
                self.session.click.reset_mock()
                label = "ABCD"[count - 1]
                with patch("quiz_core.random.choice", return_value=(label, "甲乙丙丁"[count - 1])):
                    result = self.run_answer(random_answer=True, execute=True, layout_confirmed=True)
                self.assertEqual(result["decision"]["label"], label)
                self.assert_click_inside_button(label)
                self.assertEqual(self.provider.calls, 0)

    def test_execute_requires_calibration_ack(self):
        with self.assertRaises(QuizError): self.run_answer(execute=True)
        self.reader.read.assert_not_called()
        self.assertEqual(self.provider.calls, 0)

    def test_auto_expected_question_change_prevents_ai_and_click(self):
        with self.assertRaises(QuizError):
            self.run_answer(execute=True, layout_confirmed=True, expected_view_token="previous-question")
        self.assertEqual(self.provider.calls, 0)
        self.session.click.assert_not_called()

    def test_candidate_needs_trial_opt_in(self):
        result = self.run_answer(execute=True, layout_confirmed=True)
        self.assertFalse(result["click_sent"])
        self.session.click.assert_not_called()

    def test_one_click_never_auto_verifies(self):
        result = self.run_answer(execute=True, layout_confirmed=True, allow_trial=True)
        self.assertTrue(result["click_sent"])
        self.assert_click_inside_button("A")
        self.assertEqual(result["outcome"], "awaiting_manual_feedback")
        self.assertEqual(self.store.db.execute("SELECT status FROM answers").fetchone()[0], "candidate")
        self.assertEqual(self.reader.read.call_count, 4)

    def test_cache_reused_without_second_api_call(self):
        self.run_answer()
        self.run_answer()
        self.assertEqual(self.provider.calls, 1)

    def test_repeated_same_view_does_not_click_twice(self):
        self.run_answer(execute=True, layout_confirmed=True, allow_trial=True)
        with self.assertRaises(QuizError):
            self.run_answer(execute=True, layout_confirmed=True, allow_trial=True)
        self.assertEqual(self.session.click.call_count, 1)

    def test_changed_question_after_ai_is_blocked(self):
        first = observation()
        data = fields(); data["question"] = [TextBlock("第2/10题：不同的问题？", .99, (0, 0, 90, 20))]
        self.reader.read.side_effect = [first, first, observation(data=data)]
        with self.assertRaises(QuizError): self.run_answer(execute=True, layout_confirmed=True, allow_trial=True)
        self.session.click.assert_not_called()

    def test_changed_order_after_ai_is_blocked(self):
        first = observation(); data = fields()
        data["option_A"], data["option_B"] = data["option_B"], data["option_A"]
        self.reader.read.side_effect = [first, first, observation(data=data)]
        with self.assertRaises(QuizError): self.run_answer(execute=True, layout_confirmed=True, allow_trial=True)
        self.session.click.assert_not_called()

    def test_unstable_initial_frames_do_not_call_ai(self):
        first = observation(); second = replace(first, size=(1280, 720))
        self.reader.read.side_effect = [first, second]
        with self.assertRaises(QuizError): self.run_answer()
        self.assertEqual(self.provider.calls, 0)

    def test_click_failure_records_unknown(self):
        self.session.click.side_effect = QuizError("driver failure")
        with self.assertRaises(QuizError): self.run_answer(execute=True, layout_confirmed=True, allow_trial=True)
        self.assertEqual(self.store.db.execute("SELECT outcome FROM attempts").fetchone()[0], "unknown")
        self.assertEqual(self.store.db.execute("SELECT status FROM answers").fetchone()[0], "needs_review")

    def test_disk_failure_prevents_claim_and_click(self):
        with patch("quiz_vision.write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.run_answer(execute=True, layout_confirmed=True, allow_trial=True)
        self.session.click.assert_not_called()
        self.assertIsNone(self.store.db.execute("SELECT submitted_at FROM attempts").fetchone()[0])


class MaaVersionTests(unittest.TestCase):
    def test_pinned_version_accepts_optional_v_prefix(self):
        toolkit = object()
        modules = {
            "maa.controller": NS(AdbController=object()),
            "maa.define": NS(MaaAdbScreencapMethodEnum=object(), MaaAdbInputMethodEnum=object()),
            "maa.pipeline": NS(JOCR=object()),
            "maa.resource": NS(Resource=object()),
            "maa.tasker": NS(Tasker=object()),
            "maa.toolkit": NS(Toolkit=toolkit),
        }
        with patch.dict("sys.modules", modules):
            for installed in ("5.15.0", "v5.15.0"):
                with self.subTest(installed=installed), patch("importlib.metadata.version", return_value=installed):
                    self.assertIs(load_maa()["Toolkit"], toolkit)

    def test_other_versions_still_rejected(self):
        for installed in ("5.14.0", "v5.16.0", "5.15.0rc1"):
            with self.subTest(installed=installed), patch("importlib.metadata.version", return_value=installed):
                with self.assertRaisesRegex(QuizError, "Use maafw==5.15.0"):
                    load_maa()


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg["runtime_dir"] = Path(self.tmp.name)
        self.job = NS(done=True, succeeded=True, get=lambda: None)
        self.ctrl = MagicMock()
        self.ctrl.connected = True
        self.ctrl.post_connection.return_value = self.job
        self.ctrl.post_click.return_value = self.job
        self.api = {"Toolkit": MagicMock(), "AdbController": MagicMock(return_value=self.ctrl),
                    "Screen": NS(Encode=2), "Input": NS(AdbShell=1),
                    "Resource": MagicMock(), "Tasker": MagicMock(), "JOCR": lambda **kw: NS(**kw)}
        self.device = NS(name="mock", adb_path="adb.exe", address="127.0.0.1:12345",
                         screencap_methods=2, input_methods=1, config={})
        self.api["Toolkit"].find_adb_devices.return_value = [self.device]

    def tearDown(self): self.tmp.cleanup()

    def test_unique_auto_connection(self):
        session = MaaSession(self.cfg, api=self.api).connect()
        self.assertEqual(session.address, self.device.address)
        self.ctrl.set_screenshot_use_raw_size.assert_called_once_with(True)
        self.ctrl.post_click.assert_not_called()

    def test_multiple_devices_rejected(self):
        with self.assertRaises(QuizError): select_device([self.device, NS(address="other")])

    def test_same_address_deduplicated(self):
        self.assertIs(select_device([self.device, self.device]), self.device)

    def test_explicit_selection(self):
        self.assertIs(select_device([NS(address="other"), self.device], self.device.address), self.device)

    def test_no_auto_first_device(self):
        with self.assertRaises(QuizError): select_device([])

    def test_manual_connection_requires_path_and_address(self):
        self.api["Toolkit"].find_adb_devices.return_value = []
        self.cfg.update(adb_path="C:/Android/adb.exe", address="127.0.0.1:45678")
        session = MaaSession(self.cfg, api=self.api).connect()
        self.assertEqual(session.address, self.cfg["address"])
        self.assertEqual(self.api["AdbController"].call_args.kwargs["input_methods"], 1)

    def test_input_disabled_by_default(self):
        session = MaaSession(self.cfg, api=self.api).connect()
        with self.assertRaises(QuizError): session.click(10, 10)
        self.ctrl.post_click.assert_not_called()

    def test_explicit_input(self):
        session = MaaSession(self.cfg, api=self.api, execute=True).connect()
        session.click(10, 20)
        self.ctrl.post_click.assert_called_once_with(10, 20)

    def test_empty_screenshot_rejected(self):
        session = MaaSession(self.cfg, api=self.api).connect()
        self.ctrl.post_screencap.return_value = self.job
        with self.assertRaises(QuizError): session.capture()

    def test_failed_job_rejected(self):
        with self.assertRaises(QuizError): wait_job(NS(done=True, succeeded=False), 1, "mock")

    def test_timeout(self):
        with self.assertRaises(QuizError): wait_job(NS(done=False), 0, "mock")

    def test_ocr_retains_low_confidence_results(self):
        session = MaaSession(self.cfg, api=self.api).connect()
        item = NS(text="不", score=.2, box=(1, 2, 10, 20))
        result = NS(nodes=[NS(recognition=NS(all_results=[item]))])
        session.tasker = MagicMock()
        session.tasker.post_recognition.return_value = NS(done=True, succeeded=True, get=lambda: result)
        blocks = session.ocr("mock-image", (0, 0, 100, 100))
        self.assertEqual(blocks[0].text, "不")
        self.assertEqual(blocks[0].score, .2)
        params = session.tasker.post_recognition.call_args.args[1]
        self.assertEqual(params.threshold, 0)
        self.assertFalse(params.only_rec)

    def test_pipeline_guard_has_no_click(self):
        session = MaaSession(self.cfg, api=self.api).connect()
        session.tasker = MagicMock()
        session.tasker.post_task.return_value = self.job
        session.guard_scene((10, 20, 100, 30))
        self.assertEqual(session.tasker.post_task.call_args.args[0], "DaliSceneGuard")
        self.ctrl.post_click.assert_not_called()

    def test_resource_binding_contract(self):
        session = MaaSession(self.cfg, api=self.api).connect()
        resource = self.api["Resource"].return_value
        resource.post_bundle.return_value = self.job
        with patch("setup_ocr.verify_models"):
            session.ensure_ocr()
        self.api["Tasker"].return_value.bind.assert_called_once_with(resource, self.ctrl)


class AssetAndLockTests(unittest.TestCase):
    def test_known_blob_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "asset"
            data = b"test bytes"
            path.write_bytes(data)
            sha = hashlib.sha1(b"blob 10\0" + data).hexdigest()
            verify_file(path, (10, sha))
            with self.assertRaises(QuizError): verify_file(path, (10, "0" * 40))
            with self.assertRaises(QuizError): verify_file(path, (11, sha))

    def test_models_missing_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(QuizError): verify_models(Path(temp))

    def test_lock_exclusion_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "adapter.lock"
            with DeviceLock(path):
                self.assertTrue(path.exists())
                with self.assertRaises(QuizError):
                    with DeviceLock(path): pass
                self.assertTrue(path.exists())
            self.assertFalse(path.exists())

    def test_lock_released_on_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "adapter.lock"
            with self.assertRaises(ValueError):
                with DeviceLock(path): raise ValueError("test")
            self.assertFalse(path.exists())

    def test_pipeline_resource_read_only(self):
        nodes = json.loads((ROOT / "assets/resource/pipeline/dali.json").read_text(encoding="utf-8"))
        self.assertTrue(all(n["action"] == "DoNothing" for n in nodes.values()))

    def test_unknown_config_key_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"; path.write_text('{"api_key": "not-a-real-key"}')
            with self.assertRaises(QuizError): load_config(path)


if __name__ == "__main__": unittest.main()
