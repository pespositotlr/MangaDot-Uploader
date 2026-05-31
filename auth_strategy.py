"""
auth_strategy.py - Resilient auth for mangadot-upload.

Strategy:
  1. Load cookies from local cache file.
  2. Verify with /api/auth/me.
  3. If verification fails (Cloudflare challenge, expired token, etc.) spawn
     a real Chrome window via nodriver, navigate to /login, wait for the CF
     challenge to clear, wait for invisible Turnstile to populate, fill +
     submit the login form, then harvest the fresh cookies + User-Agent.

The cache lives next to the script as `.auth-cache.json` by default. The
captured User-Agent is reapplied to the httpx session so cf_clearance stays
valid (it is fingerprinted against the UA that produced it).

nodriver is imported lazily so users who never need the fallback aren't
forced to install it.
"""

import json
import os
import time
from typing import Callable, Optional

import httpx


CF_CLEARANCE_TIMEOUT   = 30  # seconds to wait for the CF interstitial to clear
TURNSTILE_TIMEOUT      = 30  # seconds for invisible Turnstile to write its token
LOGIN_RESPONSE_TIMEOUT = 15  # seconds after submit before we expect access_token


def load_cache(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or "cookies" not in data:
        return None
    return data


def save_cache(path: str, cookies: dict, user_agent: str) -> None:
    payload = {
        "saved_at":   int(time.time()),
        "user_agent": user_agent,
        "cookies":    cookies,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def apply_cache(session: httpx.Client, cache: dict, domain: str) -> None:
    ua = cache.get("user_agent")
    if ua:
        session.headers["User-Agent"] = ua
    for name, value in cache.get("cookies", {}).items():
        session.cookies.set(name, value, domain=domain)


def verify_session(session: httpx.Client, api_url: str) -> Optional[dict]:
    """
    Return the user dict on success, None on any failure (no exceptions thrown).
    """
    try:
        r = session.get(f"{api_url}/auth/me", timeout=15)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    body = r.text
    if "Just a moment" in body or "challenge-platform" in body:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    if not data.get("authenticated"):
        return None
    return data.get("user") or {"username": "unknown"}


def refresh_via_nodriver(
    site_url: str,
    username: str,
    password: str,
    chrome_path: Optional[str] = None,
) -> tuple[dict, str]:
    """
    Open a real Chrome window, complete the CF + Turnstile + login flow, and
    return (cookies_dict, user_agent).

    Raises RuntimeError with a descriptive message on any failure.
    """
    try:
        import asyncio
        import nodriver as uc
    except ImportError as e:
        raise RuntimeError(
            "nodriver is required for the 'auto' auth mode. "
            "Install with: pip install nodriver"
        ) from e

    login_url = site_url.rstrip("/") + "/login"

    import socket
    import subprocess
    import sys
    import tempfile
    import shutil
    import urllib.request
    import time as _time

    # Locate Chrome
    resolved_chrome = chrome_path
    if not resolved_chrome:
        try:
            from nodriver.core.config import find_chrome_executable
            resolved_chrome = find_chrome_executable()
        except Exception as e:
            raise RuntimeError(
                "Could not find Chrome. Set [auth] chrome_path in config.ini."
            ) from e

    def _pick_free_port():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    chrome_proc = None
    tmp_profile = None
    free_port   = None
    stderr_logs = []

    last_error = None
    for attempt in range(1, 4):
        port = _pick_free_port()
        profile = tempfile.mkdtemp(prefix="mangadot-nodriver-")
        stderr_log_path = profile + os.sep + "chrome-stderr.log"
        stderr_logs.append(stderr_log_path)
        chrome_args = [
            resolved_chrome,
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--no-service-autorun",
            "--homepage=about:blank",
            "--no-pings",
            "--password-store=basic",
            "--disable-infobars",
            "--disable-breakpad",
            "--disable-session-crashed-bubble",
            "--disable-search-engine-choice-screen",
            "--disable-features=IsolateOrigins,site-per-process",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
        ]
        stderr_fh = open(stderr_log_path, "wb")
        popen_kwargs = {
            "stdin":  subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": stderr_fh,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_BREAKAWAY_FROM_JOB
            )
        proc = subprocess.Popen(chrome_args, **popen_kwargs)
        try:
            cdp_ready = False
            for _ in range(90):  # 45s at 0.5s intervals
                if proc.poll() is not None:
                    break  # Chrome exited early
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=2
                    ) as r:
                        r.read()
                        cdp_ready = True
                        break
                except Exception:
                    _time.sleep(0.5)
            if cdp_ready:
                chrome_proc = proc
                tmp_profile = profile
                free_port   = port
                stderr_fh.close()
                break
            # Did not come up: collect diagnostics
            try:
                proc.kill()
            except Exception:
                pass
            stderr_fh.close()
            try:
                with open(stderr_log_path, "rb") as f:
                    tail = f.read()[-800:].decode("utf-8", errors="replace")
            except Exception:
                tail = "<could not read stderr log>"
            exit_code = proc.poll()
            last_error = (
                f"attempt {attempt}/3: Chrome failed to bring up CDP on port "
                f"{port} within 45s (exit_code={exit_code}). Stderr tail:\n{tail}"
            )
            shutil.rmtree(profile, ignore_errors=True)
            if attempt < 3:
                _time.sleep(2)  # brief pause before retry
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            stderr_fh.close()
            shutil.rmtree(profile, ignore_errors=True)
            last_error = f"attempt {attempt}/3: {e}"
            if attempt < 3:
                _time.sleep(2)

    if chrome_proc is None:
        raise RuntimeError(
            f"All 3 Chrome launch attempts failed. Last error: {last_error}"
        )

    async def _do():
        # Connect nodriver to the already-running Chrome via host+port (this
        # uses nodriver's "connect_existing" path; nodriver does NOT spawn a
        # new browser process here).
        browser = await uc.start(host="127.0.0.1", port=free_port)
        try:
            page = await browser.get(login_url)

            for _ in range(CF_CLEARANCE_TIMEOUT):
                await asyncio.sleep(1)
                try:
                    title = await page.evaluate("document.title")
                except Exception:
                    title = None
                if title and "Just a moment" not in title:
                    break
            else:
                raise RuntimeError(
                    f"Cloudflare challenge did not clear after "
                    f"{CF_CLEARANCE_TIMEOUT}s"
                )

            ts_js = (
                "(() => { const el = document.querySelector("
                "'input[name=\"cf-turnstile-response\"]'); "
                "return el ? (el.value || '') : ''; })()"
            )
            for _ in range(TURNSTILE_TIMEOUT):
                await asyncio.sleep(1)
                ts = await page.evaluate(ts_js)
                if ts and len(ts) > 10:
                    break
            else:
                raise RuntimeError(
                    f"Turnstile token did not populate after "
                    f"{TURNSTILE_TIMEOUT}s"
                )

            user_el = await page.select("#username")
            await user_el.send_keys(username)
            pw_el = await page.select("#password")
            await pw_el.send_keys(password)
            btn = await page.select("button[type=submit]")
            await btn.click()

            for _ in range(LOGIN_RESPONSE_TIMEOUT):
                await asyncio.sleep(1)
                cookies = await browser.cookies.get_all()
                if any(c.name == "access_token" for c in cookies):
                    break
            else:
                me = await page.evaluate(
                    "(async () => { const r = await fetch('/api/auth/me', "
                    "{credentials: 'include'}); return JSON.stringify("
                    "{s: r.status, b: (await r.text()).slice(0, 200)}); })()",
                    await_promise=True,
                )
                raise RuntimeError(
                    f"Login did not produce access_token within "
                    f"{LOGIN_RESPONSE_TIMEOUT}s. Diagnostic: {me}"
                )

            cookies = await browser.cookies.get_all()
            ua = await page.evaluate("navigator.userAgent")
            cookie_map = {c.name: c.value for c in cookies}
            return cookie_map, ua
        finally:
            try:
                browser.stop()
            except Exception:
                pass

    try:
        return uc.loop().run_until_complete(_do())
    finally:
        # Clean up the Chrome process we launched and its temp profile.
        try:
            chrome_proc.kill()
        except Exception:
            pass
        shutil.rmtree(tmp_profile, ignore_errors=True)


def ensure_authenticated(
    session: httpx.Client,
    *,
    site_url: str,
    api_url: str,
    domain: str,
    cache_path: str,
    username: str,
    password: str,
    chrome_path: Optional[str] = None,
    force_refresh: bool = False,
    on_refresh: Optional[Callable[[], None]] = None,
) -> tuple[dict, Callable[[], None]]:
    """
    Returns (user_dict, refresher).

    refresher() can be called later to force a fresh nodriver-based login —
    useful when the JWT expires mid-batch and a /auth/refresh isn't available
    (e.g. no refresh_token cookie was issued).
    """
    def refresher():
        if on_refresh:
            on_refresh()
        cookies, ua = refresh_via_nodriver(site_url, username, password, chrome_path)
        save_cache(cache_path, cookies, ua)
        session.cookies.clear()
        apply_cache(session, {"cookies": cookies, "user_agent": ua}, domain)

    cache = None if force_refresh else load_cache(cache_path)
    if cache:
        apply_cache(session, cache, domain)
        user = verify_session(session, api_url)
        if user:
            return user, refresher

    refresher()
    user = verify_session(session, api_url)
    if not user:
        raise RuntimeError(
            "Refreshed cookies still failed verification - the site's login "
            "flow may have changed (form selectors, payload schema, etc.)."
        )
    return user, refresher
