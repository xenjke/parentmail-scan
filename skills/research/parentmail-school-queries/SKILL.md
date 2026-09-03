---
name: parentmail-school-queries
description: Use when answering school questions from ParentMail SQLite data and attachments.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ParentMail, school, SQLite, attachments, OCR, evidence]
    related_skills: []
---

# ParentMail school queries

## Overview

Use the persisted ParentMail archive as the first source for questions about school messages, dates, deadlines, routines, trips, forms, reading, PE, uniform, and attached letters. Query the database before making claims; use the original message and attachment text as evidence, not memory or notification timing.

## When to Use

- The user asks what a school message said.
- The user asks about a deadline, event, school routine, PE, reading, uniform, or holiday task.
- The answer may be in a ParentMail attachment rather than the message body.
- The user asks for the newest message or when a message arrived.

Do not use this skill to send, delete, archive, mark read/unread, or change ParentMail settings.

## Data locations

The production archive normally uses:

```text
$PARENTMAIL_DB_PATH
$PARENTMAIL_ATTACHMENTS_DIR
```

In the Hermes deployment these default to:

```text
/opt/data/parentmail/messages.sqlite3
/opt/data/parentmail/attachments/
```

Credentials, browser profiles, cookies, and tokens are never needed for read-only database queries and must not be printed.

## Schema and identity

The current SQLite schema contains:

### `messages`

- `server_message_id` — ParentMail logical message ID; use this as the primary dedupe identity.
- `fingerprint` — legacy/content identity; do not use it alone to decide whether a message is new.
- `subject`, `sender`, `body_text`, `raw_text` — message content.
- `published_at`, `message_date` — source-provided/original message date fields.
- `first_seen_at`, `last_seen_at` — local watcher observation timestamps.
- `content_hash`, `body_hash` — content comparison fields.
- `school_id`, `notified_at` — source metadata and notification bookkeeping.

### `attachments`

- `message_id` — ParentMail message ID.
- `message_fingerprint` — optional legacy link; prefer `message_id` for joins.
- `filename`, `local_path`, `mime_type` — stored file metadata.
- `content_hash` — local content identity.
- `extracted_text`, `extraction_method` — text extracted from the original file.
- `server_attachment_id` — source attachment identity when available.
- `first_seen_at`, `last_seen_at` — local observation timestamps.

### `attachment_text`

- `attachment_id`, `extracted_text`, `extraction_method`.

Use parameterized SQL for user-supplied search terms. Treat duplicate rows with the same non-null `server_message_id` as one logical message and select the most complete body or earliest observation when displaying one result.

## Date semantics

Always label which date is being reported:

- **Original message date:** `published_at` or `message_date`.
- **Watcher first observed:** `first_seen_at`.
- **Watcher last observed:** `last_seen_at`.
- **Push/delivery time:** unavailable unless ParentMail explicitly provides a delivery/event timestamp.

Never describe `published_at` as the time a notification arrived. A message can be published in July and first observed by the watcher in September.

Example query:

```sql
SELECT subject, sender, published_at, message_date,
       first_seen_at, last_seen_at, server_message_id
FROM messages
ORDER BY datetime(COALESCE(published_at, message_date)) DESC
LIMIT 20;
```

For recently discovered records, use observation time instead:

```sql
SELECT subject, sender, published_at, first_seen_at, server_message_id
FROM messages
ORDER BY datetime(first_seen_at) DESC
LIMIT 20;
```

## Query recipes

### Find a message by topic

```sql
SELECT server_message_id, subject, sender, published_at,
       first_seen_at, body_text
FROM messages
WHERE lower(subject || ' ' || body_text) LIKE '%' || lower(?) || '%'
ORDER BY datetime(COALESCE(published_at, message_date)) DESC;
```

### Read message and attachment evidence together

```sql
SELECT m.server_message_id, m.subject, m.sender,
       m.published_at, m.body_text,
       a.filename, a.local_path, a.extracted_text,
       a.extraction_method
FROM messages AS m
LEFT JOIN attachments AS a
  ON a.message_id = m.server_message_id
WHERE m.server_message_id = ?
ORDER BY a.filename;
```

If the message has no attachment rows, inspect the portal detail page or worker logs before concluding there are no attachments. A previous failed download may leave a small JSON error response with a `.pdf` filename; validate the file magic bytes (`%PDF`) before treating it as a PDF.

### Search attachments for a topic

```sql
SELECT m.subject, m.sender, m.published_at,
       a.filename, a.extracted_text, a.extraction_method
FROM attachments AS a
JOIN messages AS m
  ON m.server_message_id = a.message_id
WHERE lower(COALESCE(a.extracted_text, '')) LIKE '%' || lower(?) || '%'
ORDER BY datetime(COALESCE(m.published_at, m.message_date)) DESC;
```

Search both `body_text` and attachment text for terms such as `deadline`, `consent`, `PE`, `kit`, `reading`, `trip`, `uniform`, `Forest School`, `bring`, `return`, and `school day`.

### Inspect the complete text of one message

```sql
SELECT subject, sender, published_at, message_date,
       first_seen_at, body_text, raw_text
FROM messages
WHERE server_message_id = ?;
```

Then list all related attachments and read their extracted text. Do not rely on the short body preview when it says “Please see attached.”

## Answering rules

1. Query the database using the logical `server_message_id`.
2. Include attachment text when the body refers to an attachment or the question concerns a form, deadline, date, medical information, trip, or detailed requirements.
3. Prefer exact dates, times, URLs, and instructions from the source text.
4. Separate confirmed facts from interpretation. Say when a date comes from an attachment or when a document is historical.
5. If a message is old but was recently observed, report both dates explicitly.
6. For medical, financial, safeguarding, or consent content, quote the source instruction and avoid adding advice not present in the message; direct the user to the school/NHS contact details for decisions.
7. Keep the final answer concise: subject/source, relevant date or deadline, action required, and attachment names when useful.

## Newness and notifications

The deterministic watcher decides notification eligibility after successful SQLite commit. For query work, do not infer “new” from `Unread`, `first_seen_at`, or a high row number alone. A record is logically new only when its `server_message_id` was not previously persisted; a changed body or newly added attachment must be explicitly identified by content/source identity.

## Common pitfalls

- Ordering by `published_at` and calling the result the latest arrival.
- Treating a relative UI label such as `1 day ago` as canonical.
- Joining attachments through `fingerprint` when `message_id` is available.
- Treating an unauthenticated JSON error response as a downloaded PDF.
- Ignoring attachments because the short message body only says “Please see attached.”
- Applying a historical term, PE schedule, or curriculum document to the current school year without checking its named term/year.
- Reporting a parsed candidate before the database transaction commits.

## Verification checklist

- [ ] The query used `server_message_id` or an explicitly documented fallback.
- [ ] Message body and relevant attachment text were checked.
- [ ] Original publication date and watcher observation time were not conflated.
- [ ] PDF/file validity and extraction method were considered.
- [ ] Deadlines and required actions were copied accurately.
- [ ] Historical documents were labelled as historical when appropriate.
- [ ] No ParentMail mutation was performed.
