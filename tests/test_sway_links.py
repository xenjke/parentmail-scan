import tempfile
import sqlite3
import unittest
import os
from pathlib import Path

from playwright.sync_api import sync_playwright
import parentmail_watch
from parentmail_watch import (
    canonical_sway_url,
    collect_sway_links,
    extract_bounded_sway_text,
    extract_sway_urls,
    fetch_sway_content,
    is_allowed_sway_resource,
    is_allowed_sway_request,
    launch_sway_browser,
)


class FakeResponse:
    status = 200


class FakeLocator:
    def __init__(self, text):
        self._text = text

    def evaluate(self, expression, argument, timeout=None):
        limit = min(argument["max_chars"], argument["raw_budget"])
        return {"text": self._text[:limit], "truncated": len(self._text) > limit or len(self._text) > argument["max_chars"]}

    def inner_text(self, timeout=None):
        return self._text


class FakePage:
    def __init__(self, title, body, final_url=None):
        self._title = title
        self._body = body
        self._final_url = final_url
        self.routes = []
        self.visited = None
        self.closed = False
        self.events = []

    def on(self, event, callback):
        self.events.append(event)

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def goto(self, url, **kwargs):
        self.visited = url
        return FakeResponse()

    def wait_for_function(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, ms):
        return None

    def title(self):
        return self._title

    def locator(self, selector):
        if selector == "body":
            return FakeLocator(self._body)
        if selector == "title":
            return FakeLocator(self._title)
        raise AssertionError(f"unexpected selector {selector}")

    @property
    def url(self):
        return self._final_url or self.visited

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.route_calls = []
        self.routes = []

    def route(self, pattern, handler):
        self.route_calls.append((pattern, handler))
        self.routes.append((pattern, handler))

    def unroute(self, pattern, handler):
        self.routes = [entry for entry in self.routes if entry != (pattern, handler)]

    def new_page(self):
        return self.page


