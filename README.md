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
- Pillow and Tesseract with English language data for text embedded as images in DOCX

Example:

```bash
python -m pip install playwright pypdf pillow
playwright install chromium
# Install Tesseract separately: Arch `sudo pacman -S tesseract tesseract-data-eng`
# or macOS `brew install tesseract`.
```

The production Hermes environment already provides Playwright and Chromium through its runtime.
It currently also has Pillow and `tesseract`/English data. Check after a runtime upgrade:
`python -c 'from PIL import Image; print(Image.__version__)'` and `tesseract --list-langs`.

DOCX extraction preserves paragraph/table-cell text and OCRs embedded screenshot
tables in document order. Transparent images are composited onto white before OCR.
If an embedded image is unreadable, too large, or the OCR dependencies are missing,
the text includes an explicit incomplete-OCR marker instead of claiming no dates exist.
Images are limited in count/size and OCR has a per-image timeout.

Install local dependencies with:

```bash
make install
```

## Local virtual environment

Standard local workflow is a project-scoped virtual environment in `.venv`.

```bash
make env
make install
```

To open a shell with the environment activated:

```bash
make shell
```

## Environment

```bash
export PARENTMAIL_EMAIL='your-parentmail-email'
export PARENTMAIL_PASSWORD='your-parentmail-password'
export AGENT_BROWSER_EXECUTABLE_PATH='/path/to/chromium'
export PARENTMAIL_DATA_DIR='/opt/data/parentmail'  # production/Hermes default
```

Never commit an `.env` file containing values.

For local runs with `make`, state is stored in the repo checkout at `./.local/parentmail` (already ignored by Git), so user accounts do not need access to `/opt/data`.

Browser visibility defaults:

- The Python script defaults to headless mode unless `PARENTMAIL_HEADLESS` is set.
- The provided `make` targets set `PARENTMAIL_HEADLESS=false` on macOS so you can watch the flow, and `true` on other OSes.

Manual overrides:

```bash
PARENTMAIL_HEADLESS=true make run-dry-run
PARENTMAIL_HEADLESS=false make run
```

## Run

The script uses the new portal login flow and keeps its browser state under `/opt/data/parentmail/browser-profile-v2` in the Hermes runtime. It stores messages and attachments under `/opt/data/parentmail/`.

```bash
python parentmail_watch.py
```

For a no-write verification:

```bash
python parentmail_watch.py --dry-run
```

## Run via 1Password CLI (`op`)

If your ParentMail credentials are stored in 1Password, use the included `Makefile` target to fetch them at runtime and run the script without exporting secrets in your shell profile.

Default item ID is already set to your ParentMail item:

```bash
make run
```

If you want to use production-style paths locally, override the state directory:

```bash
PARENTMAIL_DATA_DIR='/opt/data/parentmail' make run
```

Other run modes:

```bash
make run-dry-run
make run-refresh-attachments
```

You can override the item ID or field labels if needed:

```bash
PARENTMAIL_OP_ITEM_ID='your-item-id' make run
PARENTMAIL_OP_USERNAME_FIELD='username' PARENTMAIL_OP_PASSWORD_FIELD='password' make run
```

To backfill/verify attachment links in a controlled run:

```bash
python parentmail_watch.py --dry-run --refresh-attachments
```

Successful no-change output is exactly `SILENT`. A non-zero exit means the run must be treated as failed.

## Cron

The deployed hourly job uses this deterministic script as a **pre-run collector**;
its LLM only summarizes committed new items and cannot browse ParentMail:

- script: `/opt/data/scripts/parentmail_watch_cron.sh` (executes this checkout's `parentmail_watch.py`)
- no_agent: `false` (summarization only)
- schedule: hourly
- delivery: the intended private channel

Run extraction regression tests with `PYTHONPATH=. python -m unittest discover -s tests -v`.
To also probe a private, locally stored Forest School DOCX without committing it,
set `PARENTMAIL_TEST_DOCX=/path/to/letter.docx` before that command.
Re-extracting an existing attachment should update its saved text without making
the already-persisted attachment eligible for another notification.

## State

The production state path is not part of this repository:

```text
/opt/data/parentmail/messages.sqlite3
/opt/data/parentmail/attachments/
/opt/data/parentmail/browser-profile-v2/
```

Keep these paths out of commits and back them up separately with appropriate permissions.
