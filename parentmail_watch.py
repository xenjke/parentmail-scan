#!/usr/bin/env python3
"""Deterministic, read-only IRIS ParentMail watcher.

Authentication and API calls stay inside a persistent Playwright browser context.
Only committed SQLite changes are eligible for output.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, hashlib, html, io, json, os, posixpath, re, shutil, sqlite3, subprocess, sys, tempfile, time, zipfile, zlib
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse, urlsplit
from typing import Any
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

LOGIN_URL = "https://parents.parentmail.co.uk/auth/login"
PORTAL_URL = "https://parents.parentmail.co.uk/messages"


def default_data_dir() -> Path:
    return Path(__file__).resolve().parent / ".local" / "parentmail"


DATA_DIR = Path(os.environ.get("PARENTMAIL_DATA_DIR", str(default_data_dir())))
DB = Path(os.environ.get("PARENTMAIL_DB_PATH", str(DATA_DIR / "messages.sqlite3")))
PROFILE = Path(os.environ.get("PARENTMAIL_PROFILE_DIR", str(DATA_DIR / "browser-profile-v2")))
ATTACHMENTS = Path(os.environ.get("PARENTMAIL_ATTACHMENTS_DIR", str(DATA_DIR / "attachments")))
EMAIL = os.environ.get("PARENTMAIL_EMAIL")
PASSWORD = os.environ.get("PARENTMAIL_PASSWORD")
DEBUG = os.environ.get("PARENTMAIL_DEBUG") == "1"
CURRENT_PHASE = "startup"


def debug(message: str):
    if DEBUG:
        print("DEBUG", message, flush=True)


def phase(name: str):
    global CURRENT_PHASE
    CURRENT_PHASE = name
    debug(f"phase={name}")


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def text(v: Any) -> str:
    if v is None: return ""
    return re.sub(r"\s+", " ", html.unescape(str(v))).strip()


SWAY_LINK_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
SWAY_ID_RE = re.compile(r"^/[A-Za-z0-9_-]{8,64}/?$")
SWAY_RESOURCE_HOSTS = {
    "sway.cloud.microsoft",
    "eus-cdn.sway.static.microsoft",
    "cdn.sway.static.microsoft",
    "wcpstatic.microsoft.com",
}
MAX_SWAY_LINKS_PER_MESSAGE = 3
MAX_SWAY_FETCHES_PER_RUN = 3
MAX_SWAY_TEXT_CHARS = 40_000
MAX_SWAY_TITLE_CHARS = 200
MAX_SWAY_RAW_CHARS = 120_000
MAX_SWAY_DOM_NODES = 20_000
SWAY_SUCCESS_METHODS = ("sway-playwright", "sway-playwright-truncated")
SWAY_NOT_ATTEMPTED = "sway_not_attempted_run_limit"
SWAY_FAILURE_RETRY_AFTER = dt.timedelta(hours=6)
SWAY_TEXT_EXTRACTOR = r"""(root,opts)=>{
const limit=opts.max_chars,rawLimit=opts.raw_budget,maxNodes=opts.max_nodes;
const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
const blocks="p,li,h1,h2,h3,h4,h5,h6,td,th,blockquote,section,article,div";
const chunks=[];let length=0,rawLength=0,visited=0,previous=null,truncated=false,node;
while((node=walker.nextNode())){
 if(visited>=maxNodes||length>=limit||rawLength>=rawLimit){truncated=true;break;}
 visited++;
 const parent=node.parentElement;
 if(!parent||parent.closest("script,style,noscript,template,svg,[aria-hidden='true']"))continue;
 const source=node.nodeValue||"";if(!source)continue;
 const block=parent.closest(blocks)||parent;
 const separator=previous&&block!==previous?String.fromCharCode(10):"";
 const room=Math.min(limit-length-separator.length,rawLimit-rawLength);if(room<=0){truncated=true;break;}
 const raw=source.slice(0,room);rawLength+=raw.length;
 let part=raw.trim().split(/\s+/).filter(Boolean).join(" ");
 if(!part){if(raw.length<source.length){truncated=true;break;}continue;}
 if(previous===block&&length&&part&&chunks.length&&!chunks[chunks.length-1].endsWith(" "))part=" "+part;
 chunks.push(separator+part);length+=separator.length+part.length;previous=block;
 if(raw.length<source.length){truncated=true;break;}
}
if(!truncated&&length>=limit)truncated=true;
return {text:chunks.join(""),truncated};
}"""


def extract_bounded_sway_text(page, limit=MAX_SWAY_TEXT_CHARS, timeout_ms=5_000, selector="body") -> tuple[str, bool]:
    """Copy bounded text nodes without materializing a document-wide innerText string."""
    snapshot = page.locator(selector).evaluate(
        SWAY_TEXT_EXTRACTOR,
        {
            "max_chars": limit + 1,
            "raw_budget": max(limit + 1, min(MAX_SWAY_RAW_CHARS, limit * 3)),
            "max_nodes": MAX_SWAY_DOM_NODES,
        },
        timeout=timeout_ms,
    )
    rendered = str(snapshot.get("text") or "")
    truncated = bool(snapshot.get("truncated")) or len(rendered) > limit
    return rendered[:limit], truncated


def canonical_sway_url(raw_url: str) -> str | None:
    candidate = html.unescape(raw_url or "").strip().rstrip(".,;:!?)]}")
    try:
        parsed = urlsplit(candidate)
        if (parsed.scheme.lower() != "https" or parsed.hostname != "sway.cloud.microsoft"
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not SWAY_ID_RE.fullmatch(parsed.path)):
            return None
    except ValueError:
        return None
    return f"https://sway.cloud.microsoft{parsed.path.rstrip('/')}"


def extract_sway_urls(value: str, limit: int | None = MAX_SWAY_LINKS_PER_MESSAGE) -> list[str]:
    """Find public root-level Sway links, dropping tracking query parameters."""
    urls = []
    for match in SWAY_LINK_RE.finditer(html.unescape(value or "")):
        canonical = canonical_sway_url(match.group(0))
        if canonical and canonical not in urls:
            urls.append(canonical)
        if limit is not None and len(urls) >= limit:
            break
    return urls


def is_allowed_sway_resource(url: str) -> bool:
    """Allow only HTTPS requests to the Sway app and its static asset hosts."""
    try:
        parsed = urlsplit(url)
        return (parsed.scheme.lower() == "https" and parsed.hostname in SWAY_RESOURCE_HOSTS
                and not parsed.username and not parsed.password and parsed.port in (None, 443))
    except ValueError:
        return False


def is_allowed_sway_request(url: str, resource_type: str, document_url: str) -> bool:
    """Restrict navigation to this Sway and skip non-textual third-party content."""
    if resource_type in {"image", "media", "font"}:
        return False
    if resource_type == "document":
        return canonical_sway_url(url) == document_url
    return is_allowed_sway_resource(url)


def launch_sway_browser(playwright, headless: bool, executable_path: str | None = None):
    """Start an isolated, ephemeral browser context with service workers disabled."""
    browser = playwright.chromium.launch(headless=headless, executable_path=executable_path or None)
    try:
        context = browser.new_context(service_workers="block")
    except Exception:
        browser.close()
        raise
    return browser, context


def fetch_sway_content(context, url: str) -> tuple[str, str, str]:
    """Render a public Sway page and return its accessible title/text/method."""
    canonical = canonical_sway_url(url)
    if not canonical:
        return "", "", "sway_invalid_url"
    page = None
    route_handler = None
    method = "sway_unavailable"
    try:
        page = context.new_page()

        def route_sway_resources(route):
            if is_allowed_sway_request(route.request.url, route.request.resource_type, canonical):
                route.continue_()
            else:
                route.abort()

        route_handler = route_sway_resources
        context.route("**/*", route_handler)
        page.on("popup", lambda popup: popup.close())
        response = page.goto(canonical, wait_until="domcontentloaded", timeout=15_000)
        if response is None or response.status != 200:
            return "", "", method
        if canonical_sway_url(page.url) != canonical:
            return "", "", "sway_redirected"
        deadline = time.monotonic() + 15
        rendered = ""
        raw_truncated = False
        previous = None
        stable_reads = 0
        while time.monotonic() < deadline:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            rendered, raw_truncated = extract_bounded_sway_text(
                page,
                MAX_SWAY_TEXT_CHARS,
                timeout_ms=remaining_ms,
            )
            current = rendered.strip()
            if len(current) > 200 and "Accessibility View" in current and current == previous:
                stable_reads += 1
                if stable_reads >= 2:
                    break
            else:
                stable_reads = 0
            previous = current
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            page.wait_for_timeout(min(500, remaining_ms))
        title, _ = extract_bounded_sway_text(
            page,
            MAX_SWAY_TITLE_CHARS,
            timeout_ms=2_000,
            selector="title",
        )
        extracted = html.unescape(rendered or "").replace("\r\n", "\n").replace("\r", "\n")
        extracted = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in extracted.splitlines())
        extracted = re.sub(r"\n{3,}", "\n\n", extracted).strip()
        if not extracted or "Accessibility View" not in extracted:
            return title, "", "sway_unavailable"
        if raw_truncated or len(extracted) > MAX_SWAY_TEXT_CHARS:
            return title, extracted[:MAX_SWAY_TEXT_CHARS], "sway-playwright-truncated"
        return title, extracted, "sway-playwright"
    except PlaywrightTimeoutError:
        return "", "", "sway_timeout"
    except Exception:
        return "", "", method
    finally:
        if route_handler is not None:
            try:
                context.unroute("**/*", route_handler)
            except Exception:
                pass
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def sway_link_candidates(responses: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Return unique (message ID, canonical Sway URL) pairs from message bodies."""
    candidates = []
    seen = set()
    for response in responses:
        for item in response.get("data", []):
            if not isinstance(item, dict):
                continue
            message = extract_message(item, None)
            if not message:
                continue
            for url in extract_sway_urls(message["body"]):
                key = (message["id"], url)
                if key not in seen:
                    seen.add(key)
                    candidates.append(key)
    return candidates


