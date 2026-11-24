"""PDF text-layer/OCR regression tests; fixtures contain no private mail."""
import io
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject, EncodedStreamObject

import parentmail_watch as watcher


def scanned_pdf(label="Christmas dinner on 10 December", *, pages=1):
    images = []
    for index in range(pages):
        image = Image.new("RGB", (1600, 500), "white")
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 52)
        except OSError:
            font = ImageFont.load_default(size=52)
        draw = ImageDraw.Draw(image)
        draw.text((45, 70), label, fill="black", font=font)
        draw.text((45, 180), f"Page {index + 1} - Book by 27 November", fill="black", font=font)
        images.append(image)
    output = io.BytesIO()
    images[0].save(output, format="PDF", save_all=True, append_images=images[1:], resolution=150)
    return output.getvalue()


def native_pdf(*labels):
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    for label in labels:
        page = writer.add_blank_page(width=600, height=300)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 20 Tf 30 100 Td ({label}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def angled_poster_pdf():
    image = Image.new("RGB", (1000, 1400), "#c5d600")
    font = ImageFont.load_default(size=48)
    ImageDraw.Draw(image).text((60, 80), "FESTIVE SCHOOL LUNCH", fill="#e50050", font=font)
    badge = Image.new("RGBA", (500, 300), "#d9004c")
    draw = ImageDraw.Draw(badge)
    for y, label in ((40, "10TH DEC 26"), (110, "12:00pm"), (180, "In the Food hall")):
        draw.text((35, y), label, fill="white", font=font)
    badge = badge.rotate(20, expand=True, fillcolor=(0, 0, 0, 0))
    image.paste(badge, (350, 900), badge)
    output = io.BytesIO()
    image.save(output, format="PDF", resolution=150)
    return output.getvalue()


class PdfOcrTests(unittest.TestCase):
    def test_expired_page_budget_does_not_start_skew_ocr(self):
        def render(command, **kwargs):
            Image.new("RGB", (100, 100), "white").save(Path(command[-1]).with_suffix(".png"))
            return SimpleNamespace(returncode=0)

        with patch.object(watcher.time, "monotonic", side_effect=[0, 31]), patch.object(
                watcher.subprocess, "run", side_effect=render), patch.object(watcher, "ocr_docx_image") as ocr:
            recovered, complete = watcher.ocr_pdf_page(b"synthetic input", 1)
        ocr.assert_not_called()
        self.assertFalse(complete)
        self.assertEqual(recovered, "")

    def test_empty_failed_ocr_pass_is_not_hidden_by_skew_recovery(self):
        for ordinary_result, expected_method in (
                (subprocess.TimeoutExpired("tesseract", 15), "pypdf+ocr-incomplete"),
                ("Upright-text", "pypdf+ocr")):
            with self.subTest(ordinary_result=type(ordinary_result).__name__):
                outcomes = iter((ordinary_result, "Skew-recovery" if isinstance(ordinary_result, Exception) else "", ""))

                def run(command, **kwargs):
                    if command[0] != "tesseract":
                        Image.new("RGB", (100, 100), "white").save(Path(command[-1]).with_suffix(".png"))
                    else:
                        outcome = next(outcomes)
                        if isinstance(outcome, Exception):
                            raise outcome
                        tsv = "level\tleft\ttop\twidth\theight\ttext\n"
                        if outcome:
                            tsv += f"5\t0\t20\t80\t20\t{outcome}\n"
                        kwargs["stdout"].write(tsv.encode())
                    return SimpleNamespace(returncode=0)

                with patch.object(watcher.subprocess, "run", side_effect=run):
                    extracted, method = watcher.extract_attachment_text(scanned_pdf(), "menu.pdf", "application/pdf")
                self.assertEqual(method, expected_method)
                self.assertIn("Skew-recovery" if isinstance(ordinary_result, Exception) else "Upright-text", extracted)
                self.assertEqual("check original attachment" in extracted, expected_method.endswith("incomplete"))

    def test_aggregate_page_ocr_text_is_capped_at_10000_characters(self):
        def render(command, **kwargs):
            Image.new("RGB", (100, 100), "white").save(Path(command[-1]).with_suffix(".png"))
            return SimpleNamespace(returncode=0)

        passes = ["A" * 4_000, "B" * 4_000, "C" * 4_000]
        expected = "\n".join((passes[0], f"[Skew recovery -20 degrees]\n{passes[1]}",
                              f"[Skew recovery +20 degrees]\n{passes[2]}"))
        with patch.object(watcher.subprocess, "run", side_effect=render), patch.object(
                watcher, "ocr_docx_image", side_effect=[(part, True) for part in passes]) as ocr:
            recovered, complete = watcher.ocr_pdf_page(b"synthetic input", 1)
        self.assertEqual(ocr.call_count, 3)
        self.assertEqual(len(recovered), 10_000)
        self.assertEqual(recovered, expected[:10_000])
        self.assertFalse(complete)

    def test_successful_no_word_docx_image_ocr_remains_incomplete(self):
        image = io.BytesIO()
        Image.new("RGB", (100, 100), "white").save(image, format="PNG")

        def tesseract(*args, **kwargs):
            kwargs["stdout"].write(b"level\tleft\ttop\twidth\theight\ttext\n")
            return SimpleNamespace(returncode=0)

        with patch.object(watcher.subprocess, "run", side_effect=tesseract):
            self.assertEqual(watcher.ocr_docx_image(image.getvalue()), ("", False))

    def test_colored_poster_recovers_angled_date_and_time(self):
        extracted, method = watcher.extract_attachment_text(angled_poster_pdf(), "poster.pdf", "application/pdf")
        self.assertEqual(method, "pypdf+ocr")
        self.assertIn("10TH DEC 26", extracted)
        self.assertIn("12:00pm", extracted)

    def test_ocr_literal_quote_does_not_consume_following_tsv_rows(self):
        tsv = "level\tleft\ttop\twidth\theight\ttext\n"
        for left, word in ((0, '"'), (30, "10TH"), (80, "DEC"), (130, "26")):
            tsv += f"5\t{left}\t20\t40\t20\t{word}\n"
        image = io.BytesIO()
        Image.new("RGB", (300, 100), "white").save(image, format="PNG")

        def tesseract(*args, **kwargs):
            kwargs["stdout"].write(tsv.encode())
            return SimpleNamespace(returncode=0)

        with patch.object(watcher.subprocess, "run", side_effect=tesseract):
            extracted, complete = watcher.ocr_docx_image(image.getvalue())
        self.assertTrue(complete)
        self.assertEqual(extracted, '" 10TH DEC 26')

    def test_document_ocr_page_limit_marks_unread_pages_explicitly(self):
        with patch.object(watcher, "MAX_PDF_OCR_PAGES", 1), patch.object(
                watcher, "ocr_pdf_page", return_value=("Recognized first page", True)) as ocr:
            extracted, method = watcher.extract_attachment_text(scanned_pdf(pages=3), "menu.pdf", "application/pdf")
        self.assertEqual(ocr.call_count, 1)
        self.assertEqual(method, "pypdf+ocr-incomplete")
        self.assertIn("PDF page 2", extracted)
        self.assertIn("PDF page 3", extracted)
        self.assertIn("check original attachment", extracted)

    def test_expired_document_ocr_budget_does_not_start_another_page(self):
        with patch.object(watcher, "MAX_PDF_OCR_SECONDS", 1), patch.object(
                watcher.time, "monotonic", side_effect=[0, 2]), patch.object(watcher, "ocr_pdf_page") as ocr:
            extracted, method = watcher.extract_attachment_text(scanned_pdf(), "menu.pdf", "application/pdf")
        ocr.assert_not_called()
        self.assertEqual(method, "pypdf+ocr-incomplete")
        self.assertIn("check original attachment", extracted)

    def test_image_only_pdf_uses_ocr_instead_of_returning_empty_success(self):
        extracted, method = watcher.extract_attachment_text(scanned_pdf(), "menu.pdf", "application/pdf")
        self.assertEqual(method, "pypdf+ocr")
        self.assertIn("Christmas dinner on 10 December", extracted)
        self.assertIn("Book by 27 November", extracted)

    def test_native_text_pages_use_actual_newlines_without_ocr(self):
        with patch.object(watcher, "ocr_pdf_page") as ocr:
            extracted, method = watcher.extract_attachment_text(native_pdf("First page", "Second page"), "menu.pdf", "")
        ocr.assert_not_called()
        self.assertEqual(method, "pypdf")
        self.assertEqual(extracted, "First page\nSecond page")

    def test_mixed_document_preserves_native_and_scanned_page_order(self):
        writer = PdfWriter()
        for data in (native_pdf("Native first page"), scanned_pdf(), native_pdf("Native last page")):
            writer.add_page(PdfReader(io.BytesIO(data)).pages[0])
        output = io.BytesIO()
        writer.write(output)
        extracted, method = watcher.extract_attachment_text(output.getvalue(), "menu.pdf", "application/pdf")
        self.assertEqual(method, "pypdf+ocr")
        self.assertLess(extracted.index("Native first page"), extracted.index("Christmas dinner"))
        self.assertLess(extracted.index("Christmas dinner"), extracted.index("Native last page"))

    def test_missing_renderer_marks_ocr_incomplete(self):
        with patch.object(watcher.shutil, "which", return_value=None):
            extracted, method = watcher.extract_attachment_text(scanned_pdf(), "menu.pdf", "application/pdf")
        self.assertEqual(method, "pypdf+ocr-incomplete")
        self.assertIn("check original attachment", extracted)

    def test_missing_tesseract_marks_ocr_incomplete(self):
        original_which = shutil.which
        with patch.object(watcher.shutil, "which", side_effect=lambda name: None if name == "tesseract" else original_which(name)):
            extracted, method = watcher.extract_attachment_text(scanned_pdf(), "menu.pdf", "application/pdf")
        self.assertEqual(method, "pypdf+ocr-incomplete")
        self.assertIn("check original attachment", extracted)

    def test_renderer_timeout_is_bounded_and_explicit(self):
        with patch.object(watcher.subprocess, "run", side_effect=subprocess.TimeoutExpired("pdftoppm", 15)) as run:
            extracted, method = watcher.extract_attachment_text(scanned_pdf(), "menu.pdf", "application/pdf")
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertEqual(method, "pypdf+ocr-incomplete")
        self.assertIn("check original attachment", extracted)

    def test_unsupported_page_filter_preserves_surrounding_native_text(self):
        writer = PdfWriter()
        for page in PdfReader(io.BytesIO(native_pdf("Native first page", "Unreadable page", "Native last page"))).pages:
            writer.add_page(page)
        stream = EncodedStreamObject()
        stream._data = b"unsupported encoded content"
        stream[NameObject("/Filter")] = NameObject("/UnsupportedFilter")
        writer.pages[1][NameObject("/Contents")] = stream
        output = io.BytesIO()
        writer.write(output)

        with patch.object(watcher, "ocr_pdf_page", return_value=("", False)):
            extracted, method = watcher.extract_attachment_text(output.getvalue(), "menu.pdf", "application/pdf")
        self.assertEqual(method, "pypdf+ocr-incomplete")
        self.assertIn("PDF page 2", extracted)
        self.assertIn("check original attachment", extracted)
        self.assertLess(extracted.index("Native first page"), extracted.index("PDF page 2"))
        self.assertLess(extracted.index("PDF page 2"), extracted.index("Native last page"))

    def test_failed_ocr_page_does_not_erase_other_pages(self):
        writer = PdfWriter()
        for data in (native_pdf("Keep native text"), scanned_pdf()):
            writer.add_page(PdfReader(io.BytesIO(data)).pages[0])
        output = io.BytesIO()
        writer.write(output)
        with patch.object(watcher, "ocr_pdf_page", return_value=("", False)):
            extracted, method = watcher.extract_attachment_text(output.getvalue(), "menu.pdf", "application/pdf")
        self.assertEqual(method, "pypdf+ocr-incomplete")
        self.assertIn("Keep native text", extracted)
        self.assertIn("PDF page 2", extracted)

    def test_partial_ocr_text_is_retained_but_flagged_incomplete(self):
        with patch.object(watcher, "ocr_pdf_page", return_value=("Partial result", False)):
            extracted, method = watcher.extract_attachment_text(scanned_pdf(), "menu.pdf", "application/pdf")
        self.assertEqual(method, "pypdf+ocr-incomplete")
        self.assertIn("Partial result", extracted)
        self.assertIn("check original attachment", extracted)


class PdfPersistenceTests(unittest.TestCase):
    def test_refresh_persists_pdf_ocr_without_renotifying_existing_attachment(self):
        responses = [{"data": [{"id": "synthetic-message", "subject": "School menu",
                                "last_message": {"content": "See attachment"}}]}]
        attachment = {"message_id": "synthetic-message", "filename": "menu.pdf",
                      "url": "https://parents.parentmail.co.uk/download/synthetic",
                      "mime_type": "application/pdf", "bytes": scanned_pdf()}
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            with patch.object(watcher, "DB", root / "messages.sqlite3"), patch.object(watcher, "ATTACHMENTS", root / "attachments"):
                with patch.object(watcher, "extract_attachment_text", return_value=("", "pypdf")):
                    self.assertEqual(watcher.persist(responses, [attachment]), "SILENT")
                self.assertEqual(watcher.persist(responses, [attachment]), "SILENT")
                with closing(sqlite3.connect(watcher.DB)) as connection:
                    rows = connection.execute("SELECT a.extraction_method,a.extracted_text,t.extracted_text FROM attachments a JOIN attachment_text t USING(attachment_id)").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "pypdf+ocr")
        self.assertIn("Book by 27 November", rows[0][1])
        self.assertEqual(rows[0][1], rows[0][2])

    def test_new_attachment_payload_contains_ocr_after_commit(self):
        old = {"id": "synthetic-old", "subject": "Old message"}
        new = {"id": "synthetic-new", "subject": "New menu"}
        attachment = {"message_id": new["id"], "filename": "menu.pdf",
                      "url": "https://parents.parentmail.co.uk/download/synthetic",
                      "mime_type": "application/pdf", "bytes": scanned_pdf()}
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            with patch.object(watcher, "DB", root / "messages.sqlite3"), patch.object(watcher, "ATTACHMENTS", root / "attachments"):
                self.assertEqual(watcher.persist([{"data": [old]}]), "SILENT")
                payload = watcher.persist([{"data": [old, new]}], [attachment])
                self.assertIn("ATTACHMENT\nFilename: menu.pdf", payload)
                self.assertIn("Book by 27 November", payload)
                self.assertEqual(watcher.persist([{"data": [old, new]}], [attachment]), "SILENT")


if __name__ == "__main__":
    unittest.main()
