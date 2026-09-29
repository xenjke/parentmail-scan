"""Regression tests for attachment text and embedded schedule images."""

import io
import os
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw, ImageFont

from parentmail_watch import extract_attachment_text, ocr_docx_image


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def embedded_schedule_docx(vml=False, target="media/image1.png", target_mode="", alternate=False):
    """Build an OOXML document with a table screenshot, not a w:tbl."""
    image = Image.new("RGBA", (1100, 180), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 46)
    except OSError:
        font = ImageFont.load_default(size=46)
    draw.text((24, 20), "Thursday Year 2", fill="black", font=font)
    draw.text((24, 90), "22.10.26  Beaver Class", fill="black", font=font)
    raw_image = io.BytesIO()
    image.save(raw_image, format="PNG")
    image_element = ('<w:p><w:r><w:pict><v:shape><v:imagedata r:id="rId7"/>'
                     '</v:shape></w:pict></w:r></w:p>' if vml else
                     '<w:p><w:r><w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p>')
    if alternate:
        image_element = ("<w:p><w:r><mc:AlternateContent>"
                         "<mc:Choice Requires=\"a\"><w:drawing><a:blip r:embed=\"rId7\"/></w:drawing></mc:Choice>"
                         "<mc:Fallback><w:pict><v:shape><v:imagedata r:id=\"rId7\"/></v:shape></w:pict></mc:Fallback>"
                         "</mc:AlternateContent></w:r></w:p>")
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
      xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
      xmlns:v="urn:schemas-microsoft-com:vml"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <w:body>
        <w:p><w:r><w:t>Term 1</w:t></w:r></w:p>
        {image_element}
        <w:p><w:r><w:t>Term 2</w:t></w:r></w:p>
      </w:body>
    </w:document>"""
    relationships = f"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId7" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="{target}" {target_mode}/>
    </Relationships>"""
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", relationships)
        archive.writestr("word/media/image1.png", raw_image.getvalue())
    return docx.getvalue()


