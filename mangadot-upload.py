#!/usr/bin/env python3
"""
mangadot-upload — Folder-based batch uploader for mangadot.net

Reads credentials and settings from config.ini, then zips chapter image folders
on-the-fly and uploads via the TUS protocol with resumable upload support.

Folder naming format: "V01 Ch001 Departure"
  → volume 1, chapter 1, title "Departure"

Usage:
    python mangadot-upload.py --series "My Favorite Manga"
    python mangadot-upload.py --series "My Favorite Manga" --chapter 346
    python mangadot-upload.py --series "My Favorite Manga" --start 1 --end 50 --exclude 333 334
    python mangadot-upload.py --manga 23331 --folder "K:\\path\\to\\chapters"
    python mangadot-upload.py --series "My Favorite Manga" --zip "K:\\path\\to\\ch336.zip" --chapter 336
    python mangadot-upload.py --dry-run --series "My Favorite Manga"
"""

import argparse
import base64
import configparser
import io
import json
import os
import re
import sys
import time
import zipfile

import httpx


# ── Constants ─────────────────────────────────────────────────────────────────

CHUNK_SIZE           = 5 * 1024 * 1024  # 5 MB
MAX_BATCH            = 100
CONCURRENCY_PAUSE    = 0.5
TOKEN_REFRESH_BUFFER = 120              # seconds before expiry to refresh
IMAGE_EXTS           = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
SCRIPT_DIR           = os.path.dirname(os.path.abspath(__file__))
MAX_RETRIES          = 3
RETRY_BACKOFF        = [5, 15, 45]     # seconds between retries


# ── Terminal helpers ───────────────────────────────────────────────────────────

def bold(t):   return f"\033[1m{t}\033[0m"
def dim(t):    return f"\033[2m{t}\033[0m"
def green(t):  return f"\033[32m{t}\033[0m"
def red(t):    return f"\033[31m{t}\033[0m"
def yellow(t): return f"\033[33m{t}\033[0m"
def cyan(t):   return f"\033[36m{t}\033[0m"


def format_size(n):
    if n >= 1 << 30: return f"{n/(1<<30):.1f} GB"
    if n >= 1 << 20: return f"{n/(1<<20):.1f} MB"
    if n >= 1 << 10: return f"{n/(1<<10):.0f} KB"
    return f"{n} B"

def format_speed(bps):
    if bps >= 1 << 20: return f"{bps/(1<<20):.1f} MB/s"
    if bps >= 1 << 10: return f"{bps/(1<<10):.0f} KB/s"
    return f"{bps:.0f} B/s"

def format_time(s):
    s = int(s)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s//60}m{s%60:02d}s"
    return f"{s//3600}h{(s%3600)//60:02d}m"


# ── TUS conflict sentinel ──────────────────────────────────────

class _TusConflict(Exception):
    """Raised when TUS PATCH returns 409 -- carries the real server offset."""
    def __init__(self, offset):
        self.offset = offset


# ── Retry helper ──────────────────────────────────────────────────────────────

