"""Standalone image attachment regressions using synthetic fixtures only."""
import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

import parentmail_watch as watcher


def notice_image(format_name, label):
    # PNG starts transparent to exercise the shared white-background OCR path.
    mode, background = ("RGBA", (0, 0, 0, 0)) if format_name == "PNG" else ("RGB", "white")
    with Image.new(mode, (900, 240), background) as image:
        ImageDraw.Draw(image).text((35, 60), label, fill="black", font=ImageFont.load_default(size=52))
        output = io.BytesIO()
        image.save(output, format=format_name)
        return output.getvalue()


class ImageAttachmentTests(unittest.TestCase):
    def test_tesseract_child_does_not_inherit_parentmail_credentials(self):
        data = notice_image("PNG", "SCHOOL LUNCH")
        credentials = {
            "PARENTMAIL_EMAIL": "fixture@example.invalid",
            "PARENTMAIL_PASSWORD": "synthetic-password",
        }
        runtime = {
            "PATH": "/synthetic/bin",
            "TESSDATA_PREFIX": "/synthetic/tessdata",
            "LC_ALL": "C.UTF-8",
            "OMP_THREAD_LIMIT": "2",
        }
        if tmpdir := os.environ.get("TMPDIR"):
            runtime["TMPDIR"] = tmpdir
        with (
            patch.dict(os.environ, {**credentials, **runtime}, clear=True),
            patch.object(watcher.shutil, "which", return_value="tesseract"),
            patch.object(watcher.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            watcher.ocr_docx_image(data)
            run.assert_called_once()
            child_env = run.call_args.kwargs.get("env")
            if child_env is None:
                self.fail("Tesseract must get an explicit environment")
            for name, value in credentials.items():
                self.assertFalse(name in child_env, f"{name} was passed to Tesseract")
                self.assertEqual(os.environ.get(name), value)
            for name, value in runtime.items():
                self.assertEqual(child_env.get(name), value)

    def test_mislabeled_gif_does_not_reach_tesseract(self):
        data = notice_image("GIF", "SCHOOL LUNCH")
        with (
            patch.object(watcher.shutil, "which", return_value="tesseract"),
            patch.object(watcher.subprocess, "run") as run,
        ):
            result = watcher.extract_attachment_text(data, "photo.jpg", "image/jpeg")
        self.assertEqual(result, ("", "image-ocr-incomplete"))
        self.assertFalse(run.called, "Tesseract ran on non-JPEG/PNG content")

    def test_standalone_apng_is_incomplete_without_ocr(self):
        output = io.BytesIO()
        with (
            Image.new("RGB", (32, 32), "white") as first,
            Image.new("RGB", (32, 32), "black") as second,
        ):
            first.save(output, format="PNG", save_all=True, append_images=[second])
        data = output.getvalue()
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(getattr(image, "n_frames", 1), 2)

        def fake_tesseract(args, *, stdout, **kwargs):
            stdout.write(b"left\ttop\twidth\theight\ttext\n0\t0\t20\t12\tFIRST FRAME\n")
            return watcher.subprocess.CompletedProcess(args, 0)

        with (
            patch.object(watcher.shutil, "which", return_value="tesseract"),
            patch.object(Image.Image, "convert", autospec=True, side_effect=Image.Image.convert) as convert,
            patch.object(watcher.subprocess, "run", side_effect=fake_tesseract) as run,
        ):
            result = watcher.extract_attachment_text(data, "notice.png", "image/png")
        self.assertEqual(result, ("", "image-ocr-incomplete"))
        convert.assert_not_called()
        run.assert_not_called()

    def test_oversize_jpeg_is_not_decoded(self):
        data = notice_image("JPEG", "SCHOOL LUNCH") + bytes(8_000_000)
        with (
            patch.object(watcher.shutil, "which", return_value="tesseract"),
            patch.object(Image, "open", wraps=Image.open) as decode,
        ):
            result = watcher.extract_attachment_text(data, "photo.JPEG", "application/octet-stream")
        self.assertEqual(result, ("", "image-ocr-incomplete"))
        decode.assert_not_called()

    def test_unreadable_png_stays_unextracted(self):
        self.assertEqual(
            watcher.extract_attachment_text(b"broken PNG", "notice.png", "image/png"),
            ("", "image-ocr-incomplete"),
        )

    def test_partial_ocr_retains_text_with_an_explicit_warning(self):
        data = notice_image("JPEG", "SCHOOL LUNCH")
        with patch.object(watcher, "ocr_docx_image", return_value=("SCHOOL LUNCH", False)):
            extracted, method = watcher.extract_attachment_text(data, "notice.jpg", "image/jpeg")
        self.assertEqual(method, "image-ocr-incomplete")
        self.assertIn("SCHOOL LUNCH", extracted)
        self.assertIn("check original attachment", extracted)

    @unittest.skipUnless(shutil.which("tesseract"), "Tesseract required for image OCR")
    def test_new_jpeg_and_png_text_is_persisted_and_notified_once(self):
        old = {"id": "synthetic-old", "subject": "Baseline notice"}
        new = {"id": "synthetic-new", "subject": "New notice"}
        cases = (
            ("JPEG", "notice.bin", "image/jpeg", "SCHOOL LUNCH"),
            ("PNG", "schedule.PNG", "application/octet-stream", "LIBRARY CLOSED"),
        )
        attachments = [
            {"message_id": new["id"], "filename": filename, "mime_type": mime,
             "url": f"https://parents.parentmail.co.uk/download/synthetic-{kind.lower()}",
             "bytes": notice_image(kind, label)}
            for kind, filename, mime, label in cases
        ]
        responses = [{"data": [old, new]}]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            with (
                patch.object(watcher, "DB", root / "messages.sqlite3"),
                patch.object(watcher, "ATTACHMENTS", root / "attachments"),
            ):
                self.assertEqual(watcher.persist([{"data": [old]}]), "SILENT")
                payload = watcher.persist(responses, attachments)
                with closing(sqlite3.connect(watcher.DB)) as connection:
                    rows = connection.execute(
                        "SELECT a.filename,a.local_path,a.extracted_text,a.extraction_method,"
                        "t.extracted_text,t.extraction_method FROM attachments a "
                        "JOIN attachment_text t USING(attachment_id)"
                    ).fetchall()
                self.assertEqual(len(rows), len(cases))
                stored = {row[0]: row[1:] for row in rows}
                for (_, filename, _, label), attachment in zip(cases, attachments):
                    with self.subTest(filename=filename):
                        path, extracted, method, indexed_text, indexed_method = stored[filename]
                        self.assertEqual(Path(path).read_bytes(), attachment["bytes"])
                        self.assertIn(label, extracted)
                        self.assertEqual(method, "image-ocr")
                        self.assertEqual((indexed_text, indexed_method), (extracted, method))
                        self.assertIn(f"ATTACHMENT\nFilename: {filename}", payload)
                        self.assertIn(label, payload)
                self.assertEqual(watcher.persist(responses, attachments), "SILENT")