class AttachmentExtractionTests(unittest.TestCase):
    def test_embedded_transparent_schedule_is_read_in_document_order(self):
        extracted, method = extract_attachment_text(
            embedded_schedule_docx(), "Forest School.docx", DOCX_MIME
        )
        self.assertEqual(method, "docx-ooxml+ocr")
        self.assertIn("Thursday Year 2", extracted)
        self.assertIn("22.10.26", extracted)
        self.assertIn("Beaver Class", extracted)
        self.assertLess(extracted.index("Term 1"), extracted.index("22.10.26"))
        self.assertLess(extracted.index("22.10.26"), extracted.index("Term 2"))

    def test_vml_screenshot_schedule_is_read_in_document_order(self):
        extracted, method = extract_attachment_text(
            embedded_schedule_docx(vml=True), "Forest School.docx", DOCX_MIME
        )
        self.assertEqual(method, "docx-ooxml+ocr")
        self.assertIn("22.10.26", extracted)
        self.assertLess(extracted.index("Term 1"), extracted.index("22.10.26"))
        self.assertLess(extracted.index("22.10.26"), extracted.index("Term 2"))

    def test_alternatecontent_choice_and_fallback_ocr_only_once(self):
        with mock.patch("parentmail_watch.ocr_docx_image", wraps=ocr_docx_image) as ocr:
            extracted, method = extract_attachment_text(
                embedded_schedule_docx(alternate=True), "letter.docx", DOCX_MIME
            )
        self.assertEqual(method, "docx-ooxml+ocr")
        self.assertEqual(ocr.call_count, 1)
        self.assertEqual(extracted.count("22.10.26"), 1)

    def test_alternatecontent_distinct_ids_still_selects_one_branch(self):
        original = io.BytesIO(embedded_schedule_docx(alternate=True))
        rebuilt = io.BytesIO()
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(rebuilt, "w") as target:
            for member in source.namelist():
                payload = source.read(member)
                if member == "word/document.xml":
                    payload = payload.replace(b'<v:imagedata r:id="rId7"/>', b'<v:imagedata r:id="rId8"/>')
                elif member == "word/_rels/document.xml.rels":
                    extra = (b'<Relationship Id="rId8" Type="http://schemas.openxmlformats.org/'
                             b'officeDocument/2006/relationships/image" Target="media/image1.png"/>')
                    payload = payload.replace(b"</Relationships>", extra + b"</Relationships>")
                target.writestr(member, payload)
        with mock.patch("parentmail_watch.ocr_docx_image", wraps=ocr_docx_image) as ocr:
            extracted, method = extract_attachment_text(rebuilt.getvalue(), "letter.docx", DOCX_MIME)
        self.assertEqual(method, "docx-ooxml+ocr")
        self.assertEqual(ocr.call_count, 1)
        self.assertEqual(extracted.count("22.10.26"), 1)

    def test_repeated_image_in_one_paragraph_is_not_silently_dropped(self):
        original = io.BytesIO(embedded_schedule_docx())
        rebuilt = io.BytesIO()
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(rebuilt, "w") as target:
            for member in source.namelist():
                payload = source.read(member)
                if member == "word/document.xml":
                    single = b'<w:drawing><a:blip r:embed="rId7"/></w:drawing>'
                    payload = payload.replace(single, single + single)
                target.writestr(member, payload)
        with mock.patch("parentmail_watch.ocr_docx_image", wraps=ocr_docx_image) as ocr:
            extracted, method = extract_attachment_text(rebuilt.getvalue(), "letter.docx", DOCX_MIME)
        self.assertEqual(method, "docx-ooxml+ocr")
        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(extracted.count("22.10.26"), 2)

    def test_nested_textbox_paragraph_is_extracted_only_once(self):
        original = io.BytesIO(embedded_schedule_docx())
        rebuilt = io.BytesIO()
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(rebuilt, "w") as target:
            for member in source.namelist():
                payload = source.read(member)
                if member == "word/document.xml":
                    old = b'<w:p><w:r><w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p>'
                    new = (b'<w:p><w:r><w:txbxContent><w:p><w:r><w:t>Inner schedule</w:t>'
                           b'<w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p>'
                           b'</w:txbxContent></w:r></w:p>')
                    payload = payload.replace(old, new)
                target.writestr(member, payload)
        with mock.patch("parentmail_watch.ocr_docx_image", wraps=ocr_docx_image) as ocr:
            extracted, method = extract_attachment_text(rebuilt.getvalue(), "letter.docx", DOCX_MIME)
        self.assertEqual(method, "docx-ooxml+ocr")
        self.assertEqual(extracted.count("Inner schedule"), 1)
        self.assertEqual(extracted.count("22.10.26"), 1)
        self.assertEqual(ocr.call_count, 1)

    def test_text_only_alternate_choice_without_fallback_is_retained(self):
        original = io.BytesIO(embedded_schedule_docx())
        rebuilt = io.BytesIO()
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(rebuilt, "w") as target:
            for member in source.namelist():
                payload = source.read(member)
                if member == "word/document.xml":
                    old = b'<w:p><w:r><w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p>'
                    new = (b'<w:p><mc:AlternateContent><mc:Choice Requires="w">'
                           b'<w:r><w:t>Schedule date 22.10.26</w:t></w:r>'
                           b'</mc:Choice></mc:AlternateContent></w:p>')
                    payload = payload.replace(old, new)
                target.writestr(member, payload)
        extracted, method = extract_attachment_text(rebuilt.getvalue(), "letter.docx", DOCX_MIME)
        self.assertEqual(method, "docx-ooxml")
        self.assertIn("Schedule date 22.10.26", extracted)

    def test_explicit_internal_and_absolute_local_image_targets(self):
        for target, mode in (("media/image1.png", 'TargetMode="Internal"'),
                             ("/word/media/image1.png", "")):
            with self.subTest(target=target, mode=mode):
                extracted, method = extract_attachment_text(
                    embedded_schedule_docx(target=target, target_mode=mode),
                    "letter.docx", DOCX_MIME
                )
                self.assertEqual(method, "docx-ooxml+ocr")
                self.assertIn("22.10.26", extracted)

    def test_missing_embedded_image_does_not_erase_document_text(self):
        original = io.BytesIO(embedded_schedule_docx())
        broken = io.BytesIO()
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(broken, "w") as target:
            for member in source.namelist():
                if member != "word/media/image1.png":
                    target.writestr(member, source.read(member))
        extracted, method = extract_attachment_text(broken.getvalue(), "letter.docx", DOCX_MIME)
        self.assertEqual(method, "docx-ooxml+ocr-incomplete")
        self.assertIn("Term 1", extracted)
        self.assertIn("Term 2", extracted)
        self.assertIn("check original attachment", extracted)

    def test_missing_relationships_does_not_erase_document_text(self):
        original = io.BytesIO(embedded_schedule_docx())
        broken = io.BytesIO()
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(broken, "w") as target:
            for member in source.namelist():
                if member != "word/_rels/document.xml.rels":
                    target.writestr(member, source.read(member))
        extracted, method = extract_attachment_text(broken.getvalue(), "letter.docx", DOCX_MIME)
        self.assertEqual(method, "docx-ooxml+ocr-incomplete")
        self.assertIn("Term 1", extracted)
        self.assertIn("Term 2", extracted)
        self.assertIn("check original attachment", extracted)

    def test_corrupt_compressed_relationships_do_not_erase_text(self):
        import zlib

        original_read = zipfile.ZipFile.read

        def read_with_bad_relationships(archive, member, *args, **kwargs):
            if getattr(member, "filename", member) == "word/_rels/document.xml.rels":
                raise zlib.error("corrupt deflate stream")
            return original_read(archive, member, *args, **kwargs)

        with mock.patch("parentmail_watch.zipfile.ZipFile.read", read_with_bad_relationships):
            extracted, method = extract_attachment_text(
                embedded_schedule_docx(), "letter.docx", DOCX_MIME
            )
        self.assertEqual(method, "docx-ooxml+ocr-incomplete")
        self.assertIn("Term 1", extracted)
        self.assertIn("Term 2", extracted)

    def test_oversized_relationships_are_not_parsed(self):
        original = io.BytesIO(embedded_schedule_docx())
        oversized = io.BytesIO()
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(oversized, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for member in source.namelist():
                payload = source.read(member)
                if member == "word/_rels/document.xml.rels":
                    payload = payload.replace(b"</Relationships>", b"<!--" + b"x" * 1_100_000 + b"--></Relationships>")
                target.writestr(member, payload)
        extracted, method = extract_attachment_text(oversized.getvalue(), "letter.docx", DOCX_MIME)
        self.assertEqual(method, "docx-ooxml+ocr-incomplete")
        self.assertIn("Term 1", extracted)
        self.assertNotIn("22.10.26", extracted)

    def test_corrupt_zip_image_member_keeps_other_document_text(self):
        original_read = zipfile.ZipFile.read

        def read_with_bad_image(archive, member, *args, **kwargs):
            if getattr(member, "filename", member) == "word/media/image1.png":
                raise zipfile.BadZipFile("bad image CRC")
            return original_read(archive, member, *args, **kwargs)

        with mock.patch("parentmail_watch.zipfile.ZipFile.read", read_with_bad_image):
            extracted, method = extract_attachment_text(
                embedded_schedule_docx(), "letter.docx", DOCX_MIME
            )
        self.assertEqual(method, "docx-ooxml+ocr-incomplete")
        self.assertIn("Term 1", extracted)
        self.assertIn("Term 2", extracted)
        self.assertIn("check original attachment", extracted)

    def test_large_ocr_output_is_spooled_not_captured_in_memory(self):
        stream = []

        def fake_tesseract(*args, **kwargs):
            output = kwargs.get("stdout")
            stream.append(output)
            if output is not None:
                output.write(b"x" * 2_000_001)
            return mock.Mock(returncode=0, stdout=b"")

        with mock.patch("parentmail_watch.subprocess.run", side_effect=fake_tesseract):
            extracted, method = extract_attachment_text(
                embedded_schedule_docx(), "letter.docx", DOCX_MIME
            )
        self.assertIsNotNone(stream[0], "Tesseract stdout must be disk-backed, not capture_output")
        self.assertEqual(method, "docx-ooxml+ocr-incomplete")
        self.assertIn("OCR unavailable or unreadable", extracted)

    def test_truncated_ocr_is_flagged_in_method_and_payload(self):
        tsv = ("level\tleft\ttop\twidth\theight\ttext\n"
               "5\t0\t0\t100\t40\t" + "A" * 10_100 + "\n")
        def fake_tesseract(*args, **kwargs):
            if "stdout" in kwargs:
                kwargs["stdout"].write(tsv.encode("utf-8"))
            return mock.Mock(returncode=0, stdout=tsv.encode("utf-8"))

        with mock.patch("parentmail_watch.subprocess.run", side_effect=fake_tesseract):
            extracted, method = extract_attachment_text(
                embedded_schedule_docx(), "letter.docx", DOCX_MIME
            )
        self.assertEqual(method, "docx-ooxml+ocr-incomplete")
        self.assertIn("OCR text truncated", extracted)
        self.assertIn("Term 2", extracted)

    def test_corrupt_image_decoder_keeps_text_and_flags_incomplete_ocr(self):
        with mock.patch("PIL.Image.open", side_effect=Image.DecompressionBombError("oversized image")):
            extracted, method = extract_attachment_text(
                embedded_schedule_docx(), "letter.docx", DOCX_MIME
            )
        self.assertEqual(method, "docx-ooxml+ocr-incomplete")
        self.assertIn("Term 1", extracted)
        self.assertIn("Term 2", extracted)
        self.assertIn("check original attachment", extracted)

    def test_image_ocr_dependency_missing_is_explicitly_reported(self):
        with mock.patch("parentmail_watch.shutil.which", return_value=None):
            extracted, method = extract_attachment_text(
                embedded_schedule_docx(), "letter.docx", DOCX_MIME
            )
        self.assertEqual(method, "docx-ooxml+ocr-incomplete")
        self.assertIn("Term 1", extracted)
        self.assertIn("check original attachment", extracted)
        self.assertNotIn("22.10.26", extracted)

    def test_normal_word_table_still_extracts_cell_text(self):
        xml = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>Term 1</w:t></w:r></w:p>
          <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Beaver Class</w:t></w:r></w:p></w:tc>
          <w:tc><w:p><w:r><w:t>22.10.26</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
          </w:body></w:document>'''
        file = io.BytesIO()
        with zipfile.ZipFile(file, "w") as archive:
            archive.writestr("word/document.xml", xml)
        extracted, method = extract_attachment_text(file.getvalue(), "letter.docx", DOCX_MIME)
        self.assertEqual(method, "docx-ooxml")
        self.assertLess(extracted.index("Term 1"), extracted.index("Beaver Class"))
        self.assertLess(extracted.index("Beaver Class"), extracted.index("22.10.26"))

    def test_other_attachment_formats_unchanged(self):
        from pypdf import PdfWriter

        output = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(output)
        self.assertEqual(extract_attachment_text(output.getvalue(), "page.pdf", "application/pdf"), ("", "pypdf"))
        self.assertEqual(extract_attachment_text(b"data", "page.png", "image/png"), ("", "unsupported_format"))

    @unittest.skipUnless(os.environ.get("PARENTMAIL_TEST_DOCX"), "private DOCX fixture not provided")
    def test_private_forest_school_table_has_all_week_labels(self):
        source = Path(os.environ["PARENTMAIL_TEST_DOCX"])
        extracted, method = extract_attachment_text(source.read_bytes(), source.name, DOCX_MIME)
        self.assertEqual(method, "docx-ooxml+ocr")
        self.assertIn("Bumblebee Class | Cygnet Class | Polar Bear", extracted.split("Term 2")[0])
        for term in ("Week 8", "22.10.26", "19.11.26", "10.12.26", "Beaver Class"):
            with self.subTest(term=term):
                self.assertIn(term, extracted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
