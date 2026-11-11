"""Offline regression tests for interrupted OCR model downloads."""
import hashlib
import io
import tempfile
import unittest
from http.client import IncompleteRead, RemoteDisconnected
from pathlib import Path
from unittest.mock import MagicMock, patch

import setup_ocr
from quiz_core import QuizError


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.data = {name: name.encode("ascii") for name in setup_ocr.FILES}
        specs = {
            name: (len(data), hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest())
            for name, data in self.data.items()
        }
        self.specs = patch.dict(setup_ocr.FILES, specs)
        self.specs.start()
        self.addCleanup(self.specs.stop)

    def response(self, url, timeout):
        name = url.full_url.partition("?")[0].rsplit("/", 1)[1]
        return io.BytesIO(self.data[name])

    def test_retries_connection_closed_before_response(self):
        disconnected = RemoteDisconnected("Remote end closed connection without response")
        with tempfile.TemporaryDirectory() as temp, patch("time.sleep"), patch(
            "setup_ocr.urlopen", side_effect=[disconnected] + [io.BytesIO(data) for data in self.data.values()]
        ) as request:
            destination = Path(temp) / "ocr"
            setup_ocr.install(destination)
            setup_ocr.verify_models(destination)
            self.assertEqual(request.call_count, len(self.data) + 1)
            self.assertTrue(request.call_args_list[0].args[0].full_url.startswith(setup_ocr.BASE_URL))
            for call in request.call_args_list[1:]:
                download = call.args[0]
                self.assertTrue(download.full_url.startswith("https://api.github.com/repos/MaaXYZ/MaaCommonAssets/contents/"))
                self.assertTrue(download.full_url.endswith(f"?ref={setup_ocr.REVISION}"))
                self.assertEqual(download.get_header("Accept"), "application/vnd.github.raw+json")

    def test_retries_interrupted_or_truncated_body_from_start(self):
        for error in (RemoteDisconnected("connection closed"), IncompleteRead(b"partial"), None):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp, patch("time.sleep"):
                response = MagicMock()
                response.__enter__.return_value.read.side_effect = [b"d", error] if error else [b"d", b""]
                with patch("setup_ocr.urlopen", side_effect=[response] + [io.BytesIO(data) for data in self.data.values()]):
                    destination = Path(temp) / "ocr"
                    setup_ocr.install(destination)
                    self.assertEqual((destination / "det.onnx").read_bytes(), self.data["det.onnx"])

    def test_exhausted_retries_preserve_existing_models_and_report_file(self):
        with tempfile.TemporaryDirectory() as temp, patch("time.sleep"), patch(
            "setup_ocr.urlopen", side_effect=RemoteDisconnected("Remote end closed connection without response")
        ) as request:
            destination = Path(temp)
            (destination / "det.onnx").write_bytes(b"previous model")
            with self.assertRaises(QuizError) as caught:
                setup_ocr.install(destination)
            self.assertEqual(request.call_count, 3)
            self.assertIn("det.onnx", str(caught.exception))
            self.assertIn("https://api.github.com/repos/MaaXYZ/MaaCommonAssets/contents/", str(caught.exception))
            self.assertIn(setup_ocr.REVISION, str(caught.exception))
            self.assertIn("Remote end closed connection without response", str(caught.exception))
            self.assertEqual((destination / "det.onnx").read_bytes(), b"previous model")

    def test_bad_hash_never_replaces_existing_models(self):
        with tempfile.TemporaryDirectory() as temp, patch("time.sleep"), patch(
            "setup_ocr.urlopen", side_effect=self.response
        ):
            destination = Path(temp)
            for name in self.data:
                (destination / name).write_bytes(b"previous model")
            self.data["rec.onnx"] = b"x" * len(self.data["rec.onnx"])
            with self.assertRaisesRegex(QuizError, "integrity check"):
                setup_ocr.install(destination)
            for name in self.data:
                self.assertEqual((destination / name).read_bytes(), b"previous model")
