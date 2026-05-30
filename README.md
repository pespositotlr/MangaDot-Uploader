# mangadot-upload

Folder-based batch uploader for mangadot.net.
Reads your chapter image folders, zips them on-the-fly, and uploads via TUS.

---

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Copy `config.ini.example` to `config.ini` and fill in your details:
   - Your login credentials (or cookies)
   - Your manga's ID (from its URL on mangadot.net)
   - Path to your chapters folder
   - Your scanlator name or group ID

3. (Optional) Copy `manga.json.example` to `manga.json` if you want to use
   `--series` to upload multiple series by name.

---

## Chapter folder format

Chapters must be in individual folders inside your `chapters_dir`.
The folder name tells the uploader the volume, chapter, and title:

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

---

## Usage

```
# Upload everything in chapters_dir
python mangadot-upload.py

# Preview what would be uploaded (no upload)
python mangadot-upload.py --dry-run

# Upload only chapters 5 and up
python mangadot-upload.py --start 5

# Upload only chapters 1 through 10
python mangadot-upload.py --start 1 --end 10

# Use a different config file
python mangadot-upload.py --config other.ini
```

If an upload fails mid-way, it will tell you the chapter number to resume from:
```
python mangadot-upload.py --start 42
```

---

## Auth methods (pick one in config.ini)

**Email + password** — simplest, recommended:
```ini
[auth]
email    = your@email.com
password = yourpassword
```

**Netscape cookies.txt** — export from your browser via a cookies extension:
```ini
[auth]
cookies_file = C:\path\to\cookies.txt
```

**Live browser cookies** — pulls directly from an installed browser's cookie store:
```ini
[auth]
browser = chrome   # brave / chrome / chromium / edge / firefox
```
Note: `browser-cookie3` must be installed, and you must be logged in to mangadot.net
in that browser. On Windows this may require running as Administrator.
