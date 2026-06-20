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
import time
from typing import Callable, Optional

import requests


CACHE_MAX_AGE = 30 * 24 * 3600  # 30 days
# Match the UA to whichever browser's cf_clearance we're using.
# Cloudflare validates cf_clearance against both TLS fingerprint AND User-Agent.
BROWSER_UA = {
    "chrome":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "firefox": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0",
    "brave":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "edge":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0",
    "opera":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 OPR/122.0.0.0",
    "vivaldi": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Vivaldi/7.4.3684.38",
}
DEFAULT_UA = BROWSER_UA["chrome"]  # fallback

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
    """Return the correct User-Agent string for the given browser."""
    return BROWSER_UA.get(browser.lower(), DEFAULT_UA)


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
    its expiry window. Only implemented for Firefox (direct SQLite read);
    for other browsers we can't cheaply check expiry without rookiepy
    support, so we conservatively return True and let verify_session()
    be the source of truth.
    """
    if browser.lower() != "firefox":
        return True
    try:
        _, cf_expiry = _read_firefox_cookies_direct(domain)
    except Exception:
        return False
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