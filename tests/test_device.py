"""Offline contract/safety tests. These do NOT emulate native OCR accuracy or a real game."""
import copy
import hashlib
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from maa_adapter import ROOT, MaaSession, load_config, select_device, wait_job
from quiz_core import AIAnswer, QuizError, QuizStore
from quiz_vision import (DeviceLock, Layout, TextBlock, answer_once, assert_same,
                         join_blocks, parse_fields)
from setup_ocr import verify_file, verify_models


def fields():
    text = {"title": "大理寺答题", "question": "第1/10题：测试等级为34级，不正确的是？"}
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
        self.assertEqual(self.layout.scaled(834, 470)["title"], (344, 65, 182, 31))

    def test_double_resolution(self):
        self.assertEqual(self.layout.scaled(1668, 940)["title"], (688, 130, 364, 62))

    def test_normal_720p(self):
        self.assertEqual(set(self.layout.scaled(1280, 720)), set(fields()))

    def test_reject_portrait(self):
        with self.assertRaises(QuizError): self.layout.scaled(720, 1280)

    def test_reject_ultrawide(self):
        with self.assertRaises(QuizError): self.layout.scaled(1600, 720)

    def test_reject_out_of_bounds(self):
        data = copy.deepcopy(self.layout.data)
        data["regions"]["title"] = [830, 10, 30, 30]
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

    def test_wrong_title_rejected(self):
        data = fields(); data["title"] = [TextBlock("安全验证", .99, (0, 0, 100, 20))]
        with self.assertRaises(QuizError): observation(data=data)

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

    def test_default_never_clicks(self):
        result = self.run_answer(allow_trial=True)
        self.assertFalse(result["click_sent"])
        self.session.click.assert_not_called()

    def test_cache_miss_no_provider_no_network(self):
        result = self.run_answer(provider=None)
        self.assertEqual(result["decision"]["status"], "miss")
        self.assertEqual(self.provider.calls, 0)

    def test_execute_requires_calibration_ack(self):
        with self.assertRaises(QuizError): self.run_answer(execute=True)
        self.reader.read.assert_not_called()
        self.assertEqual(self.provider.calls, 0)

    def test_candidate_needs_trial_opt_in(self):
        result = self.run_answer(execute=True, layout_confirmed=True)
        self.assertFalse(result["click_sent"])
        self.session.click.assert_not_called()

    def test_one_click_never_auto_verifies(self):
        result = self.run_answer(execute=True, layout_confirmed=True, allow_trial=True)
        self.assertTrue(result["click_sent"])
        self.session.click.assert_called_once_with(363, 279)
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