class SwayLinkTests(unittest.TestCase):
    def test_extracts_only_root_public_sway_links_and_canonicalizes_tracking_query(self):
        body = (
            "Read https://sway.cloud.microsoft/AbCdEf1234567890?ref=Link, "
            "<a href=\"https://sway.cloud.microsoft/QwErTy0987654321?ref=LinkWishing&amp;x=1\">"
            "newsletter</a> https://sway.cloud.microsoft.evil.test/not-sway"
        )
        self.assertEqual(
            extract_sway_urls(body),
            [
                "https://sway.cloud.microsoft/AbCdEf1234567890",
                "https://sway.cloud.microsoft/QwErTy0987654321",
            ],
        )

    def test_resource_allowlist_rejects_non_sway_hosts_schemes_and_redirect_ports(self):
        self.assertTrue(is_allowed_sway_resource("https://sway.cloud.microsoft/sway/v1.0/id/worlds"))
        self.assertTrue(is_allowed_sway_resource("https://eus-cdn.sway.static.microsoft/s/id/images/pic"))
        self.assertFalse(is_allowed_sway_resource("http://sway.cloud.microsoft/id"))
        self.assertFalse(is_allowed_sway_resource("https://sway.cloud.microsoft.evil.test/id"))
        self.assertFalse(is_allowed_sway_resource("https://example.com/id"))
        self.assertFalse(is_allowed_sway_resource("https://sway.cloud.microsoft:8443/id"))

    def test_route_policy_only_allows_original_document_and_textual_sway_resources(self):
        original = "https://sway.cloud.microsoft/AbCdEf1234567890"
        self.assertTrue(is_allowed_sway_request(original, "document", original))
        self.assertFalse(is_allowed_sway_request("https://sway.cloud.microsoft/OtherSway123456", "document", original))
        self.assertFalse(is_allowed_sway_request("https://example.com/image.png", "image", original))
        self.assertFalse(is_allowed_sway_request("https://eus-cdn.sway.static.microsoft/image.png", "image", original))
        self.assertTrue(is_allowed_sway_request("https://sway.cloud.microsoft/sway/v1.0/id/worlds", "xhr", original))
        self.assertFalse(is_allowed_sway_request("https://example.com/content", "xhr", original))

    def test_bounded_sway_extraction_stops_before_copying_the_full_dom_text(self):
        payload = "Accessibility View " + ("newsletter paragraph " * 20_000)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH") or None,
            )
            page = browser.new_page()
            page.set_content(f"<body><p>{payload}</p></body>")
            extracted, truncated = extract_bounded_sway_text(page, limit=2_000)
            browser.close()
        self.assertLessEqual(len(extracted), 2_000)
        self.assertTrue(truncated)
        self.assertNotIn("newsletter paragraph " * 100, extracted)

    def test_title_dom_text_is_bounded_before_python_receives_it(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH") or None,
            )
            page = browser.new_page()
            page.set_content("<html><head><title>" + ("T" * 100_000) + "</title></head><body>short</body></html>")
            title, truncated = extract_bounded_sway_text(page, limit=200, selector="title")
            browser.close()
        self.assertEqual(len(title), 200)
        self.assertTrue(truncated)

    def test_bounded_sway_walk_stops_on_many_whitespace_text_nodes(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH") or None,
            )
            page = browser.new_page()
            page.set_content("<body><p>Accessibility View " + ("<span> </span>" * 25_000) + "AFTER_LIMIT</p></body>")
            extracted, truncated = extract_bounded_sway_text(page, limit=2_000)
            browser.close()
        self.assertLessEqual(len(extracted), 2_000)
        self.assertTrue(truncated)
        self.assertNotIn("AFTER_LIMIT", extracted)

    def test_reads_rendered_accessibility_text_from_public_sway_page(self):
        body = "Accessibility View\nThe Anchor\nIssue 2\n" + ("School newsletter content. " * 30)
        page = FakePage("The Anchor", body)
        context = FakeContext(page)
        title, extracted, method = fetch_sway_content(
            context, "https://sway.cloud.microsoft/AbCdEf1234567890?ref=Link"
        )
        self.assertEqual(title, "The Anchor")
        expected = "\n".join(line.strip() for line in body.splitlines())
        self.assertEqual(extracted, expected)
        self.assertEqual(method, "sway-playwright")
        self.assertEqual(page.visited, "https://sway.cloud.microsoft/AbCdEf1234567890")
        self.assertTrue(page.closed)
        self.assertIn("popup", page.events)
        self.assertEqual(len(context.route_calls), 1)
        self.assertEqual(context.routes, [])

    def test_sway_page_title_is_bounded(self):
        page = FakePage("T" * 2_000, "Accessibility View " + ("newsletter text " * 30))
        title, extracted, method = fetch_sway_content(
            FakeContext(page), "https://sway.cloud.microsoft/AbCdEf1234567890"
        )
        self.assertLessEqual(len(title), 200)
        self.assertEqual(method, "sway-playwright")
        self.assertTrue(extracted)

    def test_rejects_redirect_to_a_different_sway_document(self):
        page = FakePage(
            "Other document",
            "Accessibility View\\nOther document " + ("content " * 30),
            final_url="https://sway.cloud.microsoft/OtherSwayDocument123",
        )
        title, extracted, method = fetch_sway_content(
            FakeContext(page), "https://sway.cloud.microsoft/AbCdEf1234567890"
        )
        self.assertEqual(title, "")
        self.assertEqual(extracted, "")
        self.assertEqual(method, "sway_redirected")

    def test_truncates_oversized_rendered_content_and_marks_it_incomplete(self):
        body = "Accessibility View\nThe Anchor\nIssue 2\n" + ("newsletter text " * 4000)
        page = FakePage("The Anchor", body)
        title, extracted, method = fetch_sway_content(
            FakeContext(page), "https://sway.cloud.microsoft/AbCdEf1234567890"
        )
        self.assertEqual(title, "The Anchor")
        self.assertLessEqual(len(extracted), 40_000)
        self.assertGreater(len(extracted), 10_000)
        self.assertEqual(method, "sway-playwright-truncated")


