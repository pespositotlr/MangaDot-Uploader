"""
auth_strategy.py - Cookie-based auth for mangadot-upload.

Strategy:
  1. Try cache file first (avoids touching the browser if session still valid).
  2. Extract cookies directly from your browser using rookiepy (no Chrome
     automation required — browser can stay open).
  3. Verify session via a plain requests GET to /api/profile.

Requirements:
    py -3.13 -m pip install rookiepy requests
"""

import json
import os
import platform
import re
import subprocess
import sys
import time
from typing import Callable, Optional

import requests


CACHE_MAX_AGE = 30 * 24 * 3600  # 30 days
# Static fallback UAs — only used if real version detection (below) fails.
# NOTE: cf_clearance is bound to the exact User-Agent that solved the
# Cloudflare challenge. If these hardcoded versions drift behind your
# actual installed browser (which auto-updates), verification will fail
# even with a technically-unexpired cf_clearance cookie. Prefer the
# dynamically-detected UA in _browser_ua() below.
BROWSER_UA = {
    "chrome":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "firefox": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0",
    "brave":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "edge":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0",
    "opera":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 OPR/122.0.0.0",
    "vivaldi": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Vivaldi/7.4.3684.38",
}
DEFAULT_UA = BROWSER_UA["chrome"]  # last-resort fallback

_UA_CACHE: dict = {}  # in-process cache so we only touch the registry once


# ---------------------------------------------------------------------------
# Real installed-browser version detection (Windows/macOS/Linux)
# ---------------------------------------------------------------------------

def _clean_version(raw) -> Optional[str]:
    if not raw:
        return None
    m = re.search(r"\d+(?:\.\d+){1,3}", str(raw))
    return m.group(0) if m else None


def _detect_windows_arch() -> str:
    return "Win64; x64" if platform.machine().endswith("64") else "Win32"


