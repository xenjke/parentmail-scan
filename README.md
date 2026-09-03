# ParentMail Scan

Deterministic, read-only IRIS ParentMail monitor for Home Assistant/Hermes cron.

## Safety contract

- Never sends, deletes, archives, or marks ParentMail records read/unread.
- Credentials are read only from `PARENTMAIL_EMAIL` and `PARENTMAIL_PASSWORD`.
- Credentials, OAuth tokens, cookies, browser profiles, SQLite state, attachments, and debug output must remain outside Git.
- An item is eligible for notification only after its message/attachment record is successfully committed to SQLite.
- Authentication/API/persistence failures fail closed; they must not be reported as `SILENT` success.

## Runtime dependencies

- Python 3.11+
- Playwright Python package and a Chromium executable
- `pypdf` for PDF extraction

Example:

```bash
python -m pip install playwright pypdf
playwright install chromium
```

The production Hermes environment already provides Playwright and Chromium through its runtime.

## Environment

```bash
export PARENTMAIL_EMAIL='your-parentmail-email'
export PARENTMAIL_PASSWORD='your-parentmail-password'
export AGENT_BROWSER_EXECUTABLE_PATH='/path/to/chromium'
```

Never commit an `.env` file containing values.

## Run

The script uses the new portal login flow and keeps its browser state under `/opt/data/parentmail/browser-profile-v2` in the Hermes runtime. It stores messages and attachments under `/opt/data/parentmail/`.

```bash
python parentmail_watch.py
```

For a no-write verification:

```bash
python parentmail_watch.py --dry-run
```

To backfill/verify attachment links in a controlled run:

```bash
python parentmail_watch.py --dry-run --refresh-attachments
```

Successful no-change output is exactly `SILENT`. A non-zero exit means the run must be treated as failed.

## Cron

Use the script as a script-only job, not an LLM-driven browser job:

- script: `parentmail_watch.py`
- no_agent: `true`
- schedule: hourly
- delivery: the intended private channel

## State

The production state path is not part of this repository:

```text
/opt/data/parentmail/messages.sqlite3
/opt/data/parentmail/attachments/
/opt/data/parentmail/browser-profile-v2/
```

Keep these paths out of commits and back them up separately with appropriate permissions.
