"""Install pinned MaaCommonAssets PP-OCRv5 models; never download executable code."""
from __future__ import annotations

import argparse
import hashlib
import tempfile
from pathlib import Path
from urllib.request import urlopen

from quiz_core import QuizError

REVISION = "dabcd4681ac990dc4361de26416d986abd80e4aa"
BASE_URL = f"https://raw.githubusercontent.com/MaaXYZ/MaaCommonAssets/{REVISION}/OCR/ppocr_v5/zh_cn"
# Git blob hashes and exact byte sizes observed from the upstream Git tree.
FILES = {
    "det.onnx": (4748769, "695bfec5f957f6b38aa70950e22df594398c6cbe"),
    "rec.onnx": (16517247, "edcbe5fdec6035db85d84498003fdabda6a92e19"),
    "keys.txt": (74012, "567898b49de2707853454682f05e0c504c0085b9"),
    "README.md": (1086, "a0d0460cf7203f531f644d5ce819c190d723f359"),
}
DEFAULT_DEST = Path(__file__).resolve().parent / "assets/resource/model/ocr"


def verify_file(path: Path, spec: tuple[int, str]):
    size, expected = spec
    if not path.is_file() or path.stat().st_size != size:
        raise QuizError(f"Missing/wrong-size OCR asset: {path.name}; run setup_ocr.py.")
    hasher = hashlib.sha1(b"blob " + str(size).encode("ascii") + b"\0")
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    if hasher.hexdigest() != expected:
        raise QuizError(f"OCR asset failed integrity check: {path.name}.")


def verify_models(destination: Path = DEFAULT_DEST):
    for name in ("det.onnx", "rec.onnx", "keys.txt"):
        verify_file(destination / name, FILES[name])


def install(destination: Path = DEFAULT_DEST):
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ocr-download-", dir=destination.parent) as stage:
        stage = Path(stage)
        for name, (size, sha) in FILES.items():
            target = stage / name
            with urlopen(f"{BASE_URL}/{name}", timeout=60) as response, target.open("wb") as stream:
                received = 0
                while chunk := response.read(1024 * 1024):
                    received += len(chunk)
                    if received > size:
                        raise QuizError(f"Unexpected download size: {name}")
                    stream.write(chunk)
            verify_file(target, (size, sha))
        # Validate every download before replacing any existing model.
        for name in FILES:
            (stage / name).replace(destination / name)
    verify_models(destination)


def main():
    parser = argparse.ArgumentParser(description="Download pinned OCR assets, or verify local assets without network access")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    try:
        if args.verify:
            verify_models(args.dest)
        else:
            install(args.dest)
        print(f"OCR assets verified: {args.dest}")
        return 0
    except (QuizError, OSError) as exc:
        print(f"OCR setup failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