class SwayPipelineTests(unittest.TestCase):
    def test_sway_browser_context_blocks_service_workers(self):
        class FakeContext:
            pass

        class FakeBrowser:
            def __init__(self):
                self.options = None

            def new_context(self, **options):
                self.options = options
                return FakeContext()

        class FakeChromium:
            def __init__(self):
                self.browser = FakeBrowser()
                self.options = None

            def launch(self, **options):
                self.options = options
                return self.browser

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()

        fake = FakePlaywright()
        browser, context = launch_sway_browser(fake, headless=True, executable_path="/chromium")
        self.assertIs(browser, fake.chromium.browser)
        self.assertIsInstance(context, FakeContext)
        self.assertEqual(fake.chromium.options["executable_path"], "/chromium")
        self.assertEqual(fake.chromium.browser.options, {"service_workers": "block"})

    def test_init_db_adds_link_cache_to_existing_parentmail_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "messages.sqlite3"
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE messages (fingerprint TEXT PRIMARY KEY, subject TEXT NOT NULL,
                    sender TEXT, message_date TEXT, body_hash TEXT, first_seen_at TEXT,
                    last_seen_at TEXT, body_text TEXT, raw_text TEXT, server_message_id TEXT,
                    school_id TEXT, published_at TEXT, content_hash TEXT, notified_at TEXT);
                CREATE TABLE attachments (attachment_id TEXT PRIMARY KEY, message_fingerprint TEXT,
                    filename TEXT, local_path TEXT, content_hash TEXT, extracted_text TEXT,
                    first_seen_at TEXT, last_seen_at TEXT, server_attachment_id TEXT,
                    message_id TEXT, mime_type TEXT, extraction_method TEXT);
                CREATE TABLE attachment_text (attachment_id TEXT PRIMARY KEY,
                    extracted_text TEXT, extraction_method TEXT);
                INSERT INTO messages(fingerprint,subject) VALUES ('existing','Old record');
            """)
            parentmail_watch.init_db(conn)
            tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
            existing = conn.execute("select subject from messages where fingerprint='existing'").fetchone()
            conn.close()
            self.assertIn("message_links", tables)
            self.assertEqual(existing, ("Old record",))

    def test_collects_page_text_from_message_links_and_skips_cached_links(self):
        url = "https://sway.cloud.microsoft/AbCdEf1234567890"
        message = {
            "id": "message-1",
            "title": "This week's newsletter",
            "last_message": {"id": "message-1", "content": f"Read the issue: {url}?ref=Link"},
        }
        page = FakePage("The Anchor", "Accessibility View\nThe Anchor\nSing2Save details " + ("newsletter " * 30))
        response = {"data": [message]}
        links = collect_sway_links([response], FakeContext(page))
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["message_id"], "message-1")
        self.assertEqual(links[0]["url"], url)
        self.assertIn("Sing2Save details", links[0]["extracted_text"])
        cached = {("message-1", url)}
        self.assertEqual(collect_sway_links([response], FakeContext(page), cached_links=cached), [])
        self.assertEqual(len(collect_sway_links([response], FakeContext(page), cached_links=cached, force=True)), 1)

    def test_link_content_is_persisted_and_attached_to_new_message_summary_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db, old_attachments = parentmail_watch.DB, parentmail_watch.ATTACHMENTS
            parentmail_watch.DB = Path(tmp) / "messages.sqlite3"
            parentmail_watch.ATTACHMENTS = Path(tmp) / "attachments"
            try:
                conn = sqlite3.connect(parentmail_watch.DB)
                parentmail_watch.init_db(conn)
                conn.execute(
                    "insert into messages(fingerprint,subject,body_hash,first_seen_at,last_seen_at) values(?,?,?,?,?)",
                    ("baseline", "Baseline", "baseline-hash", "now", "now"),
                )
                conn.commit()
                conn.close()
                (Path(tmp) / ".deterministic-worker-baselined").write_text("baseline\\n")
                response = {"data": [{
                    "id": "message-1",
                    "title": "This week's newsletter",
                    "last_message": {"id": "message-1", "content": "Read our Sway newsletter."},
                }]}
                link = {
                    "message_id": "message-1",
                    "url": "https://sway.cloud.microsoft/AbCdEf1234567890",
                    "title": "The Anchor",
                    "extracted_text": "Issue 2. Sing2Save performance on 9 October.",
                    "extraction_method": "sway-playwright",
                }
                output = parentmail_watch.persist([response], linked_content=[link])
                self.assertIn("LINKED SWAY PAGE", output)
                self.assertIn("Sing2Save performance", output)
                conn = sqlite3.connect(parentmail_watch.DB)
                saved = conn.execute("select title,extracted_text,extraction_method from message_links where message_id=?", ("message-1",)).fetchone()
                conn.close()
                self.assertEqual(saved, ("The Anchor", link["extracted_text"], "sway-playwright"))
                self.assertEqual(parentmail_watch.persist([response], None, True), "SILENT")

                link["extracted_text"] = "Updated newsletter text."
                self.assertEqual(parentmail_watch.persist([response], linked_content=[link]), "SILENT")
                link["extracted_text"] = "Dry-run text must not persist."
                self.assertEqual(parentmail_watch.persist([response], linked_content=[link], dry_run=True), "SILENT")
                conn = sqlite3.connect(parentmail_watch.DB)
                saved_text = conn.execute("select extracted_text from message_links where message_id=?", ("message-1",)).fetchone()[0]
                conn.close()
                self.assertEqual(saved_text, "Updated newsletter text.")
            finally:
                parentmail_watch.DB, parentmail_watch.ATTACHMENTS = old_db, old_attachments


if __name__ == "__main__":
    unittest.main(verbosity=2)