def _read_windows_registry(browser: str) -> Optional[str]:
    try:
        import winreg
        paths = {
            "chrome":  [(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Google\Chrome\BLBeacon", "version"),
                        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon", "version")],
            "edge":    [(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{56EB18F8-B008-4CBD-B6D2-8C97FE7E7558}", "pv"),
                        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{56EB18F8-B008-4CBD-B6D2-8C97FE7E7558}", "pv")],
            "brave":   [(winreg.HKEY_CURRENT_USER, r"SOFTWARE\BraveSoftware\Brave-Browser\BLBeacon", "version")],
            "opera":   [(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Opera Software\BLBeacon", "version")],
            "vivaldi": [(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Vivaldi\BLBeacon", "version")],
            "firefox": [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Mozilla\Mozilla Firefox", "CurrentVersion"),
                        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Mozilla\Mozilla Firefox", "CurrentVersion"),
                        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Mozilla\Mozilla Firefox", "CurrentVersion")],
        }
        for hive, path, key in paths.get(browser, []):
            try:
                with winreg.OpenKey(hive, path) as k:
                    val, _ = winreg.QueryValueEx(k, key)
                    v = _clean_version(val)
                    if v:
                        return v
            except OSError:
                continue
    except Exception:
        return None
    return None


def _read_mac_plist(browser: str) -> Optional[str]:
    try:
        import plistlib
        plist_paths = {
            "chrome":  "/Applications/Google Chrome.app/Contents/Info.plist",
            "edge":    "/Applications/Microsoft Edge.app/Contents/Info.plist",
            "brave":   "/Applications/Brave Browser.app/Contents/Info.plist",
            "firefox": "/Applications/Firefox.app/Contents/Info.plist",
            "opera":   "/Applications/Opera.app/Contents/Info.plist",
            "vivaldi": "/Applications/Vivaldi.app/Contents/Info.plist",
        }
        path = plist_paths.get(browser)
        if not path or not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            plist = plistlib.load(f)
            raw = plist.get("KSVersion") or plist.get("CFBundleShortVersionString")
            return _clean_version(raw)
    except Exception:
        return None


def _read_linux_version(browser: str) -> Optional[str]:
    cmds = {
        "chrome":  ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
        "edge":    ["microsoft-edge", "microsoft-edge-stable"],
        "brave":   ["brave-browser", "brave"],
        "firefox": ["firefox", "firefox-esr"],
        "opera":   ["opera"],
        "vivaldi": ["vivaldi", "vivaldi-stable"],
    }
    for cmd in cmds.get(browser, []):
        try:
            out = subprocess.run([cmd, "--version"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=5)
            if out.returncode == 0 and out.stdout:
                return _clean_version(out.stdout)
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _detect_installed_version(browser: str) -> Optional[str]:
    try:
        if sys.platform == "win32":
            return _read_windows_registry(browser)
        elif sys.platform == "darwin":
            return _read_mac_plist(browser)
        else:
            return _read_linux_version(browser)
    except Exception:
        return None


def _build_user_agent(browser: str, version: Optional[str]) -> str:
    if not version:
        return BROWSER_UA.get(browser, DEFAULT_UA)
    own_major = version.split(".")[0]

    if sys.platform == "win32":
        arch_clause = f"Windows NT 10.0; {_detect_windows_arch()}"
        if browser == "firefox":
            return f"Mozilla/5.0 ({arch_clause}; rv:{own_major}.0) Gecko/20100101 Firefox/{own_major}.0"
        if browser == "edge":
            return f"Mozilla/5.0 ({arch_clause}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{own_major}.0.0.0 Safari/537.36 Edg/{own_major}.0.0.0"
        if browser == "opera":
            return f"Mozilla/5.0 ({arch_clause}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{own_major}.0.0.0 Safari/537.36 OPR/{own_major}.0.0.0"
        if browser == "vivaldi":
            return f"Mozilla/5.0 ({arch_clause}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{own_major}.0.0.0 Safari/537.36 Vivaldi/{version}"
        return f"Mozilla/5.0 ({arch_clause}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{own_major}.0.0.0 Safari/537.36"
    elif sys.platform == "darwin":
        if browser == "firefox":
            return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7; rv:{own_major}.0) Gecko/20100101 Firefox/{own_major}.0"
        if browser == "edge":
            return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{own_major}.0.0.0 Safari/537.36 Edg/{own_major}.0.0.0"
        return f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{own_major}.0.0.0 Safari/537.36"
    else:
        if browser == "firefox":
            return f"Mozilla/5.0 (X11; Linux x86_64; rv:{own_major}.0) Gecko/20100101 Firefox/{own_major}.0"
        return f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{own_major}.0.0.0 Safari/537.36"

# Supported rookiepy extractors: (display_name, rookiepy_function_name)
SUPPORTED_BROWSERS = [
    ("Chrome",  "chrome"),
    ("Firefox", "firefox"),
    ("Brave",   "brave"),
    ("Edge",    "edge"),
    ("Opera",   "opera"),
    ("Vivaldi", "vivaldi"),
]


# ---------------------------------------------------------------------------
# rookiepy extraction
# ---------------------------------------------------------------------------

def _find_firefox_profile() -> Optional[str]:
    """
    Return the path to the Firefox profile that actually contains a
    cookies.sqlite, preferring 'default-release' over the bare 'default'
    stub Firefox creates as a migration placeholder.
    """
    import configparser
    firefox_dir = os.path.expandvars(r"%APPDATA%\Mozilla\Firefox")
    profiles_ini = os.path.join(firefox_dir, "profiles.ini")
    if not os.path.isfile(profiles_ini):
        return None

    cfg = configparser.ConfigParser()
    cfg.read(profiles_ini, encoding="utf-8")

    candidates = []
    for section in cfg.sections():
        if not section.startswith("Profile"):
            continue
        path   = cfg.get(section, "Path", fallback="")
        is_rel = cfg.getint(section, "IsRelative", fallback=1)
        if is_rel:
            if not path.startswith("Profiles"):
                full = os.path.join(firefox_dir, "Profiles", path)
            else:
                full = os.path.join(firefox_dir, path)
        else:
            full = path
        if os.path.isfile(os.path.join(full, "cookies.sqlite")):
            candidates.append(full)

    if not candidates:
        return None
    for c in candidates:
        if "default-release" in c:
            return c
    return candidates[0]


def _read_firefox_cookies_direct(domain: str) -> tuple:
    """
    Read Firefox cookies for *domain* directly from the correct profile's
    cookies.sqlite — bypassing rookiepy's broken profile auto-detection.
    Works with Firefox open (copies the db to a temp file first).

    Returns (cookies_dict, cf_clearance_expiry_epoch_or_None).
    """
    import sqlite3, shutil, tempfile
    profile = _find_firefox_profile()
    if not profile:
        raise RuntimeError(
            "Could not locate a Firefox profile with cookies.sqlite. "
            "Make sure Firefox is installed and you have logged in at least once."
        )
    db_path = os.path.join(profile, "cookies.sqlite")
    # Copy to temp file so we can read it while Firefox is open
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        shutil.copy2(db_path, tmp_path)
        conn = sqlite3.connect(tmp_path)
        rows = conn.execute(
            "SELECT name, value, expiry FROM moz_cookies WHERE host LIKE ?",
            (f"%{domain}%",)
        ).fetchall()
        conn.close()
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
    if not rows:
        raise RuntimeError(
            f"No cookies found for {domain!r} in Firefox profile at: {profile}. "
            f"Make sure you are logged in to {domain} in Firefox."
        )
    cookies = {name: value for name, value, _ in rows}
    cf_expiry = None
    for name, _, expiry in rows:
        if name == "cf_clearance":
            cf_expiry = expiry  # Firefox stores this as a Unix epoch (seconds)
            break
    return cookies, cf_expiry


def _browser_ua(browser: str) -> str:
    """
    Return a User-Agent string that matches the ACTUALLY installed browser
    version on this machine, so it lines up with whatever UA was active
    when Cloudflare issued the current cf_clearance cookie. Falls back to
    the static BROWSER_UA dict if detection fails for any reason.
    """
    browser = browser.lower()
    if browser in _UA_CACHE:
        return _UA_CACHE[browser]
    version = _detect_installed_version(browser)
    ua = _build_user_agent(browser, version)
    _UA_CACHE[browser] = ua
    return ua


def _extract_cookies_rookiepy(browser: str, domain: str) -> dict:
    """
    Use rookiepy to pull cookies for *domain* from the given browser.
    Browser can be open — rookiepy reads the profile directly without
    needing an exclusive lock.

    Returns a plain dict {name: value}.
    Raises RuntimeError on failure.
    """
    try:
        import rookiepy
    except ImportError:
        raise RuntimeError(
            "rookiepy is not installed.\n"
            "Run:  py -3.13 -m pip install rookiepy"
        )

    fn = getattr(rookiepy, browser.lower(), None)
    if fn is None:
        supported = ", ".join(name for _, name in SUPPORTED_BROWSERS)
        raise ValueError(
            f"Unsupported browser: {browser!r}. "
            f"Supported: {supported}"
        )

    # For Firefox, bypass rookiepy entirely and read the correct profile's
    # cookies.sqlite directly — rookiepy's auto-detection picks the wrong
    # profile when multiple profiles exist (e.g. default vs default-release).
    if browser.lower() == "firefox":
        cookies, _cf_expiry = _read_firefox_cookies_direct(domain)
        if not cookies:
            raise RuntimeError(
                f"No cookies found for {domain!r} in Firefox. "
                f"Make sure you are logged in to {domain} in Firefox."
            )
        return cookies

    try:
        raw = fn(domains=[domain, f".{domain}"])
    except Exception as e:
        raise RuntimeError(
            f"rookiepy could not read cookies from {browser}: {e}\n"
            f"Make sure you are logged in to {domain} in {browser} "
            f"and the browser profile is accessible."
        ) from e

    if not raw:
        raise RuntimeError(
            f"No cookies found for {domain!r} in {browser}. "
            f"Make sure you are logged in to {domain} in {browser}."
        )

    cookies = {c["name"]: c["value"] for c in raw if c.get("name")}
    print(f"  [debug-raw] rookiepy found {len(raw)} cookies for {domain}:")
    for c in raw:
        name = c.get("name", "?")
        val  = c.get("value", "")
        display = val if len(val) <= 40 else val[:40] + "..."
        print(f"  [debug-raw]   {name} = {display!r}")
    return cookies


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def load_cache(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("saved_at", 0) > CACHE_MAX_AGE:
            return None
        if not data.get("cookies"):
            return None
        return data
    except Exception:
        return None


def save_cache(path: str, cookies: dict, user_agent: str) -> None:
    data = {
        "saved_at":   time.time(),
        "user_agent": user_agent,
        "cookies":    cookies,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _apply_cookies(session: requests.Session, cookies: dict,
                   ua: str, domain: str) -> None:
    session.cookies.clear()
    session.headers["User-Agent"] = ua
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=f".{domain.lstrip('.')}")


# ---------------------------------------------------------------------------
# Session verification
# ---------------------------------------------------------------------------

def verify_session(session: requests.Session, site_url: str,
                   debug: bool = False) -> Optional[dict]:
    """
    Hit /api/profile with plain requests.
    The session must already have the correct User-Agent set to match the
    browser the cf_clearance cookie came from — Cloudflare validates both.
    Returns {"username": "..."} on success, None on failure.
    """
    url = f"{site_url.rstrip('/')}/api/profile"
    try:
        r = session.get(url, timeout=15)
    except Exception as e:
        if debug:
            print(f"  [debug] verify_session request failed: {e}")
        return None

    if debug:
        print(f"  [debug] GET {url} -> HTTP {r.status_code}")
        print(f"  [debug] Response body (first 500 chars):")
        print(f"  [debug]   {r.text[:500]!r}")

    if r.status_code != 200:
        if debug:
            print(f"  [debug] Non-200 status; verification failed")
        return None
    if "Just a moment" in r.text or "challenge-platform" in r.text:
        if debug:
            print(f"  [debug] Cloudflare challenge page detected")
        return None

    try:
        data = r.json()
    except Exception as e:
        if debug:
            print(f"  [debug] JSON parse failed: {e}")
        return None

    # /api/profile returns {"profile": {"email": "...", ...}}
    profile = data.get("profile", {})
    username = (
        profile.get("username")
        or profile.get("email")
        or data.get("username")
        or data.get("email")
        or "unknown"
    )
    return {"username": username}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _cf_clearance_is_fresh(domain: str, browser: str, debug: bool = False) -> bool:
    """
    Check whether the browser's current cf_clearance cookie is still within
    its expiry window. Only implemented for Firefox (direct SQLite read).

    IMPORTANT: MangaDot appears to have moved to Ory Kratos-based sessions
    (cookies like ory_kratos_session / csrf_token_* rather than a Cloudflare
    cf_clearance cookie). If no cf_clearance cookie exists at all, we can't
    conclude the cache is stale from that alone — the site's auth scheme
    may simply no longer use it. In that case we conservatively return True
    and let verify_session() be the actual source of truth, same as we
    already do for non-Firefox browsers.
    """
    if browser.lower() != "firefox":
        return True
    try:
        cookies, cf_expiry = _read_firefox_cookies_direct(domain)
    except Exception:
        return False
    if "cf_clearance" not in cookies:
        # Site may no longer issue this cookie at all — don't treat its
        # absence as staleness. Let verify_session() decide.
        if debug:
            print("  [debug] No cf_clearance cookie found for this domain — "
                  "site may not use Cloudflare's clearance cookie anymore. "
                  "Trying cache anyway; verify_session() will be the judge.")
        return True
    if not cf_expiry:
        return False
    fresh = cf_expiry > time.time() + 60  # 60s safety margin
    if debug:
        remaining = cf_expiry - time.time()
        print(f"  [debug] cf_clearance expiry: {cf_expiry} "
              f"({'fresh, ' + str(int(remaining)) + 's left' if fresh else 'STALE'})")
    return fresh


def ensure_authenticated(
    session: requests.Session,
    *,
    site_url: str,
    api_url: str,            # kept for interface compatibility; not used
    domain: str,
    cache_path: str,
    username: str,           # kept for interface compatibility; not used
    password: str,           # kept for interface compatibility; not used
    browser: str = "chrome",
    force_refresh: bool = False,
    on_refresh: Optional[Callable[[], None]] = None,
    debug: bool = False,
) -> tuple:
    """
    Returns (user_dict, refresher_callable).

    Auth priority:
      1. Cache file — but ONLY if the browser's cf_clearance is still fresh
         (checked via its real expiry timestamp in cookies.sqlite for
         Firefox). A dated cache with an expired cf_clearance is skipped
         automatically rather than being tried and failing.
      2. rookiepy / direct SQLite read — pulls fresh cookies from the
         browser profile (browser can stay open; no automation needed).

    The refresher re-reads from the browser and updates the session + cache.
    Pass debug=True to print cookies found, expiry checks, and HTTP details.
    """

    ua = _browser_ua(browser)

    def _apply_and_verify(cookies: dict, ua: str) -> Optional[dict]:
        _apply_cookies(session, cookies, ua, domain)
        if debug:
            print(f"  [debug] User-Agent being used: {ua!r}")
            print(f"  [debug] Cookies being sent to {domain}:")
            for k, v in cookies.items():
                display = v if len(v) <= 40 else v[:40] + "..."
                print(f"  [debug]   {k} = {display!r}")
        return verify_session(session, site_url, debug=debug)

    def refresher() -> None:
        if on_refresh:
            on_refresh()
        cookies = _extract_cookies_rookiepy(browser, domain)
        save_cache(cache_path, cookies, ua)
        _apply_and_verify(cookies, ua)

    # 1. Try cache first, but only if cf_clearance is still fresh in the
    #    browser right now. A 30-day-old cache with a long-expired
    #    cf_clearance will always 403, so skip straight to a fresh read
    #    instead of wasting a round trip on a doomed verify call.
    if not force_refresh:
        cache = load_cache(cache_path)
        if cache:
            if _cf_clearance_is_fresh(domain, browser, debug=debug):
                user = _apply_and_verify(
                    cache["cookies"], cache.get("user_agent", ua)
                )
                if user:
                    return user, refresher
            elif debug:
                print("  [debug] Skipping cache — cf_clearance is stale; "
                      "re-reading from browser instead.")

    # 2. Extract fresh cookies via rookiepy / direct SQLite read
    if on_refresh:
        on_refresh()

    try:
        cookies = _extract_cookies_rookiepy(browser, domain)
    except Exception as e:
        raise RuntimeError(f"Authentication failed: {e}") from e

    save_cache(cache_path, cookies, ua)
    user = _apply_and_verify(cookies, ua)
    if user:
        return user, refresher

    raise RuntimeError(
        "Authentication failed: cookies were read from the browser but "
        "session verification failed.\n"
        f"Make sure you are logged in to {domain} in your browser and have "
        "passed any Cloudflare challenge (visit the site once manually if needed)."
    )