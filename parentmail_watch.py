#!/usr/bin/env python3
"""Deterministic, read-only IRIS ParentMail watcher.

Authentication and API calls stay inside a persistent Playwright browser context.
Only committed SQLite changes are eligible for output.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, html, io, json, os, re, shutil, sqlite3, sys
from pathlib import Path
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


def debug(message: str):
    if DEBUG:
        print("DEBUG", message, flush=True)


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


def login_and_collect(refresh_attachments=False, _recovered=False):
    try:
        return _login_and_collect(refresh_attachments)
    except RuntimeError as e:
        if str(e) == "parentmail_login_401" and not _recovered:
            debug("confirmed login 401; resetting stale persistent profile once")
            shutil.rmtree(PROFILE, ignore_errors=True)
            return login_and_collect(refresh_attachments, True)
        raise


def _login_and_collect(refresh_attachments=False):
    if not EMAIL or not PASSWORD:
        raise RuntimeError("PARENTMAIL_EMAIL/PARENTMAIL_PASSWORD are not available")
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
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        debug(f"login page url={page.url} title={page.title()} buttons={page.get_by_role('button').all_text_contents()[:5]}")
        # If a persisted session is valid, go directly to Messages.
        if "/auth/login" in page.url:
            email_fields = page.locator("input[type=email]")
            if email_fields.count():
                email_fields.first.fill(EMAIL)
                page.get_by_role("button", name="Login", exact=True).click()
                debug("login email submitted")
            page.wait_for_timeout(1200)
            try:
                page.wait_for_url(re.compile(r"identity\.iris\.co\.uk"), timeout=30000)
            except PlaywrightTimeoutError:
                pass
            debug(f"after IRIS redirect url={page.url} buttons={page.get_by_role('button').all_text_contents()[:5]}")
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
                page.locator("input[type=password]").first.wait_for(state="visible", timeout=30000)
            except PlaywrightTimeoutError:
                raise RuntimeError("password field was not available after the asynchronous login flow")
            pw = page.locator("input[type=password]")
            if not pw.count():
                raise RuntimeError("password field was not available after the asynchronous login flow")
            pw.first.fill(PASSWORD)
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
            page.goto("https://parents.parentmail.co.uk/messages/"+mid, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(900)
            for link in page.locator("a[href*='/download/']").all():
                href=link.get_attribute("href") or ""
                if not href: continue
                filename=text(link.inner_text()) or href.rsplit("/",1)[-1]
                response=browser.request.get(href, timeout=30000)
                if response.status != 200: continue
                body=response.body()
                if not body.startswith(b"%PDF"):
                    continue
                attachments.append({"message_id":mid,"filename":filename,"url":href,"bytes":body,"mime_type":"application/pdf"})
        browser.close()
        if not responses:
            raise RuntimeError("authenticated portal returned no conversation API responses")
        return responses, attachments


def persist(responses, attachments=None, dry_run=False):
    attachments = attachments or []
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
            safe=re.sub(r"[^A-Za-z0-9._-]+","_",a["filename"]).strip("._") or "attachment.pdf"
            local=ATTACHMENTS/(a["message_id"]+"_"+safe)
            extracted=""; method="none"
            try:
                from pypdf import PdfReader
                extracted="\\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(a["bytes"])).pages).strip()
                method="pypdf"
            except Exception:
                method="unavailable_or_failed"
            if not dry_run:
                ATTACHMENTS.mkdir(parents=True,exist_ok=True); local.write_bytes(a["bytes"])
            c.execute("""insert into attachments(attachment_id,message_fingerprint,filename,local_path,content_hash,extracted_text,first_seen_at,last_seen_at,server_attachment_id,message_id,mime_type,extraction_method)
              values(?,?,?,?,?,?,?,?,?,?,?,?) on conflict(attachment_id) do update set local_path=excluded.local_path,content_hash=excluded.content_hash,extracted_text=excluded.extracted_text,last_seen_at=excluded.last_seen_at,extraction_method=excluded.extraction_method""",
              (attachment_id,fingerprints.get(a["message_id"],a["message_id"]),a["filename"],str(local),digest,extracted,now(),now(),server_id,a["message_id"],a["mime_type"],method))
            c.execute("insert into attachment_text(attachment_id,extracted_text,extraction_method) values(?,?,?) on conflict(attachment_id) do update set extracted_text=excluded.extracted_text,extraction_method=excluded.extraction_method",(attachment_id,extracted,method))
            if old_a is None: new_attachments.append(a)
        if not dry_run:
            c.commit()
            if migration_baseline:
                baseline_marker.write_text(now() + "\n")
        else:
            c.rollback()
    except Exception:
        c.rollback(); raise
    c.close()
    if baseline: return "SILENT"
    if not new and not new_attachments: return "SILENT"
    parts=[f"{m['subject']}\n{m['sender']}\n{m['published'] or 'date unavailable'}\n{m['body'][:500]}" for m in new]
    parts.extend(f"Attachment added: {a['filename']} (message {a['message_id']})" for a in new_attachments)
    return "\n\n".join(parts)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--dry-run',action='store_true'); ap.add_argument('--refresh-attachments',action='store_true'); args=ap.parse_args()
    try:
        responses, attachments=login_and_collect(args.refresh_attachments)
        print(persist(responses,attachments,args.dry_run))
        return 0
    except PlaywrightTimeoutError as e:
        if os.environ.get("PARENTMAIL_DEBUG"):
            print("DEBUG PlaywrightTimeoutError")
        else:
            print("SILENT")
        return 2
    except Exception as e:
        # Do not leak credentials/tokens or noisy recaps.
        if os.environ.get("PARENTMAIL_DEBUG"):
            msg=re.sub(r"(?i)(password|token|authorization|bearer|cookie)\\s*[:=]\\s*[^\\s]+", r"\\1=[redacted]", str(e))
            print("DEBUG",type(e).__name__,msg[:500])
        else:
            print("SILENT")
        return 2
if __name__=='__main__': raise SystemExit(main())