def is_retryable(exc):
    """Return True for transient network/server errors worth retrying."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (502, 503, 504, 520, 521, 522, 524)
    return False

def get_tus_offset(session, upload_url):
    """Ask the TUS server how many bytes it has received so far."""
    resp = session.head(upload_url, headers={"Tus-Resumable": "1.0.0"})
    resp.raise_for_status()
    return int(resp.headers.get("Upload-Offset", 0))

def with_retry(fn, label=""):
    """Call fn(), retrying up to MAX_RETRIES times on transient errors."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as e:
            if attempt < MAX_RETRIES and is_retryable(e):
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                print(f"\n  {yellow('Retryable error')} ({attempt+1}/{MAX_RETRIES}){' — ' + label if label else ''}: {e}")
                print(f"  Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


# ── TUS helpers ───────────────────────────────────────────────────────────────

def encode_tus_metadata(d):
    return ",".join(
        f"{k} {base64.b64encode(str(v).encode()).decode()}"
        for k, v in d.items()
    )

def decode_jwt_payload(token):
    part = token.split(".")[1]
    pad  = 4 - len(part) % 4
    if pad != 4:
        part += "=" * pad
    return json.loads(base64.b64decode(part))


# ── Manga library ─────────────────────────────────────────────────────────────

def load_manga_library(path):
    """Load manga.json. Returns {} if file doesn't exist."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def resolve_series(library, name):
    """
    Look up a series name in the library.
    Returns (manga_id, chapter_dir_full_path) or raises ValueError.
    """
    # Exact match first, then case-insensitive
    entry = library.get(name)
    if entry is None:
        for k, v in library.items():
            if k.lower() == name.lower():
                entry = v
                name  = k
                break
    if entry is None:
        available = "\n    ".join(library.keys())
        raise ValueError(
            f"Series {name!r} not found in manga.json.\n"
            f"  Available:\n    {available}"
        )
    manga_id    = entry["manga_id"]
    chapter_dir = entry["chapter_dir"]
    return manga_id, chapter_dir


# ── Chapter folder parsing ────────────────────────────────────────────────────

def parse_chapter_folder(name):
    """
    Parse a folder name into (volume, chapter, title).

    Supported patterns:
        V01 Ch001 Some Title
        Vol.1 Ch.5 Title Here
        v1 ch1 title
        Ch001 Title        (volume = None)
        V02 Ch003.5 Half   (decimal chapters)

    Returns (volume_int_or_None, chapter_float, title_str_or_None)
    Raises ValueError if no chapter number is found.
    """
    vol_match = re.search(r"[Vv](?:ol\.?\s*)?(\d+)", name)
    ch_match  = re.search(r"[Cc]h(?:apter)?\.?\s*(\d+(?:\.\d+)?)", name)

    if not ch_match:
        raise ValueError(f"No chapter number found in: {name!r}")

    volume  = int(vol_match.group(1)) if vol_match else None
    chapter = float(ch_match.group(1))
    after   = name[ch_match.end():].strip(" _-–")
    title   = after if after else None

    return volume, chapter, title


def collect_chapter_folders(chapters_dir, start=None, end=None, exclude=None):
    """
    Walk chapters_dir, parse each subfolder name, filter by start/end/exclude,
    and return a sorted list of chapter dicts.
    """
    if not os.path.isdir(chapters_dir):
        raise FileNotFoundError(f"chapters_dir not found: {chapters_dir}")

    chapters = []
    skipped  = []

    for entry in os.scandir(chapters_dir):
        if not entry.is_dir():
            continue
        try:
            volume, chapter, title = parse_chapter_folder(entry.name)
        except ValueError:
            skipped.append(entry.name)
            continue

        if start is not None and chapter < start:
            continue
        if end is not None and chapter > end:
            continue
        if exclude and chapter in exclude:
            continue

        images = sorted(
            f.path for f in os.scandir(entry.path)
            if f.is_file() and os.path.splitext(f.name)[1].lower() in IMAGE_EXTS
        )
        if not images:
            print(f"  {yellow('SKIP')} (no images): {entry.name}")
            continue

        chapters.append({
            "folder":  entry.path,
            "name":    entry.name,
            "volume":  volume,
            "chapter": chapter,
            "title":   title,
            "images":  images,
        })

    if skipped:
        print(f"  {dim(f'Skipped {len(skipped)} folder(s) with no chapter number')}")

    chapters.sort(key=lambda c: (c["chapter"], c["name"]))
    return chapters


# ── Zip helpers ───────────────────────────────────────────────────────────────

def build_zip_in_memory(image_paths):
    """Zip image files into an in-memory BytesIO (STORED, no double-compression)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for path in image_paths:
            zf.write(path, arcname=os.path.basename(path))
    buf.seek(0)
    return buf, buf.getbuffer().nbytes

def load_zip_file(path):
    """Load an existing zip file into a BytesIO buffer."""
    with open(path, "rb") as f:
        data = f.read()
    buf = io.BytesIO(data)
    buf.seek(0)
    return buf, len(data)


# ── Auth ──────────────────────────────────────────────────────────────────────

class AuthManager:
    def __init__(self, session, site_url, api_url, domain):
        self.session      = session
        self.site_url     = site_url
        self.api_url      = api_url
        self.domain       = domain
        self.access_token = None

    def login(self, email, password):
        resp = self.session.post(
            f"{self.api_url}/auth/login",
            json={"email": email, "password": password},
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Login failed (HTTP {resp.status_code}): {resp.text[:200]}"
            )
        data  = resp.json()
        token = (
            data.get("access_token")
            or data.get("token")
            or self.session.cookies.get("access_token")
        )
        if not token:
            token = resp.cookies.get("access_token")
        if token:
            self.access_token = token
            self.session.cookies.set("access_token", token, domain=self.domain)

    def load_from_file(self, path):
        with open(path, encoding="utf-8") as f:
            raw = f.read()

        stripped = raw.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            entries = json.loads(raw)
            if isinstance(entries, dict):
                entries = [entries]
            for c in entries:
                name  = c.get("name", "")
                value = c.get("value", "")
                dom   = c.get("domain", self.domain).lstrip(".")
                cpath = c.get("path", "/")
                if not name:
                    continue
                self.session.cookies.set(name, value, domain=dom, path=cpath)
                if name == "access_token":
                    self.access_token = value
            return

        # Netscape format
        for line in raw.splitlines():
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            dom, _flag, cpath, _sec, _exp, name, value = parts[:7]
            self.session.cookies.set(
                name, value,
                domain=(dom or self.domain).lstrip("."),
                path=cpath or "/",
            )
            if name == "access_token":
                self.access_token = value

    def load_from_browser(self, browser):
        try:
            import browser_cookie3
        except ImportError:
            raise RuntimeError(
                "browser-cookie3 is not installed. Run: pip install browser-cookie3"
            )
        fn = getattr(browser_cookie3, browser.lower(), None)
        if fn is None:
            raise ValueError(
                f"Unsupported browser: {browser}. "
                "Supported: brave, chrome, chromium, edge, firefox"
            )
        try:
            cj = fn(domain_name=self.domain)
        except Exception as e:
            raise RuntimeError(
                f"Could not read cookies from {browser}: {e}\n"
                f"Make sure you are logged in to {self.domain} in {browser}."
            ) from e
        for c in cj:
            # Strip leading dot that browser-cookie3 often includes
            cookie_domain = (c.domain or self.domain).lstrip(".")
            self.session.cookies.set(
                c.name, c.value,
                domain=cookie_domain,
                path=c.path or "/",
            )
            if c.name == "access_token":
                self.access_token = c.value

    def ensure_valid_token(self):
        if not self.access_token:
            return
        try:
            payload   = decode_jwt_payload(self.access_token)
            remaining = payload.get("exp", 0) - time.time()
            if remaining < TOKEN_REFRESH_BUFFER:
                self._refresh()
        except Exception:
            pass

    def _refresh(self):
        print(f"  {dim('[auth] Refreshing token...')}", end=" ", flush=True)
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self.session.post(f"{self.api_url}/auth/refresh")
                if resp.status_code != 200:
                    print(yellow("skipped (failed)"))
                    return
                token = resp.cookies.get("access_token")
                if token:
                    self.access_token = token
                    self.session.cookies.set("access_token", token, domain=self.domain)
                    try:
                        rem = decode_jwt_payload(token).get("exp", 0) - time.time()
                        print(dim(f"ok ({int(rem)}s remaining)"))
                    except Exception:
                        print(dim("ok"))
                    return
                print(yellow("skipped (no new token)"))
                return
            except Exception as e:
                if attempt < MAX_RETRIES and is_retryable(e):
                    wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    print(yellow(f"timeout, retrying in {wait}s..."), end=" ", flush=True)
                    time.sleep(wait)
                else:
                    print(yellow(f"skipped ({e})"))
                    return


# ── TUS upload ────────────────────────────────────────────────────────────────

def upload_buffer_tus(session, api_url, site_url,
                      buf, file_size,
                      chapter, volume, title,
                      batch_id, manga_id, language,
                      group_id, upload_type, scanlator_name):
    """Upload an in-memory buffer via TUS, with per-chunk retry."""

    metadata = {
        "manga_id":       str(manga_id),
        "chapter_number": str(chapter),
        "language":       language,
        "group_id":       str(group_id),
        "upload_type":    upload_type,
        "batch_id":       batch_id,
    }
    if scanlator_name:
        metadata["scanlator_name"] = scanlator_name
    if volume is not None:
        metadata["volume_number"] = str(volume)
    if title:
        metadata["chapter_title"] = title

    headers = {
        "Tus-Resumable":   "1.0.0",
        "Upload-Length":   str(file_size),
        "Upload-Metadata": encode_tus_metadata(metadata),
        "Content-Type":    "application/offset+octet-stream",
    }

    def do_create():
        r = session.post(f"{api_url}/tus/", headers=headers, content=b"")
        r.raise_for_status()
        return r

    create_resp = with_retry(do_create, "TUS create")

    upload_url = create_resp.headers.get("Location")
    if not upload_url:
        raise RuntimeError("No Location header in TUS creation response")
    if upload_url.startswith("/"):
        upload_url = f"{site_url}{upload_url}"
    elif not upload_url.startswith("http"):
        upload_url = f"{api_url}/tus/{upload_url}"

    offset     = 0
    start_time = time.time()

    while offset < file_size:
        buf.seek(offset)
        chunk     = buf.read(CHUNK_SIZE)
        chunk_len = len(chunk)

        patch_headers = {
            "Tus-Resumable":  "1.0.0",
            "Upload-Offset":  str(offset),
            "Content-Type":   "application/offset+octet-stream",
            "Content-Length": str(chunk_len),
        }

        def do_patch(ch=chunk, ph=patch_headers):
            r = session.patch(upload_url, headers=ph, content=ch)
            if r.status_code == 409:
                # Server already has data past this offset — query real offset and resume
                real_offset = get_tus_offset(session, upload_url)
                raise _TusConflict(real_offset)
            r.raise_for_status()
            return r

        try:
            patch_resp = with_retry(do_patch, f"TUS patch offset={offset}")
            new_offset = patch_resp.headers.get("Upload-Offset")
            offset     = int(new_offset) if new_offset else offset + chunk_len
        except _TusConflict as e:
            offset = e.offset
            continue
        except Exception:
            # After any server/network error, query real offset to avoid 409 on retry
            try:
                real = get_tus_offset(session, upload_url)
                offset = real
                print()
                print(f'    Resuming from server offset: {format_size(offset)}')
            except Exception:
                pass
            raise
            raise

        pct       = min(100, int(offset / file_size * 100))
        elapsed   = time.time() - start_time
        speed     = offset / elapsed if elapsed > 0 else 0
        remaining = (file_size - offset) / speed if speed > 0 else 0

        bar_width = 30
        filled    = int(bar_width * offset / file_size)
        bar       = "█" * filled + "░" * (bar_width - filled)

        print(
            f"    {bar} {pct:3d}%  "
            f"{format_size(offset)}/{format_size(file_size)}  "
            f"{format_speed(speed)}  "
            f"ETA {format_time(remaining)}  ",
            end="\r",
        )

    elapsed_total = time.time() - start_time
    avg_speed     = file_size / elapsed_total if elapsed_total > 0 else 0
    print(
        f"    {'█'*30} 100%  "
        f"{format_size(file_size)}  "
        f"avg {format_speed(avg_speed)}  "
        f"{format_time(elapsed_total)}  "
    )


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    return cfg


# ── Duplicate handling ────────────────────────────────────────────────────────

def fetch_existing_uploads(session, api_url, manga_id, language):
    existing = {}
    page = 1
    while True:
        resp = session.get(f"{api_url}/uploads/mine", params={"page": page, "limit": 100})
        if resp.status_code != 200:
            return existing
        data    = resp.json()
        uploads = data.get("uploads", [])
        for u in uploads:
            if u.get("manga_id") != manga_id:
                continue
            if u.get("language") != language:
                continue
            ch_num = u.get("chapter_number")
            if ch_num is not None:
                existing[float(ch_num)] = u
        pagination = data.get("pagination", {})
        if page >= pagination.get("total_pages", 1):
            break
        page += 1
    return existing

def delete_upload(session, api_url, upload_id):
    resp = session.delete(f"{api_url}/uploads/{upload_id}")
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Delete returned success=false: {data}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Folder-based batch uploader for mangadot.net",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python mangadot-upload.py --series \"The JOJOLands\"\n"
            "  python mangadot-upload.py --series \"The JOJOLands\" --chapter 36\n"
            "  python mangadot-upload.py --series \"The JOJOLands\" --start 1 --end 50 --exclude 33 34\n"
            "  python mangadot-upload.py --series \"The JOJOLands\" --reupload\n"
            "  python mangadot-upload.py --manga 23331 --folder \"K:\\\\path\\\\to\\\\chapters\"\n"
            "  python mangadot-upload.py --series \"The JOJOLands\" --zip \"K:\\\\ch36.zip\" --chapter 36\n"
            "  python mangadot-upload.py --dry-run --series \"The JOJOLands\"\n"
        ),
    )
    parser.add_argument("--config",  default=os.path.join(SCRIPT_DIR, "config.ini"),
                        help="Path to config file")
    parser.add_argument("--library", default=os.path.join(SCRIPT_DIR, "manga.json"),
                        help="Path to manga library JSON (default: manga.json next to script)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be uploaded without uploading")
    parser.add_argument("--series",  type=str, default=None,
                        help="Series name from manga.json, e.g. --series \"The JOJOLands\"")
    parser.add_argument("--manga",   type=int, default=None,
                        help="Manga ID, overrides config and --series")
    parser.add_argument("--folder",  type=str, default=None,
                        help="Chapter folders directory, overrides config and --series")
    parser.add_argument("--zip",     type=str, default=None,
                        help="Upload a single existing zip file instead of a folder")
    parser.add_argument("--chapter", type=float, default=None,
                        help="Single chapter number (required when using --zip)")
    parser.add_argument("--volume",  type=int, default=None,
                        help="Volume number (optional, used with --zip)")
    parser.add_argument("--title",   type=str, default=None,
                        help="Chapter title (optional, used with --zip)")
    parser.add_argument("--start",   type=float, default=None,
                        help="Start from this chapter number (inclusive)")
    parser.add_argument("--end",     type=float, default=None,
                        help="End at this chapter number (inclusive)")
    parser.add_argument("--exclude", type=float, nargs="+", default=[],
                        metavar="CH",
                        help="Skip specific chapters, e.g. --exclude 36 37 42.5")
    parser.add_argument("--reupload", action="store_true",
                        help="Delete and re-upload chapters that already exist")
    parser.add_argument("--refresh-cookies", action="store_true",
                        help="Force a fresh nodriver login (auto mode only); "
                             "ignore cached cookies")
    args = parser.parse_args()

    # --chapter doubles as --start/--end when not using --zip
    if args.chapter is not None and args.zip is None:
        args.start = args.chapter
        args.end   = args.chapter

    # ── Load config ──
    try:
        cfg = load_config(args.config)
    except FileNotFoundError as e:
        print(f"\n  {red('Error:')} {e}")
        sys.exit(1)

    def get(section, key, fallback=None):
        try:
            v = cfg.get(section, key).strip()
            return v if v else fallback
        except (configparser.NoSectionError, configparser.NoOptionError):
            return fallback

    site_url = get("site", "url", "https://mangadot.net").rstrip("/")
    api_url  = f"{site_url}/api"
    domain   = re.sub(r"https?://", "", site_url).split("/")[0]

    language       = get("upload", "language", "en")
    upload_type    = get("upload", "upload_type", "chapter")
    group_id       = int(get("upload", "group_id", "0"))
    scanlator_name = get("upload", "scanlator_name", "")
    base_dir       = get("paths", "base_dir", "")

    # ── Resolve manga ID and chapter dir ──
    library    = load_manga_library(args.library)
    manga_id   = None
    chapters_dir = None

    if args.series:
        try:
            manga_id, chapters_dir = resolve_series(library, args.series)
        except ValueError as e:
            print(f"\n  {red('Error:')} {e}")
            sys.exit(1)

    # --manga and --folder override --series
    if args.manga is not None:
        manga_id = args.manga
    if args.folder is not None:
        chapters_dir = args.folder

    # Fall back to config values
    if manga_id is None:
        manga_id_str = get("upload", "manga_id")
        if not manga_id_str or manga_id_str == "12345":
            print(f"\n  {red('Error:')} Provide --series, --manga, or set manga_id in config.ini")
            sys.exit(1)
        manga_id = int(manga_id_str)

    if chapters_dir is None and args.zip is None:
        chapters_dir = get("paths", "chapters_dir", "")
        if not chapters_dir:
            print(f"\n  {red('Error:')} Provide --series, --folder, --zip, or set chapters_dir in config.ini")
            sys.exit(1)

    # ── Banner ──
    print()
    print(bold("╔══════════════════════════════════════════════════════╗"))
    print(bold("║         mangadot.net  ·  Chapter Uploader           ║"))
    print(bold("╚══════════════════════════════════════════════════════╝"))
    print()

    # ── Build chapter list ──
    if args.zip:
        # Single zip mode
        zip_path = os.path.expandvars(args.zip)
        if not os.path.isfile(zip_path):
            print(f"  {red('Error:')} zip file not found: {zip_path}")
            sys.exit(1)
        if args.chapter is None:
            print(f"  {red('Error:')} --chapter is required when using --zip")
            sys.exit(1)
        # Try to infer volume/title from zip filename if not provided
        zip_name   = os.path.splitext(os.path.basename(zip_path))[0]
        vol, ch_inferred, title_inferred = None, args.chapter, None
        try:
            vol, ch_inferred, title_inferred = parse_chapter_folder(zip_name)
        except ValueError:
            pass
        chapters = [{
            "folder":  zip_path,
            "name":    os.path.basename(zip_path),
            "volume":  args.volume if args.volume is not None else vol,
            "chapter": args.chapter,
            "title":   args.title if args.title is not None else title_inferred,
            "images":  [],
            "zip":     zip_path,
        }]
        print(f"  Zip:      {dim(zip_path)}")
    else:
        # Folder scan mode
        print(f"  Scanning: {dim(chapters_dir)}")
        try:
            chapters = collect_chapter_folders(
                chapters_dir, args.start, args.end, set(args.exclude)
            )
        except FileNotFoundError as e:
            print(f"  {red(str(e))}")
            sys.exit(1)

        if args.exclude:
            labels = ", ".join(
                str(int(c) if c == int(c) else c) for c in sorted(args.exclude)
            )
            print(f"  {dim('Excluding chapters: ' + labels)}")

        if not chapters:
            print(f"  {red('No chapter folders matched.')}")
            sys.exit(1)

    # ── Print plan ──
    print()
    print(bold("── Upload plan ────────────────────────────────────────"))
    if args.series:
        print(f"  Series:  {bold(args.series)}")
    print(f"  Manga ID: {manga_id}")
    print()

    total_images = sum(len(c["images"]) for c in chapters)
    for ch in chapters:
        vol_str = f"v{int(ch['volume']):02d}" if ch["volume"] is not None else "   "
        ch_str  = f"ch{ch['chapter']:03g}"
        title   = ch["title"] or ""
        if "zip" in ch:
            detail = dim(f"(zip: {os.path.basename(ch['zip'])})")
        else:
            detail = dim(f"{len(ch['images'])} image{'s' if len(ch['images']) != 1 else ''}")
        print(f"  {cyan(vol_str)} {bold(ch_str)}  {title:<35} {detail}")

    print()
    if not args.zip:
        print(f"  Total: {bold(str(len(chapters)))} chapters, {bold(str(total_images))} images")
        if len(chapters) > 1:
            ch_range = f"ch{chapters[0]['chapter']:g} – ch{chapters[-1]['chapter']:g}"
            print(f"  Range: {dim(ch_range)}")
    print()

    if args.dry_run:
        print(dim("  (dry run — nothing uploaded)"))
        return

    # ── Auth ──
    user_agent = get(
        "auth", "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36",
    )
    session = httpx.Client(
        headers={
            "User-Agent": user_agent,
            "Origin":     site_url,
            "Referer":    f"{site_url}/upload",
        },
        follow_redirects=True,
        timeout=120.0,
    )
    auth = AuthManager(session, site_url, api_url, domain)

    mode         = get("auth", "mode")
    email        = get("auth", "email")
    password     = get("auth", "password")
    cookies_file = get("auth", "cookies_file")
    browser      = get("auth", "browser")

    if mode == "auto":
        username_v = get("auth", "username")
        password_v = get("auth", "password")
        if not (username_v and password_v):
            print(f"  {red('Error:')} mode=auto requires username and password under [auth]")
            sys.exit(1)
        cache_file  = get("auth", "cache_file") or os.path.join(SCRIPT_DIR, ".auth-cache.json")
        chrome_path = get("auth", "chrome_path")
        try:
            from auth_strategy import ensure_authenticated
        except ImportError:
            print(f"  {red('Error:')} auth_strategy.py not found alongside this script")
            sys.exit(1)

        print(f"  Authenticating as {bold(username_v)}...", end=" ", flush=True)
        try:
            user_info, refresher = ensure_authenticated(
                session,
                site_url=site_url,
                api_url=api_url,
                domain=domain,
                cache_path=cache_file,
                username=username_v,
                password=password_v,
                chrome_path=chrome_path,
                force_refresh=args.refresh_cookies,
                on_refresh=lambda: print("\n  " + yellow("Cache stale - opening Chrome to refresh cookies...")),
            )
        except RuntimeError as e:
            print(red("FAILED"))
            print(f"  {e}")
            sys.exit(1)
        print(green("OK") + f" - logged in as {bold(user_info.get('username', '?'))}")

        # Sync access_token so AuthManager's JWT-expiry check sees it.
        auth.access_token = session.cookies.get("access_token")

        # Form-login doesn't issue a refresh_token cookie, so replace the
        # AuthManager's refresh callback with a full nodriver re-login.
        def _auto_refresh():
            print(f"  {dim('[auth] JWT near expiry; refreshing via Chrome...')}")
            try:
                refresher()
                auth.access_token = session.cookies.get("access_token")
                print(f"  {dim('[auth] refreshed OK')}")
            except Exception as e:
                print(f"  {yellow('[auth] refresh failed:')} {e}")
        auth._refresh = _auto_refresh

    elif email and password:
        print(f"  Logging in as {bold(email)}...", end=" ", flush=True)
        try:
            auth.login(email, password)
            print(green("OK"))
        except RuntimeError as e:
            print(red("FAILED"))
            print(f"  {e}")
            sys.exit(1)
    elif cookies_file:
        cookies_file = os.path.expandvars(os.path.expanduser(cookies_file))
        if not os.path.exists(cookies_file):
            print(f"  {red('Cookies file not found:')} {cookies_file}")
            sys.exit(1)
        print(f"  Loading cookies from {dim(cookies_file)}")
        auth.load_from_file(cookies_file)
    elif browser:
        print(f"  Extracting cookies from {bold(browser)}...", end=" ", flush=True)
        try:
            auth.load_from_browser(browser)
            print(green("OK"))
        except (ValueError, RuntimeError) as e:
            print(red("FAILED"))
            print(f"  {e}")
            sys.exit(1)
    else:
        print(f"  {red('No auth method configured in config.ini.')}")
        print("  Set mode=auto (+ username/password), email+password, cookies_file, or browser under [auth].")
        sys.exit(1)

    # ── Verify auth (with re-login prompt on Cloudflare block) ──
    def verify_session():
        """Check /auth/me. On Cloudflare challenge, prompt user to re-login in
        Firefox then reload cookies and retry. Returns username on success."""
        while True:
            print("  Verifying session...", end=" ", flush=True)
            resp = session.get(f"{api_url}/auth/me")
            if resp.status_code == 200:
                return resp.json().get("user", {}).get("username", "unknown")
            print(red("FAILED"))
            is_cf = "Just a moment" in resp.text or "challenge-platform" in resp.text
            if is_cf:
                print()
                print(f"  {yellow('Cloudflare challenge — your cf_clearance cookie has expired.')}")
                print(f"  {bold('1.')} Go to Firefox and log out of mangadot.net, then log back in.")
                print(f"  {bold('2.')} Close ALL other Firefox windows/tabs if cookie extraction keeps failing.")
                print(f"  {bold('3.')} Press Enter here when done, or type \'q\' to quit.")
                choice = input("  > ").strip().lower()
                if choice == "q":
                    sys.exit(0)
                # Reload cookies from Firefox
                print("  Reloading cookies from firefox...", end=" ", flush=True)
                session.cookies.clear()
                try:
                    auth.load_from_browser("firefox")
                    print(green("OK"))
                except Exception as e:
                    print(red(f"FAILED: {e}"))
                    print("  Try again or press q to quit.")
            else:
                print(f"  HTTP {resp.status_code}: {resp.text[:500]}")
                sys.exit(1)

    if mode == "auto":
        # ensure_authenticated() already verified; reuse the username from there
        username = user_info.get("username", "unknown")
    else:
        username = verify_session()
        print(green("OK") + f" — logged in as {bold(username)}")

    # ── Check for existing uploads ──
    existing_uploads = fetch_existing_uploads(session, api_url, manga_id, language)
    if args.reupload:
        matched = [c for c in chapters if c["chapter"] in existing_uploads]
        if matched:
            print(f"  {yellow(str(len(matched)) + ' existing chapter(s) will be replaced')}")
        else:
            print(f"  {dim('No existing chapters found — uploading fresh')}")
    else:
        dupes = [c for c in chapters if c["chapter"] in existing_uploads]
        if dupes:
            dupe_nums = ", ".join(f"ch{c['chapter']:g}" for c in dupes)
            print(f"  {yellow('Warning:')} {len(dupes)} chapter(s) already exist and will be skipped: {dim(dupe_nums)}")
            print(f"  {dim('Use --reupload to replace them instead.')}")
            chapters = [c for c in chapters if c["chapter"] not in existing_uploads]
        if not chapters:
            print(f"  {red('All chapters already uploaded. Nothing to do.')}")
            print(f"  {dim('Use --reupload to replace existing chapters.')}")
            sys.exit(0)

    # ── Upload settings summary ──
    print()
    print(bold("── Upload settings ────────────────────────────────────"))
    print(f"  Manga ID:    {manga_id}")
    print(f"  Language:    {language}")
    print(f"  Type:        {upload_type}")
    if scanlator_name:
        print(f"  Scanlator:   {scanlator_name}")
    else:
        print(f"  Group ID:    {group_id}")
    print()

    # ── Upload in batches ──
    uploaded_bytes = 0
    completed      = 0
    overall_start  = time.time()
    batches        = [chapters[i:i + MAX_BATCH] for i in range(0, len(chapters), MAX_BATCH)]

    for batch_idx, batch in enumerate(batches, 1):
        print(f"{'=' * 60}")
        print(f"  Batch {batch_idx}/{len(batches)}  ({len(batch)} chapters)")
        print(f"{'=' * 60}")

        chapters_info = []
        for ch in batch:
            entry = {"chapter_number": ch["chapter"]}
            if ch["volume"] is not None:
                entry["volume_number"] = ch["volume"]
            if ch["title"]:
                entry["chapter_title"] = ch["title"]
            chapters_info.append(entry)

        # Delete existing chapters if --reupload
        if args.reupload:
            to_delete = [
                (ch, existing_uploads[ch["chapter"]])
                for ch in batch if ch["chapter"] in existing_uploads
            ]
            if to_delete:
                print(f"  Deleting {len(to_delete)} existing chapter(s)...", end=" ", flush=True)
                failed = []
                for ch, existing in to_delete:
                    try:
                        delete_upload(session, api_url, existing["id"])
                    except Exception as e:
                        failed.append(f"ch{ch['chapter']:g}: {e}")
                if failed:
                    print(red("FAILED"))
                    for msg in failed:
                        print(f"    {msg}")
                    sys.exit(1)
                print(green("OK"))

        auth.ensure_valid_token()
        print("  Initializing batch...", end=" ", flush=True)
        payload = {
            "manga_id": manga_id,
            "language": language,
            "group_id": group_id,
            "type":     upload_type,
            "chapters": chapters_info,
        }
        if scanlator_name:
            payload["scanlator_name"] = scanlator_name

        def do_batch_init():
            r = session.post(f"{api_url}/uploads/batch/init", json=payload)
            r.raise_for_status()
            return r

        resp = with_retry(do_batch_init, "batch init")
        data = resp.json()
        if not data.get("success"):
            print(red("FAILED"))
            print(f"  {data}")
            sys.exit(1)
        batch_id = data["batch_id"]
        print(green("OK") + f"  {dim('batch_id: ' + batch_id)}\n")

        for ch in batch:
            completed_label = f"[{completed+1}/{len(chapters)}]"
            vol_str  = f"v{int(ch['volume']):02d}" if ch["volume"] is not None else "v??"
            ch_str   = f"ch{ch['chapter']:03g}"
            title    = ch["title"] or ""

            print(f"  {completed_label} {cyan(vol_str)} {bold(ch_str)}  {title}")

            if "zip" in ch:
                # Load existing zip from disk
                print(f"    Loading zip...", end=" ", flush=True)
                try:
                    buf, file_size = load_zip_file(ch["zip"])
                except Exception as e:
                    print(red("FAILED"))
                    print(f"    {e}")
                    sys.exit(1)
                print(dim(f"{format_size(file_size)}"))
            else:
                # Build zip from images
                n_images = len(ch["images"])
                print(f"    Zipping {n_images} images...", end=" ", flush=True)
                try:
                    buf, file_size = build_zip_in_memory(ch["images"])
                except Exception as e:
                    print(red("FAILED"))
                    print(f"    {e}")
                    sys.exit(1)
                print(dim(f"{format_size(file_size)}"))

            auth.ensure_valid_token()
            for cf_retry in range(3):
                try:
                    upload_buffer_tus(
                        session, api_url, site_url,
                        buf, file_size,
                        ch["chapter"], ch["volume"], ch["title"],
                        batch_id, manga_id, language,
                        group_id, upload_type, scanlator_name,
                    )
                    break
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 403 and (
                        "Just a moment" in e.response.text or
                        "challenge-platform" in e.response.text or
                        cf_retry < 2
                    ):
                        print(f"\n  {yellow('Cloudflare block detected mid-upload.')}")
                        if mode == "auto":
                            print(f"  {dim('Refreshing cookies via Chrome (auto mode)...')}")
                            try:
                                refresher()
                                auth.access_token = session.cookies.get("access_token")
                                print(f"  {dim('Cookies refreshed; retrying.')}")
                            except Exception as reload_err:
                                ch_num = ch["chapter"]
                                print(f"  {red('Auto-refresh failed:')} {reload_err}")
                                print(f"  Resume with: {bold(f'--start {ch_num}')}")
                                sys.exit(1)
                        else:
                            print(f"  {bold('1.')} Log out and back in to mangadot.net in Firefox.")
                            print(f"  {bold('2.')} Press Enter to retry this chapter, or type \'q\' to quit.")
                            ch_num = ch["chapter"]
                            print(f"  (If you quit, resume with: {bold(f'--start {ch_num}')})")
                            choice = input("  > ").strip().lower()
                            if choice == "q":
                                sys.exit(0)
                            print("  Reloading cookies...", end=" ", flush=True)
                            session.cookies.clear()
                            try:
                                auth.load_from_browser("firefox")
                                print(green("OK"))
                            except Exception as reload_err:
                                print(red(f"FAILED: {reload_err}"))
                        buf.seek(0)  # reset buffer for retry
                    else:
                        ch_num = ch["chapter"]
                        print(f"\n  {red('Upload failed:')} {e}")
                        print(f"  Resume with: {bold(f'--start {ch_num}')}")
                        sys.exit(1)
                except Exception as e:
                    ch_num = ch["chapter"]
                    print(f"\n  {red('Upload failed:')} {e}")
                    print(f"  Resume with: {bold(f'--start {ch_num}')}")
                    sys.exit(1)

            uploaded_bytes += file_size
            completed      += 1
            time.sleep(CONCURRENCY_PAUSE)

        auth.ensure_valid_token()
        print(f"\n  Finalizing batch {batch_idx}...", end=" ", flush=True)

        def do_finalize():
            r = session.post(f"{api_url}/uploads/batch/{batch_id}/complete")
            r.raise_for_status()
            return r

        with_retry(do_finalize, "batch finalize")
        print(green("Done!\n"))

    elapsed = time.time() - overall_start
    avg_spd = uploaded_bytes / elapsed if elapsed > 0 else 0
    print(f"{'=' * 60}")
    print(f"  {green('All done!')}  {len(chapters)} chapter{'s' if len(chapters) != 1 else ''} uploaded")
    print(f"  {format_size(uploaded_bytes)} in {format_time(elapsed)}  (avg {format_speed(avg_spd)})")
    print(f"{'=' * 60}\n")

    # Look up the new chapter IDs and emit FINAL_URL lines for orchestrators
    # that parse stdout (matches Mangadex-Scheduled-Uploader's convention).
    # ?source=user works around a current mangadot.net routing bug where the
    # bare /chapter/{id} URL doesn't resolve correctly.
    try:
        post_upload = fetch_existing_uploads(session, api_url, manga_id, language)
        for ch in chapters:
            entry = post_upload.get(ch["chapter"])
            if entry and entry.get("id"):
                print(f"FINAL_URL: {site_url}/chapter/{entry['id']}?source=user")
    except Exception as e:
        print(dim(f"  (could not resolve FINAL_URL: {e})"))


if __name__ == "__main__":
    main()