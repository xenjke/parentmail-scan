"""Synthetic browser/SQLite regression: no ParentMail login or live state."""
import copy
from contextlib import closing, nullcontext
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

import parentmail_watch as watcher


SWAY_URL = "https://sway.cloud.microsoft/AnchorIssue42026"


class NewsletterDiscoveryTests(unittest.TestCase):
    def _run_synthetic_collector(self, responses, *, refresh_links=False, refresh_attachments=False,
                                 fetch_result=("Newsletter", "Concert on Friday", "sway-playwright")):
        """Run the real collector with a local DOM and fake list transport, never login."""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH") or None)
            try:
                context = browser.new_context()
                page = context.new_page()
                visited = []

                def serve_detail(route):
                    visited.append(route.request.url)
                    route.fulfill(content_type="text/html", body=(
                        f'<main><p><a href="{SWAY_URL}?ref=Link">Click here for the newsletter</a></p></main>'))

                context.route("**/*", serve_detail)
                fake_playwright = SimpleNamespace(chromium=SimpleNamespace(
                    launch_persistent_context=lambda *args, **kwargs: context))
                fake_sway_browser = SimpleNamespace(close=lambda: None)
                fake_sway_context = SimpleNamespace(close=lambda: None)
                with patch.object(watcher, "sync_playwright", return_value=nullcontext(fake_playwright)), \
                        patch.multiple(watcher, EMAIL=True, PASSWORD=True,
                                       PROFILE=watcher.DB.parent / "test-profile",
                                       LOGIN_URL="https://parents.parentmail.co.uk/test-dashboard"), \
                        patch.object(page, "wait_for_timeout"), \
                        patch.object(page, "evaluate", side_effect=[[], {"responses": responses}]), \
                        patch.object(watcher, "launch_sway_browser",
                                     return_value=(fake_sway_browser, fake_sway_context)) as launch_sway, \
                        patch.object(watcher, "fetch_sway_content", return_value=fetch_result) as fetch:
                    result = watcher._login_and_collect(refresh_attachments, refresh_links)
                    return result, visited, fetch.call_args_list, launch_sway.call_count
            finally:
                browser.close()

    def test_refresh_links_revisits_known_detail_and_updates_cache_without_duplicate_notification(self):
        responses = [{"data": [{"id": "known-newsletter", "title": "Newsletter",
                                "last_message": {"content": "Click here for the newsletter"}}]}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(watcher, "DB", Path(tmp) / "messages.sqlite3"):
            self.assertEqual(watcher.persist(responses), "SILENT")
            with closing(sqlite3.connect(watcher.DB)) as db:
                original = db.execute("select body_text,content_hash,first_seen_at,notified_at from messages").fetchone()
            result, visited, calls, launches = self._run_synthetic_collector(responses, refresh_links=True)
            self.assertIn("https://parents.parentmail.co.uk/messages/known-newsletter", visited)
            self.assertEqual(launches, 1)
            self.assertEqual([call.args[1] for call in calls], [SWAY_URL])
            self.assertEqual(watcher.persist(result[0], result[1], linked_content=result[2]), "SILENT")
            with closing(sqlite3.connect(watcher.DB)) as db:
                self.assertEqual(db.execute("select message_id,source_url,extracted_text from message_links").fetchall(),
                                 [("known-newsletter", SWAY_URL, "Concert on Friday")])
                self.assertEqual(db.execute("select body_text,content_hash,first_seen_at,notified_at from messages").fetchone(), original)
            result, visited, calls, launches = self._run_synthetic_collector(responses)
            self.assertNotIn("https://parents.parentmail.co.uk/messages/known-newsletter", visited)
            self.assertEqual(calls, [])
            self.assertEqual(launches, 0)
            self.assertEqual(watcher.persist(result[0], result[1], linked_content=result[2]), "SILENT")

    def test_combined_candidates_share_one_message_budget_and_ignore_unknown_message_ids(self):
        urls = [f"https://sway.cloud.microsoft/ExampleIssue{i:08d}" for i in range(7)]
        responses = [{"data": [{"id": "one", "last_message": {"content": f"{urls[0]} {urls[1]}"}},
                                {"id": "two", "last_message": {"content": "Click here"}}]}]
        responses.append({"data": [{"id": "one", "last_message": {"content": f"{urls[1]} {urls[2]} {urls[3]}"}}]})
        details = [("one", urls[0] + "?ref=Link"), ("one", urls[4]), ("two", urls[5]),
                   ("two", urls[5] + "#tracking"), ("not-in-list", urls[6])]
        expected = [("one", url) for url in urls[:3]] + [("two", urls[5])]
        self.assertEqual(watcher.sway_link_candidates(responses, details), expected)
        with patch.object(watcher, "fetch_sway_content", return_value=("Issue", "text", "sway-playwright")) as fetch:
            links = watcher.collect_sway_links(responses, None, detail_candidates=details)
        self.assertEqual(fetch.call_count, watcher.MAX_SWAY_FETCHES_PER_RUN)
        self.assertEqual([(link["message_id"], link["url"]) for link in links], expected)
        self.assertEqual(links[-1]["extraction_method"], watcher.SWAY_NOT_ATTEMPTED)

    def test_detail_links_are_associated_only_with_the_exact_parentmail_detail_page(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH") or None)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.fulfill(
                    content_type="text/html", body=f'<p><a href="{SWAY_URL}">newsletter</a></p>'))
                for url in ("https://parents.parentmail.co.uk/messages/other",
                            "https://parents.parentmail.co.uk/messages",
                            "https://parents.parentmail.co.uk.evil.test/messages/expected",
                            "http://parents.parentmail.co.uk/messages/expected"):
                    with self.subTest(url=url):
                        page.goto(url)
                        self.assertEqual(watcher.collect_detail_sway_links(page, "expected"), [])
                page.goto("https://parents.parentmail.co.uk/messages/expected")
                self.assertEqual(watcher.collect_detail_sway_links(page, "expected"), [("expected", SWAY_URL)])
                page.goto("https://parents.parentmail.co.uk/messages/encoded%2Fid")
                self.assertEqual(watcher.collect_detail_sway_links(page, "encoded/id"), [("encoded/id", SWAY_URL)])
            finally:
                browser.close()

    def test_detail_anchor_policy_and_dom_budgets_are_bounded_before_returning_to_python(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH") or None)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<body></body>"))
                page.goto("https://parents.parentmail.co.uk/messages/expected")
                invalid = ["https://example.test/newsletter", "javascript:alert(1)",
                           "http://sway.cloud.microsoft/AnchorIssue42026",
                           "//sway.cloud.microsoft/AnchorIssue42026",
                           "https://sway.cloud.microsoft.evil.test/AnchorIssue42026",
                           "https://user:pass@sway.cloud.microsoft/AnchorIssue42026",
                           "https://sway.cloud.microsoft:8443/AnchorIssue42026",
                           SWAY_URL + "/nested", SWAY_URL + "?x=" + "x" * watcher.MAX_DETAIL_HREF_CHARS]
                anchors = "".join(f'<a href="{url}">unsupported</a>' for url in invalid)
                page.set_content(f'<main>{anchors}<p><a href="{SWAY_URL}?ref=Link&amp;x=1">newsletter</a></p>'
                                 f'<a href="{SWAY_URL}#tracking">duplicate</a></main>')
                self.assertEqual(watcher.collect_detail_sway_links(page, "expected"), [("expected", SWAY_URL)])
                page.set_content('<main>' + '<a href="https://example.test/">no</a>' * watcher.MAX_DETAIL_ANCHORS
                                 + f'<a href="{SWAY_URL}">beyond anchor budget</a></main>')
                self.assertEqual(watcher.collect_detail_sway_links(page, "expected"), [])
                page.set_content('<main>' + '<span> </span>' * watcher.MAX_DETAIL_DOM_NODES
                                 + f'<a href="{SWAY_URL}">beyond node budget</a></main>')
                self.assertEqual(watcher.collect_detail_sway_links(page, "expected"), [])
                urls = [f"https://sway.cloud.microsoft/ExampleIssue{i:08d}" for i in range(5)]
                page.set_content('<main>' + ''.join(f'<a href="{url}">issue</a>' for url in urls) + '</main>')
                self.assertEqual(watcher.collect_detail_sway_links(page, "expected"),
                                 [("expected", url) for url in urls[:watcher.MAX_SWAY_LINKS_PER_MESSAGE]])
            finally:
                browser.close()

    def test_detail_discovery_prefers_message_body_and_ignores_navigation(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH") or None)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<body></body>"))
                page.goto("https://parents.parentmail.co.uk/messages/expected")
                other = "https://sway.cloud.microsoft/UnrelatedDocument"
                for body in (f'<a href="{other}">outside main</a><main><nav><a href="{other}">menu</a></nav>'
                             f'<p><a href="{SWAY_URL}">newsletter</a></p></main>',
                             f'<main><a href="{other}">other</a><article data-testid="message-body">'
                             f'<p><a href="{SWAY_URL}">newsletter</a></p></article></main>'):
                    page.set_content(body)
                    self.assertEqual(watcher.collect_detail_sway_links(page, "expected"), [("expected", SWAY_URL)])
            finally:
                browser.close()

    def test_normal_collection_enriches_unknown_label_only_message_once(self):
        responses = [{"data": [{"message_id": "new/encoded-id", "title": "Newsletter",
                                "last_message": {"content": "Click here for the newsletter"}}]}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(watcher, "DB", Path(tmp) / "messages.sqlite3"):
            watcher.persist([{"data": [{"id": "baseline"}]}])
            result, visited, calls, launches = self._run_synthetic_collector(responses + responses)
            self.assertEqual(visited.count("https://parents.parentmail.co.uk/messages/new%2Fencoded-id"), 1)
            self.assertEqual([call.args[1] for call in calls], [SWAY_URL])
            self.assertEqual(launches, 1)
            output = watcher.persist(result[0], result[1], linked_content=result[2])
            self.assertIn("Concert on Friday", output)
            self.assertEqual(watcher.persist(result[0], result[1], linked_content=result[2]), "SILENT")

    def test_budget_limited_detail_links_remain_pending_for_next_silent_run(self):
        responses = [{"data": [{"id": f"newsletter-{i}", "last_message": {"content": "Click here"}}
                                for i in range(watcher.MAX_SWAY_FETCHES_PER_RUN + 1)]}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(watcher, "DB", Path(tmp) / "messages.sqlite3"):
            watcher.persist([{"data": [{"id": "baseline"}]}])
            result, visited, calls, launches = self._run_synthetic_collector(responses)
            self.assertEqual(len(calls), watcher.MAX_SWAY_FETCHES_PER_RUN)
            output = watcher.persist(result[0], result[1], linked_content=result[2])
            self.assertIn(watcher.SWAY_NOT_ATTEMPTED, output)
            result, visited, calls, launches = self._run_synthetic_collector(responses)
            self.assertEqual(len(calls), 1, "pending detail URL must survive becoming a known message")
            self.assertEqual(watcher.persist(result[0], result[1], linked_content=result[2]), "SILENT")
            with closing(sqlite3.connect(watcher.DB)) as db:
                methods = [row[0] for row in db.execute("select extraction_method from message_links")]
            self.assertEqual(methods, ["sway-playwright"] * (watcher.MAX_SWAY_FETCHES_PER_RUN + 1))
            result, visited, calls, launches = self._run_synthetic_collector(responses)
            self.assertEqual(calls, [])
            self.assertEqual(launches, 0)
            self.assertEqual(watcher.persist(result[0], result[1], linked_content=result[2]), "SILENT")

    def test_failed_detail_only_link_retries_after_cooldown_without_reopening_message(self):
        responses = [{"data": [{"id": "failed-newsletter", "last_message": {"content": "Click here"}}]}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(watcher, "DB", Path(tmp) / "messages.sqlite3"):
            watcher.persist([{"data": [{"id": "baseline"}]}])
            result, visited, calls, launches = self._run_synthetic_collector(
                responses, fetch_result=("", "", "sway_timeout"))
            self.assertIn("sway_timeout", watcher.persist(result[0], result[1], linked_content=result[2]))
            result, visited, calls, launches = self._run_synthetic_collector(responses)
            self.assertEqual(calls, [])
            with closing(sqlite3.connect(watcher.DB)) as db:
                db.execute("update message_links set last_seen_at=?", ("2000-01-01T00:00:00+00:00",))
                db.commit()
            result, visited, calls, launches = self._run_synthetic_collector(responses)
            self.assertEqual([call.args[1] for call in calls], [SWAY_URL])
            self.assertNotIn("https://parents.parentmail.co.uk/messages/failed-newsletter", visited)
            self.assertEqual(watcher.persist(result[0], result[1], linked_content=result[2]), "SILENT")
            with closing(sqlite3.connect(watcher.DB)) as db:
                self.assertEqual(db.execute("select extracted_text,extraction_method from message_links").fetchone(),
                                 ("Concert on Friday", "sway-playwright"))

    def test_pending_replacement_is_not_starved_by_historical_successful_links(self):
        history = [f"https://sway.cloud.microsoft/HistoricalIssue{i:08d}" for i in range(3)]
        replacement = "https://sway.cloud.microsoft/ReplacementIssue2026"
        for body in ("Click here", " ".join(history)):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp, patch.object(
                    watcher, "DB", Path(tmp) / "messages.sqlite3"):
                responses = [{"data": [{"id": "known-newsletter", "last_message": {"content": body}}]}]
                good = [{"message_id": "known-newsletter", "url": url, "title": "Old issue",
                         "extracted_text": "Historical text", "extraction_method": "sway-playwright"}
                        for url in history]
                watcher.persist(responses, linked_content=good)
                pending = {"message_id": "known-newsletter", "url": replacement, "title": "",
                           "extracted_text": "", "extraction_method": watcher.SWAY_NOT_ATTEMPTED}
                self.assertEqual(watcher.persist(responses, linked_content=[pending]), "SILENT")
                result, visited, calls, launches = self._run_synthetic_collector(responses)
                self.assertEqual([call.args[1] for call in calls], [replacement])
                self.assertEqual(watcher.persist(result[0], result[1], linked_content=result[2]), "SILENT")
                result, visited, calls, launches = self._run_synthetic_collector(responses)
                self.assertEqual(calls, [])
                self.assertEqual(launches, 0)

    def test_pending_placeholder_cannot_erase_successful_link_cache(self):
        responses = [{"data": [{"id": "known-newsletter", "last_message": {"content": "Click here"}}]}]
        good = {"message_id": "known-newsletter", "url": SWAY_URL, "title": "The Anchor",
                "extracted_text": "Concert on Friday", "extraction_method": "sway-playwright"}
        with tempfile.TemporaryDirectory() as tmp, patch.object(watcher, "DB", Path(tmp) / "messages.sqlite3"):
            watcher.persist(responses, linked_content=[good])
            pending = dict(good, title="", extracted_text="", extraction_method=watcher.SWAY_NOT_ATTEMPTED)
            self.assertEqual(watcher.persist(responses, linked_content=[pending]), "SILENT")
            with closing(sqlite3.connect(watcher.DB)) as db:
                self.assertEqual(db.execute("select title,extracted_text,extraction_method from message_links").fetchone(),
                                 ("The Anchor", "Concert on Friday", "sway-playwright"))

    def test_refresh_over_budget_retains_uncached_detail_urls_for_known_messages(self):
        responses = [{"data": [{"id": f"known-{i}", "last_message": {"content": "Click here"}}
                                for i in range(watcher.MAX_SWAY_FETCHES_PER_RUN + 1)]}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(watcher, "DB", Path(tmp) / "messages.sqlite3"):
            self.assertEqual(watcher.persist(responses), "SILENT")
            result, visited, calls, launches = self._run_synthetic_collector(responses, refresh_links=True)
            self.assertEqual(len(calls), watcher.MAX_SWAY_FETCHES_PER_RUN)
            self.assertEqual(watcher.persist(result[0], result[1], linked_content=result[2]), "SILENT")
            result, visited, calls, launches = self._run_synthetic_collector(responses)
            self.assertEqual(len(calls), 1, "known message's excess detail URL must not be lost")
            self.assertEqual(watcher.persist(result[0], result[1], linked_content=result[2]), "SILENT")
            with closing(sqlite3.connect(watcher.DB)) as db:
                self.assertEqual(db.execute("select count(*) from message_links where extraction_method=?",
                                            ("sway-playwright",)).fetchone()[0],
                                 watcher.MAX_SWAY_FETCHES_PER_RUN + 1)

    def test_label_only_list_item_detail_anchor_reaches_persisted_new_message_summary(self):
        item = {
            "id": "newsletter-4",
            "title": "The Anchor - Issue 4 2026",
            "last_message": {"content": "Click here for The Anchor - Issue 4 2026"},
        }
        responses = [{"data": [item]}]
        original = copy.deepcopy(responses)
        canonical_message = watcher.extract_message(item, None)
        self.assertIsNotNone(canonical_message)
        assert canonical_message is not None
        with tempfile.TemporaryDirectory() as tmp, patch.object(watcher, "DB", Path(tmp) / "messages.sqlite3"):
            self.assertEqual(watcher.persist([{"data": [{"id": "baseline", "title": "Baseline"}]}]), "SILENT")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True, executable_path=os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH") or None)
                detail_context = browser.new_context()
                detail_context.route("**/*", lambda route: route.fulfill(
                    content_type="text/html",
                    body=f'<main><article data-testid="message-body"><a href="{SWAY_URL}?ref=Link">'
                         'Click here for The Anchor - Issue 4 2026</a></article></main>'))
                detail_page = detail_context.new_page()
                detail_page.goto("https://parents.parentmail.co.uk/messages/newsletter-4")
                discover = getattr(watcher, "collect_detail_sway_links", lambda page, mid: [])
                candidates = discover(detail_page, item["id"])
                self.assertEqual(candidates, [(item["id"], SWAY_URL)])

                sway_context = browser.new_context(service_workers="block")
                newsletter = "Accessibility View The Anchor. Concert on Friday. " + "School news. " * 30
                sway_context.route("**/*", lambda route: route.fulfill(
                    content_type="text/html", body=f"<title>The Anchor</title><body>{newsletter}</body>"))
                sway_page = sway_context.new_page()
                sway_page.goto(SWAY_URL)
                # Stub only navigation; run the production Sway reader against real DOM text.
                with patch.object(sway_context, "new_page", return_value=sway_page), patch.object(
                        sway_page, "goto", return_value=SimpleNamespace(status=200)):
                    links = watcher.collect_sway_links(responses, sway_context, detail_candidates=candidates)
                output = watcher.persist(responses, linked_content=links)
                self.assertIn("LINKED SWAY PAGE", output)
                self.assertIn("Concert on Friday", output)
                self.assertEqual(len(links), 1)
                with closing(sqlite3.connect(watcher.DB)) as db:
                    self.assertEqual(db.execute(
                        "select message_id,source_url,extracted_text,extraction_method from message_links"
                    ).fetchone(), (item["id"], SWAY_URL, newsletter.strip(), "sway-playwright"))
                    self.assertEqual(db.execute(
                        "select body_text,content_hash from messages where server_message_id=?", (item["id"],)
                    ).fetchone(), (canonical_message["body"], canonical_message["hash"]))
                self.assertEqual(responses, original)
                browser.close()


if __name__ == "__main__":
    unittest.main()