def collect_sway_links(responses: list[dict[str, Any]], context, cached_links=None, force=False,
                       known_message_ids=None, recently_failed=None) -> list[dict[str, str]]:
    """Read uncached public Sway links found in ParentMail message bodies.

    Links on messages not yet persisted are fetched first so stale failures on
    older messages cannot starve them of the per-run budget. Links on older
    messages that failed recently are skipped until the retry window passes.
    New-message links that do not fit the budget are returned as not attempted
    so the summary can say the page was not read.
    """
    cached = set(cached_links or ())
    failed = set(recently_failed or ())
    known = set(known_message_ids or ())
    candidates = [key for key in sway_link_candidates(responses) if force or key not in cached]
    candidates = [key for key in candidates if force or key[0] not in known or key not in failed]
    candidates.sort(key=lambda key: key[0] in known)
    found = []
    fetched = 0
    for message_id, url in candidates:
        if fetched >= MAX_SWAY_FETCHES_PER_RUN:
            if message_id in known:
                break
            found.append({"message_id": message_id, "url": url, "title": "",
                          "extracted_text": "", "extraction_method": SWAY_NOT_ATTEMPTED})
            continue
        fetched += 1
        title, extracted, method = fetch_sway_content(context, url)
        found.append({
            "message_id": message_id,
            "url": url,
            "title": title,
            "extracted_text": extracted,
            "extraction_method": method,
        })
    return found


