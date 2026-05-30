# mangadot-upload

Folder-based batch uploader for mangadot.net.
Reads your chapter image folders, zips them in-memory, and uploads them via
the TUS protocol with resumable upload support.

Features:
- Upload by series name from a JSON library (`--series`) or by raw manga ID / folder
- Auto-skips chapters that are already uploaded (or replace them with `--reupload`)
- Resumable uploads — if a chunk fails, it queries the server's real offset and resumes
- Retry with backoff on transient network/server errors
- Auto-refreshes the access token before it expires
- Interactive re-login flow if Cloudflare blocks the session mid-run
- Three auth methods: email+password, cookies.txt, or live browser cookie extraction

---

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Copy `config.ini.example` to `config.ini` and fill in your details:
   - Your login credentials (or cookies)
   - Your manga's ID (from its URL on mangadot.net) — only needed as a fallback
   - Path to your chapters folder — only needed as a fallback
   - Your scanlator name or group ID

3. (Optional but recommended) Copy `manga.json.example` to `manga.json` and add
   one entry per series you upload. You can then use `--series "Name"` to
   resolve both the manga ID and the chapters folder in one go.

---

## manga.json library

Lets you upload by series name instead of memorizing manga IDs and paths.

```json
{
    "The JOJOLands":      { "manga_id": 23331, "chapter_dir": "C:\\releases\\The JOJOLands" },
    "3-Gatsu no Lion":    { "manga_id": 581,   "chapter_dir": "C:\\releases\\3-Gatsu no Lion" }
}
```

Then:
```
python mangadot-upload.py --series "The JOJOLands"
```

Series name matching is case-insensitive. You can override either field on the
command line with `--manga <id>` or `--folder <path>`.

---

## Chapter folder format

Each chapter must live in its own folder inside `chapter_dir`. The folder name
encodes the volume, chapter, and (optional) title:

```
V01 Ch001 Departure
V01 Ch002 The Journey Begins
V02 Ch010 A New Arc
V02 Ch010.5 Bonus Story
Ch015 Standalone Chapter      ← no volume is fine too
```

Flexible parsing — these all work:
- `V01 Ch001 Title`
- `Vol.1 Ch.5 Title`
- `v1 ch1 title`
- `Ch001 Title` (no volume)
- `V02 Ch003.5 Half Chapter` (decimal chapter numbers)

Images inside each folder can be `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, or `.avif`.
They're sorted by filename and zipped (STORED, no recompression) in memory —
nothing is written to disk.

---

## Usage

### Basic

```
# Upload everything for a series
python mangadot-upload.py --series "The JOJOLands"

# Preview what would be uploaded — no upload
python mangadot-upload.py --series "The JOJOLands" --dry-run
```

### Selecting chapters

```
# Single chapter
python mangadot-upload.py --series "The JOJOLands" --chapter 36

# Range
python mangadot-upload.py --series "The JOJOLands" --start 1 --end 50

# Range with exclusions
python mangadot-upload.py --series "The JOJOLands" --start 1 --end 50 --exclude 33 34 42.5

# Everything from chapter 42 onward (useful for resuming after a failure)
python mangadot-upload.py --series "The JOJOLands" --start 42
```

### Replacing existing chapters

By default the script fetches your already-uploaded chapters and **skips any
that match** (same manga + same chapter number + same language). To delete
and re-upload them instead:

```
python mangadot-upload.py --series "The JOJOLands" --reupload
python mangadot-upload.py --series "The JOJOLands" --chapter 36 --reupload
```

### Without manga.json

```
# Use the manga_id / chapters_dir from config.ini
python mangadot-upload.py

# Override one or both
python mangadot-upload.py --manga 23331 --folder "C:\path\to\chapters"
```

### Uploading a pre-built zip

If you already have a zip you'd rather upload as-is:

```
python mangadot-upload.py --series "The JOJOLands" --zip "C:\ch36.zip" --chapter 36
python mangadot-upload.py --series "The JOJOLands" --zip "C:\ch36.zip" --chapter 36 --volume 7 --title "Some Title"
```

`--chapter` is required with `--zip`. Volume and title are inferred from the
zip filename if not given (same parsing rules as folder names).

### Other flags

```
--config <path>    Use a different config file (default: config.ini next to the script)
--library <path>   Use a different manga library file (default: manga.json next to the script)
```

---

## Auth methods (pick one in config.ini)

**Email + password** — simplest:
```ini
[auth]
email    = your@email.com
password = yourpassword
```

**Netscape or JSON cookies file** — export from your browser via a cookies extension:
```ini
[auth]
cookies_file = C:\path\to\cookies.txt
```

**Live browser cookies** — pulls directly from an installed browser's cookie store:
```ini
[auth]
browser = firefox   # brave / chrome / chromium / edge / firefox
```
Requires `browser-cookie3` (already in `requirements.txt`). You must be logged
in to mangadot.net in that browser. On Windows this may require running as
Administrator depending on the browser.

**Firefox is recommended.** Chrome and Edge tend to get hit harder by
Cloudflare and the login can fail outright on those browsers.

---

## When Cloudflare blocks the session

If your `cf_clearance` cookie expires mid-run or the API starts returning
Cloudflare challenge pages, the script will pause and prompt you:

1. Log out and back in to mangadot.net in Firefox.
2. Press Enter to retry, or `q` to quit.
3. Cookies are reloaded and the upload resumes from the same chapter.

If you choose to quit, the script tells you exactly which chapter to resume
from:

```
python mangadot-upload.py --series "The JOJOLands" --start 42
```

The TUS upload is also resumable mid-chapter — if a chunk fails, the script
asks the server for the real offset and continues from there.
