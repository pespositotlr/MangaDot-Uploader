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
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Callable, Optional

import httpx


CF_CLEARANCE_TIMEOUT   = 30  # seconds to wait for the CF interstitial to clear
TURNSTILE_TIMEOUT      = 30  # seconds for invisible Turnstile to write its token
LOGIN_RESPONSE_TIMEOUT = 15  # seconds after submit before we expect access_token


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def _kill_proc_tree(proc: Optional[subprocess.Popen]) -> None:
    """
    Kill a process and its entire child tree, then wait for it to exit.

    On Windows, plain proc.kill() only signals the root process; Chrome
    spawns several child processes that keep the CDP port open even after the
    parent dies.  taskkill /F /T kills the whole job tree atomically.

    On POSIX we kill the process group so forked children are also reaped.
    """
    if proc is None:
        return
    if sys.platform == "win32":
        try:
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    else:
        try:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
    # Wait up to 5 s for the OS to reclaim the port before the next attempt.
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _kill_stale_chrome_by_profile_prefix(prefix: str = "mangadot-nodriver") -> None:
    """
    On Windows, sweep for any chrome.exe processes whose command line
    references a temp profile that matches our prefix.  This cleans up
    leftover processes from previous runs that survived a crash.
    """
    if sys.platform != "win32":
        return
    try:
        # WMIC is available on all supported Windows versions.
        result = subprocess.run(
            [
                "wmic", "process", "where",
                f"name='chrome.exe' and commandline like '%{prefix}%'",
                "get", "processid", "/format:value",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.lower().startswith("processid="):
                pid_str = line.split("=", 1)[1].strip()
                if pid_str.isdigit():
                    subprocess.call(
                        ["taskkill", "/F", "/T", "/PID", pid_str],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Session verification
# ---------------------------------------------------------------------------

def verify_session(session: httpx.Client, api_url: str) -> Optional[dict]:
    """
    Return the user dict on success, None on any failure (no exceptions thrown).

    Tries the Ory Kratos whoami endpoint first, then falls back to the legacy
    /auth/me endpoint in case the site adds it back later.
    """
    site_url = api_url.rstrip("/")
    # Remove trailing /api if present to get the bare site URL
    if site_url.endswith("/api"):
        site_url = site_url[:-4]

    candidates = [
        f"{site_url}/api/.ory/kratos/public/sessions/whoami",
        f"{site_url}/.ory/kratos/public/sessions/whoami",
        f"{api_url}/auth/me",
    ]

    for url in candidates:
        try:
            r = session.get(url, timeout=15)
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        body = r.text
        if "Just a moment" in body or "challenge-platform" in body:
            return None
        try:
            data = r.json()
        except Exception:
            continue

        # Ory Kratos whoami response: {"active": true, "identity": {"traits": {"username": ...}}}
        if "active" in data:
            if not data.get("active"):
                return None
            traits = data.get("identity", {}).get("traits", {})
            username = traits.get("username") or traits.get("email") or "unknown"
            return {"username": username}

        # Legacy /auth/me response: {"authenticated": true, "user": {...}}
        if "authenticated" in data:
            if not data.get("authenticated"):
                return None
            return data.get("user") or {"username": "unknown"}

    return None


# ---------------------------------------------------------------------------
# nodriver-based cookie refresh
# ---------------------------------------------------------------------------

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

    def _pick_free_port() -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    # Kill any leftover Chrome processes from a previous crashed run before
    # we start attempting to launch a new one.
    _kill_stale_chrome_by_profile_prefix("mangadot-nodriver")

    chrome_proc: Optional[subprocess.Popen] = None
    tmp_profile: Optional[str] = None
    free_port:   Optional[int]  = None

    last_error: Optional[str] = None

    for attempt in range(1, 4):
        port    = _pick_free_port()
        profile = tempfile.mkdtemp(prefix="mangadot-nodriver-")
        stderr_log_path = os.path.join(profile, "chrome-stderr.log")

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
        popen_kwargs: dict = {
            "stdin":  subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": stderr_fh,
        }
        if sys.platform == "win32":
            # CREATE_NEW_PROCESS_GROUP lets taskkill /T walk the full tree.
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_BREAKAWAY_FROM_JOB
            )
        else:
            # On POSIX, start_new_session puts Chrome in its own process group
            # so os.killpg can reap all children at once.
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(chrome_args, **popen_kwargs)
        try:
            cdp_ready = False
            for _ in range(90):  # 45 s at 0.5 s intervals
                if proc.poll() is not None:
                    break  # Chrome exited early
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=2
                    ) as resp:
                        resp.read()
                        cdp_ready = True
                        break
                except Exception:
                    time.sleep(0.5)

            if cdp_ready:
                chrome_proc = proc
                tmp_profile  = profile
                free_port    = port
                stderr_fh.close()
                break

            # CDP did not come up — kill the full tree, collect diagnostics.
            stderr_fh.close()
            _kill_proc_tree(proc)
            try:
                with open(stderr_log_path, "rb") as f:
                    tail = f.read()[-800:].decode("utf-8", errors="replace")
            except Exception:
                tail = "<could not read stderr log>"
            exit_code  = proc.poll()
            last_error = (
                f"attempt {attempt}/3: Chrome failed to bring up CDP on port "
                f"{port} within 45s (exit_code={exit_code}). Stderr tail:\n{tail}"
            )
            shutil.rmtree(profile, ignore_errors=True)
            if attempt < 3:
                time.sleep(3)  # give the OS time to fully release the port

        except Exception as exc:
            stderr_fh.close()
            _kill_proc_tree(proc)
            shutil.rmtree(profile, ignore_errors=True)
            last_error = f"attempt {attempt}/3: {exc}"
            if attempt < 3:
                time.sleep(3)

    if chrome_proc is None:
        raise RuntimeError(
            f"All 3 Chrome launch attempts failed. Last error: {last_error}"
        )

    # ------------------------------------------------------------------
    # nodriver async flow
    # ------------------------------------------------------------------
    async def _do() -> tuple[dict, str]:
        # Connect to the already-running Chrome via its CDP port.
        # nodriver does NOT spawn a new browser process here.
        browser = await uc.start(host="127.0.0.1", port=free_port)
        try:
            page = await browser.get(login_url)

            # Wait for Cloudflare interstitial to clear.
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

            # Wait for invisible Turnstile to populate its hidden input.
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

            # Fill and submit the login form.
            # Wait up to 15s for each element to appear rather than selecting once.
            FORM_TIMEOUT = 15

            async def wait_for_selector(pg, selector, timeout=FORM_TIMEOUT):
                for _ in range(timeout):
                    el = await pg.select(selector)
                    if el is not None:
                        return el
                    await asyncio.sleep(1)
                page_title = await pg.evaluate("document.title")
                page_url   = await pg.evaluate("window.location.href")
                raise RuntimeError(
                    f"Login form element '{selector}' not found after {timeout}s. "
                    f"Page: {page_title} — {page_url}"
                )

            async def wait_for_login_button(pg, timeout=FORM_TIMEOUT):
                for _ in range(timeout):
                    btns = await pg.select_all("button[type=submit]")
                    for i, b in enumerate(btns):
                        txt = await pg.evaluate(
                            f"document.querySelectorAll('button[type=submit]')[{i}].textContent.trim()"
                        )
                        if txt == "Log in":
                            return b
                    await asyncio.sleep(1)
                raise RuntimeError(f"'Log in' button not found after {timeout}s")

            user_el = await wait_for_selector(page, "#identifier")
            await user_el.send_keys(username)
            pw_el = await wait_for_selector(page, "#password")
            await pw_el.send_keys(password)
            btn = await wait_for_login_button(page)
            await btn.click()

            # Wait for the ory_kratos_session cookie to appear.
            for _ in range(LOGIN_RESPONSE_TIMEOUT):
                await asyncio.sleep(1)
                cookies = await browser.cookies.get_all()
                if any(c.name == "ory_kratos_session" for c in cookies):
                    break
            else:
                debug_url   = await page.evaluate("window.location.href")
                debug_title = await page.evaluate("document.title")
                me = await page.evaluate(
                    "(async () => { const r = await fetch('/api/.ory/kratos/public/sessions/whoami', "
                    "{credentials: 'include'}); return JSON.stringify("
                    "{s: r.status, b: (await r.text()).slice(0, 200)}); })()",
                    await_promise=True,
                )
                raise RuntimeError(
                    f"Login did not produce ory_kratos_session within "
                    f"{LOGIN_RESPONSE_TIMEOUT}s. "
                    f"Page: {debug_title} — {debug_url}. "
                    f"Diagnostic: {me}"
                )

            cookies    = await browser.cookies.get_all()
            ua         = await page.evaluate("navigator.userAgent")
            # Only keep mangadot.net cookies — filter out Bing/MSA/etc cookies
            # that Chrome picks up from other tabs or redirects.
            cookie_map = {
                c.name: c.value for c in cookies
                if "mangadot" in (c.domain or "")
            }
            return cookie_map, ua

        finally:
            try:
                browser.stop()
            except Exception:
                pass

    try:
        return uc.loop().run_until_complete(_do())
    finally:
        # Kill the full Chrome process tree and remove the temp profile.
        _kill_proc_tree(chrome_proc)
        if tmp_profile:
            shutil.rmtree(tmp_profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

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
    def refresher() -> None:
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