def hash_text(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def init_db(c: sqlite3.Connection):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS messages (
      fingerprint TEXT PRIMARY KEY, subject TEXT NOT NULL, sender TEXT,
      message_date TEXT, body_hash TEXT, first_seen_at TEXT, last_seen_at TEXT,
      body_text TEXT, raw_text TEXT, server_message_id TEXT, school_id TEXT,
      published_at TEXT, content_hash TEXT, notified_at TEXT
    );
    CREATE TABLE IF NOT EXISTS attachments (
      attachment_id TEXT PRIMARY KEY, message_fingerprint TEXT,
      filename TEXT, local_path TEXT, content_hash TEXT, extracted_text TEXT,
      first_seen_at TEXT, last_seen_at TEXT, server_attachment_id TEXT,
      message_id TEXT, mime_type TEXT, extraction_method TEXT
    );
    CREATE TABLE IF NOT EXISTS attachment_text (
      attachment_id TEXT PRIMARY KEY, extracted_text TEXT, extraction_method TEXT
    );
    CREATE TABLE IF NOT EXISTS message_links (
      link_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, message_fingerprint TEXT,
      source_url TEXT NOT NULL, title TEXT, content_hash TEXT, extracted_text TEXT,
      first_seen_at TEXT, last_seen_at TEXT, extraction_method TEXT
    );
    """)
    c.commit()


def extract_message(item: dict[str, Any], school_id: str | None) -> dict[str, Any] | None:
    mid = item.get("id") or item.get("uuid") or item.get("message_id")
    last = item.get("last_message") if isinstance(item.get("last_message"), dict) else item
    mid = mid or last.get("id") or last.get("uuid")
    if not mid: return None
    subject = text(item.get("title") or item.get("subject") or last.get("subject") or "(no subject)")
    sender = text(item.get("sender") or (item.get("teacher") or {}).get("name") or last.get("sender") or "")
    body = text(last.get("content") or last.get("body") or item.get("content") or "")
    published = (last.get("sent_at_timestamp") or last.get("sent_at") or last.get("created_at") or
                 last.get("published_at") or last.get("message_date") or item.get("published_at") or item.get("sent_at"))
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
    return {"id": str(mid), "subject": subject, "sender": sender, "body": body,
            "published": published, "raw": raw, "hash": hash_text(subject + "\n" + body),
            "item": item, "school_id": school_id}


def visible_docx_nodes(node: ET.Element):
    """Walk one OOXML AlternateContent branch rather than Choice and Fallback."""
    mc = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
    image_tags = {"{http://schemas.openxmlformats.org/drawingml/2006/main}blip",
                  "{urn:schemas-microsoft-com:vml}imagedata"}
    if node.tag == mc + "AlternateContent":
        choice = next((child for child in node if child.tag == mc + "Choice"
                       and any(descendant.tag in image_tags for descendant in child.iter())), None)
        if choice is None:
            choice = next((child for child in node if child.tag == mc + "Choice"), None)
        if choice is None:
            choice = next((child for child in node if child.tag == mc + "Fallback"), None)
        if choice is not None:
            yield from visible_docx_nodes(choice)
        return
    yield node
    for child in node:
        yield from visible_docx_nodes(child)


def extract_attachment_text(data: bytes, filename: str, mime_type: str) -> tuple[str, str]:
    """Extract searchable text without rejecting non-PDF attachments."""
    lower_name = filename.lower()
    mime = (mime_type or "").lower()
    if mime == "application/pdf" or lower_name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            extracted = "\\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(data)).pages).strip()
            return extracted, "pypdf"
        except Exception:
            return "", "unavailable_or_failed"
    if (mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            or lower_name.endswith(".docx")):
        try:
            # DOCX tables can be screenshots rather than w:tbl elements. OCR only
            # images referenced in the document, in the surrounding text order.
            w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
            r = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
            v = "{urn:schemas-microsoft-com:vml}"
            o = "{urn:schemas-microsoft-com:office:office}"
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                document_member = archive.getinfo("word/document.xml")
                if document_member.file_size > 4_000_000:
                    raise ValueError("DOCX document XML is too large")
                xml_data = archive.read(document_member)
                root = ET.fromstring(xml_data)
                drawings = list(root.iter(a + "blip")) + list(root.iter(v + "imagedata"))
                relationships = {}
                if drawings:
                    try:
                        rel_member = archive.getinfo("word/_rels/document.xml.rels")
                        if rel_member.file_size > 1_000_000:
                            raise ValueError("DOCX relationships XML is too large")
                        rels = ET.fromstring(archive.read(rel_member))
                    except (KeyError, ValueError, zipfile.BadZipFile, ET.ParseError, zlib.error):
                        rels = ()
                    relationships = {
                        item.get("Id"): item.get("Target")
                        for item in rels
                        if (item.get("Type", "").endswith("/image")
                            and item.get("TargetMode", "Internal").lower() == "internal")
                    }
                parts = []
                incomplete = False
                image_count = 0
                words = []
                for node in visible_docx_nodes(root):
                    if node.tag == w + "p":
                        if words:
                            parts.append(text(" ".join(words)))
                            words = []
                    elif node.tag == w + "t":
                        words.append(node.text or "")
                    elif node.tag in (a + "blip", v + "imagedata"):
                        relationship_id = (node.get(r + "embed") or node.get(r + "id")
                                           or node.get(o + "relid"))
                        if words:
                            parts.append(text(" ".join(words)))
                            words = []
                        image_count += 1
                        target = relationships.get(relationship_id)
                        path = posixpath.normpath(
                            target.lstrip("/") if target and target.startswith("/word/")
                            else posixpath.join("word", target or "")
                        )
                        ocr_text, ocr_complete = "", False
                        if image_count <= 8 and target and path.startswith("word/media/"):
                            try:
                                member = archive.getinfo(path)
                            except KeyError:
                                pass
                            else:
                                if member.file_size <= 8_000_000:
                                    try:
                                        image_data = archive.read(member)
                                    except Exception:
                                        # A corrupt image member must not erase text already read.
                                        pass
                                    else:
                                        ocr_text, ocr_complete = ocr_docx_image(image_data)
                        if ocr_text:
                            parts.append(f"[Embedded image {image_count} OCR]\n{ocr_text}\n[/Embedded image {image_count} OCR]")
                            if not ocr_complete:
                                incomplete = True
                                parts.append(f"[Embedded image {image_count}: OCR text truncated; check original attachment]")
                        else:
                            incomplete = True
                            parts.append(f"[Embedded image {image_count}: OCR unavailable or unreadable; check original attachment]")
                if words:
                    parts.append(text(" ".join(words)))
            method = "docx-ooxml+ocr-incomplete" if incomplete else "docx-ooxml+ocr" if image_count else "docx-ooxml"
            return "\n".join(part for part in parts if part), method
        except Exception:
            return "", "unavailable_or_failed"
    return "", "unsupported_format"


def ocr_docx_image(data: bytes) -> tuple[str, bool]:
    """OCR an embedded screenshot, returning (text, complete)."""
    if not shutil.which("tesseract"):
        return "", False
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 12_000_000:
                return "", False
            rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        image_bytes = io.BytesIO()
        background.convert("RGB").save(image_bytes, format="PNG")
        with tempfile.TemporaryFile(dir=os.environ.get("TMPDIR")) as output:
            result = subprocess.run(
                ["tesseract", "stdin", "stdout", "--psm", "11", "-l", "eng", "tsv"],
                input=image_bytes.getvalue(), stdout=output, stderr=subprocess.DEVNULL,
                timeout=15, check=False,
            )
            if result.returncode or output.tell() > 2_000_000:
                return "", False
            output.seek(0)
            tsv = output.read().decode("utf-8", errors="replace")
        # PSM 11 reads faint/highlighted table rows missed by PSM 3, but its
        # plain-text output can scramble cell order. Rebuild rows from boxes.
        boxes = []
        for box in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
            word = text(box.get("text"))
            if word:
                if len(boxes) >= 4_000:
                    return "", False
                boxes.append((int(box["left"]), int(box["top"]), int(box["width"]), int(box["height"]), word))
        if not boxes:
            return "", False
        heights = sorted(box[3] for box in boxes)
        median_height = heights[len(heights) // 2]
        line_tolerance = max(8, median_height // 2)
        column_gap = max(40, median_height * 3 // 2)
        rows = []
        for box in sorted(boxes, key=lambda item: (item[1] + item[3] / 2, item[0])):
            center = box[1] + box[3] / 2
            if not rows or abs(center - rows[-1][0]) > line_tolerance:
                rows.append((center, [box]))
            else:
                rows[-1][1].append(box)
        lines = []
        for _, row in rows:
            tokens = []
            previous_end = None
            for left, _, width, _, word in sorted(row):
                if previous_end is not None and left - previous_end > column_gap:
                    tokens.append("|")
                tokens.append(word)
                previous_end = left + width
            lines.append(" ".join(tokens))
        extracted = "\n".join(lines)
        return extracted[:10_000], len(extracted) <= 10_000
    except Exception:
        # Untrusted embedded pixels or a missing OCR dependency must not erase
        # the rest of the document; caller marks this image as incomplete.
        return "", False


def login_and_collect(refresh_attachments=False, refresh_links=False, _recovered=False):
    """Collect ParentMail responses, attachment bytes, and linked Sway text."""
    try:
        return _login_and_collect(refresh_attachments, refresh_links)
    except RuntimeError as e:
        if str(e) == "parentmail_login_401" and not _recovered:
            debug("confirmed login 401; resetting stale persistent profile once")
            shutil.rmtree(PROFILE, ignore_errors=True)
            return login_and_collect(refresh_attachments, refresh_links, True)
        raise


def _login_and_collect(refresh_attachments=False, refresh_links=False):
    if not EMAIL or not PASSWORD:
        raise RuntimeError("PARENTMAIL_EMAIL/PARENTMAIL_PASSWORD are not available")
    phase("configuration")
    headless = env_bool("PARENTMAIL_HEADLESS", True)
    debug(f"config data_dir={DATA_DIR} db={DB} profile={PROFILE} headless={headless}")
    PROFILE.mkdir(parents=True, exist_ok=True); PROFILE.chmod(0o700)
    with sync_playwright() as p:
        exe = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH")
        browser = p.chromium.launch_persistent_context(str(PROFILE), headless=headless, executable_path=exe or None,
            accept_downloads=True, viewport={"width":1280,"height":900})
        page = browser.pages[0] if browser.pages else browser.new_page()
        auth_status=[]
        def on_auth_response(resp):
            if "/api/v1.9/ss/v1/guardians/login" in resp.url:
                auth_status.append(resp.status)
        page.on("response", on_auth_response)
        phase("open_login")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        debug(f"login page url={page.url} title={page.title()} buttons={page.get_by_role('button').all_text_contents()[:5]}")
        # If a persisted session is valid, go directly to Messages.
        if "/auth/login" in page.url:
            phase("submit_email")
            email_fields = page.locator("input[type=email]")
            if email_fields.count():
                email_fields.first.fill(EMAIL)
                page.get_by_role("button", name="Login", exact=True).click()
                debug("login email submitted")
            page.wait_for_timeout(1200)
            try:
                phase("wait_iris")
                page.wait_for_url(re.compile(r"identity\.iris\.co\.uk"), timeout=30000)
            except PlaywrightTimeoutError:
                pass
            debug(f"after IRIS redirect url={page.url} buttons={page.get_by_role('button').all_text_contents()[:5]}")
            # A valid persisted session may land directly on the new portal
            # Dashboard. In that case there is no second IRIS Next step.
            already_authenticated = (
                "parents.parentmail.co.uk" in page.url
                and page.get_by_role("button", name=re.compile("View Profile", re.I)).count() > 0
            )
            if already_authenticated:
                debug("existing authenticated portal session reached Dashboard")
            else:
                # The direct portal may remain on "Logging in..." while the
                # asynchronous OAuth redirect is completing.
                next_button = page.get_by_role("button", name=re.compile("^Next$", re.I))
                try:
                    next_button.wait_for(state="visible", timeout=30000)
                except PlaywrightTimeoutError:
                    if auth_status and auth_status[-1] == 401:
                        raise RuntimeError("parentmail_login_401")
                    raise RuntimeError(f"IRIS Next step was not available after login; url={page.url}; buttons={page.get_by_role('button').all_text_contents()[:5]}; body={re.sub(r'\\s+', ' ', page.locator('body').inner_text())[:300]}")
                # IRIS/Okta uses a text input for the second email step.
                if next_button.count():
                    text_fields = page.locator("input[type=text]")
                    if text_fields.count():
                        text_fields.first.fill(EMAIL)
                    next_button.first.click()
                    debug("IRIS Next clicked")
                try:
                    phase("wait_password")
                    page.locator("input[type=password]").first.wait_for(state="visible", timeout=30000)
                except PlaywrightTimeoutError:
                    raise RuntimeError("password field was not available after the asynchronous login flow")
                pw = page.locator("input[type=password]")
                if not pw.count():
                    raise RuntimeError("password field was not available after the asynchronous login flow")
                pw.first.fill(PASSWORD)
                phase("submit_password")
                page.get_by_role("button", name=re.compile("Verify|Sign in|Log in", re.I)).click()
                debug("password submitted")
                page.wait_for_timeout(2500)
                for label in ["Stay signed in", "Keep me signed in"]:
                    try:
                        page.get_by_role("button", name=label, exact=True).click(timeout=1500); break
                    except Exception: pass
                debug(f"post-IRIS url={page.url} buttons={page.get_by_role('button').all_text_contents()[:5]}")
                page.wait_for_timeout(2500)
            try:
                page.wait_for_url(re.compile(r"pmx\.parentmail\.co\.uk|parents\.parentmail\.co\.uk"), timeout=30000)
            except PlaywrightTimeoutError:
                pass
        phase("open_messages")
        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        debug(f"messages page url={page.url} title={page.title()}")
        if "parents.parentmail.co.uk" not in page.url:
            raise RuntimeError("authenticated ParentMail portal was not reached")
        # Capture conversation API responses made by the portal itself.
        responses=[]
        def on_response(resp):
            u=resp.url
            if "/conversations" in u and resp.request.method == "GET":
                try:
                    data=resp.json()
                    if isinstance(data,dict) and isinstance(data.get("data"),list): responses.append(data)
                except Exception: pass
        page.on("response", on_response)
        phase("capture_conversations")
        page.reload(wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        resources = page.evaluate("performance.getEntriesByType('resource').map(x=>x.name)")
        debug(f"after messages reload url={page.url} resources={len(resources)}")
        # Ask the authenticated page to fetch its own discovered conversation URLs, using its cookies/context.
        result=page.evaluate("""async () => {
          const keys=Object.keys(localStorage);
          const raw=localStorage.getItem('SchoolSpiderParentPortal/main/authtoken');
          let token=null; try { token=raw ? JSON.parse(raw) : null; } catch(e) {}
          const urls=[...new Set(performance.getEntriesByType('resource').map(x=>x.name).filter(x=>x.includes('/conversations')))].slice(0,20);
          const out=[];
          for (const u of urls) { try { const r=await fetch(u,{credentials:'include',headers:{Accept:'application/json'}}); const j=await r.json(); if (j && Array.isArray(j.data)) out.push(j); } catch(e) {} }
          return {urls:urls.length, responses:out, keys};
        }""")
        if result.get("responses"): responses.extend(result["responses"])
        debug(f"conversation capture responses={len(responses)} browser_fetch_responses={len(result.get('responses', []))} keys={result.get('keys', [])}")
        phase("collect_attachments")
        # Visit detail pages in the same authenticated browser context to collect
        # attachment links. By default only unknown message IDs are opened; the
        # refresh flag is for a controlled migration/backfill run.
        known = set()
        if not refresh_attachments and DB.exists():
            db = sqlite3.connect(DB)
            known = {r[0] for r in db.execute("select server_message_id from messages where server_message_id is not null")}
            db.close()
        attachments=[]
        candidate_ids=[]
        for resp in responses:
            for item in resp.get("data",[]):
                if isinstance(item,dict):
                    mid=item.get("id") or item.get("uuid")
                    if mid and (refresh_attachments or mid not in known): candidate_ids.append(str(mid))
        for mid in dict.fromkeys(candidate_ids):
            page.goto("https://parents.parentmail.co.uk/messages/" + quote(mid, safe=""), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(900)
            for link in page.locator("a[href*='/download/']").all():
                href=link.get_attribute("href") or ""
                if not href: continue
                download_url=urljoin(page.url, href)
                parsed_url=urlparse(download_url)
                if parsed_url.scheme != "https" or parsed_url.hostname not in {"parents.parentmail.co.uk", "pmx.parentmail.co.uk"}:
                    debug("skipping attachment URL outside ParentMail origin")
                    continue
                filename=text(link.inner_text()) or parsed_url.path.rsplit("/",1)[-1]
                response=browser.request.get(download_url, timeout=30000)
                if response.status != 200: continue
                body=response.body()
                if not body: continue
                content_type=(response.headers.get("content-type") or "application/octet-stream").split(";",1)[0].strip()
                attachments.append({"message_id":mid,"filename":filename,"url":download_url,"bytes":body,"mime_type":content_type})
        browser.close()
        phase("collect_sway_links")
        cached_links = set()
        recently_failed = set()
        known_message_ids = set()
        if DB.exists():
            db = sqlite3.connect(DB)
            try:
                known_message_ids = {str(row[0]) for row in db.execute(
                    "select server_message_id from messages where server_message_id is not null")}
                retry_cutoff = (dt.datetime.now(dt.timezone.utc) - SWAY_FAILURE_RETRY_AFTER).isoformat()
                for message_id, source_url, method, last_seen in db.execute(
                        "select message_id,source_url,extraction_method,last_seen_at from message_links"):
                    key = (str(message_id), str(source_url))
                    if method in SWAY_SUCCESS_METHODS:
                        cached_links.add(key)
                    elif (last_seen or "") >= retry_cutoff:
                        recently_failed.add(key)
            except sqlite3.OperationalError:
                # Existing installations gain the link cache on this run.
                pass
            finally:
                db.close()
        linked_content = []
        candidates = sway_link_candidates(responses)
        pending = [key for key in candidates if refresh_links or key not in cached_links]
        if pending:
            link_options = {"force": refresh_links, "known_message_ids": known_message_ids,
                            "recently_failed": recently_failed}
            sway_browser = None
            sway_context = None
            try:
                sway_browser, sway_context = launch_sway_browser(p, headless, exe)
                linked_content = collect_sway_links(responses, sway_context, cached_links, **link_options)
            except Exception:
                # Sway is optional enrichment; preserve the ParentMail message and
                # tell the summarizer that the public page could not be read.
                debug("isolated Sway browser could not start")
                linked_content = collect_sway_links(responses, None, cached_links, **link_options)
            finally:
                # A crashed Sway browser must not fail the ParentMail run.
                for closable in (sway_context, sway_browser):
                    if closable is not None:
                        try:
                            closable.close()
                        except Exception:
                            debug("isolated Sway browser did not close cleanly")
        if not responses:
            raise RuntimeError("authenticated portal returned no conversation API responses")
        return responses, attachments, linked_content


def persist(responses, attachments=None, dry_run=False, *, linked_content=None):
    phase("persist_sqlite")
    attachments = attachments or []
    linked_content = linked_content or []
    DB.parent.mkdir(parents=True, exist_ok=True)
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; init_db(c)
    parsed=[]; seen=set(); school=None
    for resp in responses:
        for item in resp.get("data",[]):
            if isinstance(item,dict):
                school = school or str((item.get("teacher") or {}).get("id") or item.get("school_id") or "") or None
                m=extract_message(item,school)
                if m and m["id"] not in seen: seen.add(m["id"]); parsed.append(m)
    if not parsed:
        raise RuntimeError("conversation responses contained no parseable messages")
    baseline_marker = DB.parent / ".deterministic-worker-baselined"
    migration_baseline = not baseline_marker.exists()
    baseline = c.execute("select count(*) from messages").fetchone()[0] == 0
    new=[]; new_attachments=[]; fingerprints={}
    try:
        for m in parsed:
            existing = c.execute("select fingerprint,content_hash from messages where server_message_id=? order by length(coalesce(body_text,'')) desc, first_seen_at asc limit 1", (m["id"],)).fetchone()
            fp = existing[0] if existing else m["id"]
            fingerprints[m["id"]]=fp
            old = existing
            changed=old is not None and old[1] != m["hash"]
            if old is None or changed:
                c.execute("""insert into messages(fingerprint,subject,sender,message_date,body_hash,first_seen_at,last_seen_at,body_text,raw_text,server_message_id,school_id,published_at,content_hash,notified_at)
                values(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL) on conflict(fingerprint) do update set subject=excluded.subject,sender=excluded.sender,last_seen_at=excluded.last_seen_at,body_text=excluded.body_text,raw_text=excluded.raw_text,content_hash=excluded.content_hash,body_hash=excluded.body_hash,published_at=excluded.published_at""",
                (fp,m['subject'],m['sender'],m['published'],m['hash'],now(),now(),m['body'],m['raw'],m['id'],m['school_id'],m['published'],m['hash']))
                # Existing IDs are silently migrated once because the old parser
                # used a different normalization. New IDs are always eligible.
                if old is None:
                    new.append(m)
            else:
                c.execute("update messages set last_seen_at=? where fingerprint=?",(now(),fp))
        for a in attachments:
            digest=hashlib.sha256(a["bytes"]).hexdigest()
            server_id=hash_text(a["url"])
            attachment_id=hash_text(a["message_id"]+"\\n"+a["filename"]+"\\n"+digest)
            old_a=c.execute("select attachment_id from attachments where attachment_id=? or server_attachment_id=?",(attachment_id,server_id)).fetchone()
            safe_message_id=re.sub(r"[^A-Za-z0-9._-]+", "_", str(a["message_id"])).strip("._") or "message"
            safe=re.sub(r"[^A-Za-z0-9._-]+","_",a["filename"]).strip("._") or "attachment.bin"
            local=ATTACHMENTS/(safe_message_id+"_"+safe)
            extracted, method = extract_attachment_text(a["bytes"], a["filename"], a["mime_type"])
            if not dry_run:
                ATTACHMENTS.mkdir(parents=True,exist_ok=True); local.write_bytes(a["bytes"])
            c.execute("""insert into attachments(attachment_id,message_fingerprint,filename,local_path,content_hash,extracted_text,first_seen_at,last_seen_at,server_attachment_id,message_id,mime_type,extraction_method)
              values(?,?,?,?,?,?,?,?,?,?,?,?) on conflict(attachment_id) do update set local_path=excluded.local_path,content_hash=excluded.content_hash,extracted_text=excluded.extracted_text,last_seen_at=excluded.last_seen_at,extraction_method=excluded.extraction_method""",
              (attachment_id,fingerprints.get(a["message_id"],a["message_id"]),a["filename"],str(local),digest,extracted,now(),now(),server_id,a["message_id"],a["mime_type"],method))
            c.execute("insert into attachment_text(attachment_id,extracted_text,extraction_method) values(?,?,?) on conflict(attachment_id) do update set extracted_text=excluded.extracted_text,extraction_method=excluded.extraction_method",(attachment_id,extracted,method))
            if old_a is None: new_attachments.append(a)
        for link in linked_content:
            message_id = str(link.get("message_id") or "")
            source_url = canonical_sway_url(link.get("url") or "")
            fingerprint = fingerprints.get(message_id)
            if not message_id or not source_url or not fingerprint:
                continue
            if link.get("extraction_method") == SWAY_NOT_ATTEMPTED:
                continue
            extracted = str(link.get("extracted_text") or "")[:MAX_SWAY_TEXT_CHARS]
            content_hash = hash_text(extracted)
            link_id = hash_text(message_id + "\\n" + source_url)
            timestamp = now()
            c.execute("""insert into message_links(link_id,message_id,message_fingerprint,source_url,title,content_hash,extracted_text,first_seen_at,last_seen_at,extraction_method)
              values(?,?,?,?,?,?,?,?,?,?) on conflict(link_id) do update set last_seen_at=excluded.last_seen_at,
              title=case when {keep} then message_links.title else excluded.title end,
              content_hash=case when {keep} then message_links.content_hash else excluded.content_hash end,
              extracted_text=case when {keep} then message_links.extracted_text else excluded.extracted_text end,
              extraction_method=case when {keep} then message_links.extraction_method else excluded.extraction_method end""".format(
                # A failed refresh keeps the last good page text.
                keep="excluded.extracted_text='' and coalesce(message_links.extracted_text,'')<>''"),
              (link_id,message_id,fingerprint,source_url,text(link.get("title")),content_hash,extracted,timestamp,timestamp,text(link.get("extraction_method"))))
        if not dry_run:
            c.commit()
            if migration_baseline:
                baseline_marker.write_text(now() + "\n")
        else:
            c.rollback()
    except Exception:
        c.rollback(); raise
    if baseline:
        c.close()
        return "SILENT"
    if not new and not new_attachments:
        c.close()
        return "SILENT"
    parts=[f"MESSAGE\nSubject: {m['subject']}\nSender: {m['sender']}\nPublished: {m['published'] or 'date unavailable'}\nBody:\n{m['body']}" for m in new]
    new_message_ids = {m["id"] for m in new}
    linked_budget = 50_000
    nl = chr(10)
    for link in linked_content:
        if str(link.get("message_id")) not in new_message_ids:
            continue
        title = text(link.get("title")) or "Untitled Sway page"
        url = canonical_sway_url(link.get("url") or "") or ""
        extracted = str(link.get("extracted_text") or "")
        method = text(link.get("extraction_method")) or "sway_unavailable"
        if not extracted:
            parts.append(nl.join(("LINKED SWAY PAGE", f"Title: {title}", f"URL: {url}", f"Content could not be extracted ({method}); do not claim this page was read.")))
            continue
        if linked_budget <= 0:
            parts.append("LINKED SWAY PAGE" + nl + "Additional page text omitted due to the run size limit.")
            break
        included = extracted[:linked_budget]
        truncated = len(included) < len(extracted) or method == "sway-playwright-truncated"
        suffix = nl + "[Linked page text truncated; open the original page for the rest.]" if truncated else ""
        parts.append(nl.join(("LINKED SWAY PAGE (untrusted page content)", f"Title: {title}", f"URL: {url}", "Extracted text:", included + suffix)))
        linked_budget -= len(included)
    for m in new:
        if len(extract_sway_urls(m["body"], limit=None)) > MAX_SWAY_LINKS_PER_MESSAGE:
            parts.append(nl.join(("LINKED SWAY PAGE", f"Message: {m['subject']}",
                f"Only the first {MAX_SWAY_LINKS_PER_MESSAGE} Sway links in this message were considered; do not claim the others were read.")))
    for a in new_attachments:
        attachment_id=hash_text(a["message_id"]+"\\n"+a["filename"]+"\\n"+hashlib.sha256(a["bytes"]).hexdigest())
        row=c.execute("select extracted_text from attachment_text where attachment_id=?", (attachment_id,)).fetchone()
        parts.append(f"ATTACHMENT\nFilename: {a['filename']}\nMessage ID: {a['message_id']}\nMime: {a['mime_type']}\nExtracted text:\n{row[0] if row else ''}")
    c.close()
    return "\n\n".join(parts)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--dry-run',action='store_true'); ap.add_argument('--refresh-attachments',action='store_true'); ap.add_argument('--refresh-links',action='store_true'); args=ap.parse_args()
    try:
        responses, attachments, linked_content=login_and_collect(args.refresh_attachments,args.refresh_links)
        print(persist(responses,attachments,args.dry_run,linked_content=linked_content))
        return 0
    except PlaywrightTimeoutError:
        if DEBUG:
            print("DEBUG PlaywrightTimeoutError")
        else:
            print(f"ERROR ParentMail worker failed\nphase: {CURRENT_PHASE}\nreason: Playwright timeout")
        return 2
    except Exception as e:
        msg=re.sub(r"(?i)(password|token|authorization|bearer|cookie)\\s*[:=]\\s*[^\\s]+", r"\\1=[redacted]", str(e))
        if DEBUG:
            print("DEBUG",type(e).__name__,msg[:500])
        else:
            print(f"ERROR ParentMail worker failed\nphase: {CURRENT_PHASE}\nreason: {type(e).__name__}: {msg[:500]}")
        return 2
if __name__=='__main__': raise SystemExit(main())
