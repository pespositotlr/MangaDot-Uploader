# MangaDot-Uploader

Folder-based batch uploader for mangadot.net.
Reads your chapter image folders, zips them in-memory, and uploads them via
the TUS protocol with resumable upload support.

Features:
- Upload by series name from a JSON library (`--series`) or by raw manga ID / folder
- Auto-skips chapters that are already uploaded (or replace them with `--reupload`)
- Resumable uploads — if a chunk fails, it queries the server's real offset and resumes
- Retry with backoff on transient network/server errors
- **Schedulable**: an `auto` auth mode that caches cookies and refreshes them by
  spawning a real Chrome window via `nodriver` when Cloudflare expires them.
- Four auth methods: `mode=auto` (recommended), email+password (currently blocked
  by Cloudflare), Netscape/JSON cookies file, or live browser cookie extraction.

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
    "My Favorite Manga":      { "manga_id": 22222, "chapter_dir": "C:\\releases\\My Favorite Manga" },
    "My Second Favorite Manga":    { "manga_id": 343444,   "chapter_dir": "C:\\releases\\My Second Favorite Manga" }
}
```

Then:
```
python mangadot-upload.py --series "My Favorite Manga"
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
python mangadot-upload.py --series "My Favorite Manga"

# Preview what would be uploaded — no upload
python mangadot-upload.py --series "My Favorite Manga" --dry-run
```

### Selecting chapters

```
# Single chapter
python mangadot-upload.py --series "My Favorite Manga" --chapter 336

# Range
python mangadot-upload.py --series "My Favorite Manga" --start 1 --end 50

# Range with exclusions
python mangadot-upload.py --series "My Favorite Manga" --start 1 --end 50 --exclude 33 34 42.5

# Everything from chapter 42 onward (useful for resuming after a failure)
python mangadot-upload.py --series "My Favorite Manga" --start 42
```

### Replacing existing chapters

By default the script fetches your already-uploaded chapters and **skips any
that match** (same manga + same chapter number + same language). To delete
and re-upload them instead:

```
python mangadot-upload.py --series "My Favorite Manga" --reupload
python mangadot-upload.py --series "My Favorite Manga" --chapter 36 --reupload
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
python mangadot-upload.py --series "My Favorite Manga" --zip "C:\ch36.zip" --chapter 36
python mangadot-upload.py --series "My Favorite Manga" --zip "C:\ch36.zip" --chapter 36 --volume 7 --title "Some Title"
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

### `mode = auto` (recommended)

The only auth method that survives Cloudflare on this site. Reads cached
cookies from `.auth-cache.json`, verifies them against `/api/auth/me`, and on
failure spawns a real Chrome window via `nodriver` to log in fresh. The new
cookies plus Chrome's User-Agent are written back to the cache.

```ini
[auth]
mode     = auto
username = your_mangadot_username
password = your_password
# cache_file  = .auth-cache.json   (default)
# chrome_path = C:\Program Files\Google\Chrome\Application\chrome.exe
```

Requirements: Chrome installed and `pip install nodriver`. The cache file is
gitignored. Refresh takes ~10s when triggered; warm-start with valid cache
takes <1s.

Force a refresh manually with `--refresh-cookies`.

### `email + password` (currently broken)

Was meant to POST `/api/auth/login` directly with httpx. Cloudflare on
mangadot.net blocks this with the "Just a moment..." JS challenge regardless
of TLS fingerprint. Left in the code for the day CF on this site is relaxed.

```ini
[auth]
email    = your@email.com
password = yourpassword
```

### `cookies_file`

Netscape or JSON cookies exported from your browser via an extension.

```ini
[auth]
cookies_file = C:\path\to\cookies.txt
```

### `browser = firefox`

Pulls cookies directly from an installed browser's cookie store (via
`browser-cookie3`). Requires being logged in to mangadot.net in that browser
already. Firefox is the only one that reliably works through Cloudflare.

```ini
[auth]
browser = firefox   # brave / chrome / chromium / edge / firefox
```

---

## Unattended / programmatic runs

`mode = auto` makes the script callable from another tool or scheduler without
human interaction. When Cloudflare's `cf_clearance` cookie (hours-to-a-day TTL)
expires, the script auto-opens Chrome, completes the login flow, harvests
fresh cookies, and continues.

Requirements for the host environment:

1. **`mode = auto`** in `config.ini` with `username` and `password`.
2. **An interactive desktop session** — Chrome needs a real display to satisfy
   Cloudflare's anti-bot checks (window size, GPU, screen dimensions). If the
   caller is a Windows scheduled task, configure it as "Run only when user is
   logged on". A Session-0 service or remote SSH session will not work.
3. **Chrome installed** and `pip install nodriver` (in `requirements.txt`).

Cost per invocation:

- **Warm cache** (cf_clearance still valid): <1 s overhead.
- **Cold cache** (cf_clearance expired): ~10–15 s, during which a Chrome
  window briefly appears, then closes.
- **Mid-batch JWT refresh** (~every 15 min during long uploads): triggers
  another Chrome refresh.

The script exits non-zero on any unrecoverable error. Stdout/stderr include
ANSI color codes; set `TERM=dumb` or pipe through `strip-ansi` if your caller
captures logs.

---

## When Cloudflare blocks the session (`browser=`/`cookies_file=` modes)

In the legacy `browser=` and `cookies_file=` modes the script falls back to a
manual re-login prompt when CF challenges appear:

1. Log out and back in to mangadot.net in Firefox.
2. Press Enter to retry, or `q` to quit.
3. Cookies are reloaded and the upload resumes from the same chapter.

In `mode = auto` this whole loop is automated — a fresh Chrome refresh runs
silently.

If you do quit, the script tells you exactly which chapter to resume from:

```
python mangadot-upload.py --series "My Favorite Manga" --start 42
```

The TUS upload is also resumable mid-chapter — if a chunk fails, the script
asks the server for the real offset and continues from there.
