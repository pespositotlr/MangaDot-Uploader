r"""Keep MangaDot logged in for the uploader, so releases work when nobody is at the PC.

mangadot-upload.py runs this before every upload (no scheduled task needed). Each run:
  1. Asks MangaDot how long the current login has left (the uploader's cache first, then Firefox's cookies).
  2. If more than RENEW_DAYS are left, it only makes sure the uploader's cache holds that login, then stops.
  3. Otherwise it logs in the same way the mangadot.net login page does (username + password form), using
     Firefox's Cloudflare pass, and saves the new login to the uploader's cache:
     .auth-cache.json (config.ini [auth] cache_file)
  Firefox's own login is not touched; the uploader reads the cache first.

The password is read from Windows Credential Manager. It is never written to a file or printed.
The username comes from config.ini [auth] username (gitignored).
One-time setup (you type the password at the hidden prompt):
    py -m keyring set mangadot <your MangaDot username>

Usage:
    py mangadot_login.py           # log in only if needed (what the uploader runs)
    py mangadot_login.py --check   # just report, never log in
    py mangadot_login.py --force   # log in now even if the current login is fine
Exit code 0 = logged in, 1 = could not log in (details in mangadot_login_log.txt; a Discord DM is sent at most every 12 h).
Log: mangadot_login_log.txt next to this script.
"""
import argparse, datetime, json, os, re, subprocess, sys, time

import requests
import auth_strategy as a
from curl_cffi import requests as cr
from mangadot_settings import HERE, CACHE, USERNAME

LOG = os.path.join(HERE, "mangadot_login_log.txt")
STATE = os.path.join(HERE, "mangadot_login_state.json")
KRATOS = "https://mangadot.net/api/.ory/kratos/public"
KEYRING_SERVICE = "mangadot"
RENEW_DAYS = 2
FIREFOX = r"C:\Program Files\Mozilla Firefox\firefox.exe"
JSON = {"Accept": "application/json"}


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M ") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def http(cookies, ua):
    s = cr.Session(impersonate="firefox135")
    s.headers["User-Agent"] = ua
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=".mangadot.net")
    return s


def cloudflare_blocked(r):
    return r.status_code == 403 and "Just a moment" in r.text


def expires(info):
    """whoami's expires_at as a UTC timestamp."""
    t = re.sub(r"\.\d+", "", info["expires_at"]).replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(t).timestamp()


class CloudflareBlock(RuntimeError):
    """Cloudflare wants a fresh check before it lets requests through."""


def whoami(cookies, ua):
    """The live session for these cookies, or None if logged out. Raises CloudflareBlock."""
    if "ory_kratos_session" not in cookies:
        return None
    r = http(cookies, ua).get(KRATOS + "/sessions/whoami", headers=JSON, timeout=30)
    if cloudflare_blocked(r):
        raise CloudflareBlock("Cloudflare check")
    if r.status_code != 200:
        return None
    j = r.json()
    return j if j.get("active") else None


def firefox_cookies():
    try:
        return a._read_firefox_cookies_direct("mangadot.net")[0]
    except Exception:
        return {}


def refresh_cloudflare():
    """Open mangadot.net in Firefox so it renews the Cloudflare pass, then re-read Firefox's cookies."""
    try:
        subprocess.Popen([FIREFOX, "https://mangadot.net"])
    except Exception:
        pass
    time.sleep(45)
    return firefox_cookies()


def current_login(ua):
    """(expires, source, cookies) for the first saved login MangaDot still accepts (the uploader's cache, then
    Firefox), or None. The cache is checked with Firefox's newest Cloudflare pass, since Firefox is what renews it.
    Stops at the first answer to keep requests to a minimum. Raises CloudflareBlock."""
    ff = firefox_cookies()
    cache = a.load_cache(CACHE)
    candidates = []
    if cache:
        cookies = dict(cache["cookies"])
        if ff.get("cf_clearance"):
            cookies["cf_clearance"] = ff["cf_clearance"]
        candidates.append(("cache", cookies))
    candidates.append(("Firefox", ff))
    for source, cookies in candidates:
        info = whoami(cookies, ua)
        if info:
            return expires(info), source, cookies
    return None


def password():
    import keyring
    if not USERNAME:
        raise RuntimeError("no username: set [auth] username in config.ini")
    pw = keyring.get_password(KEYRING_SERVICE, USERNAME)
    if not pw:
        raise RuntimeError(f'no saved password: run  py -m keyring set {KEYRING_SERVICE} {USERNAME}')
    return pw


def login(ua):
    """Run MangaDot's password login form; returns the new cookies. Raises CloudflareBlock."""
    ff = firefox_cookies()
    base = {k: v for k, v in ff.items() if k == "cf_clearance"}
    s = http(base, ua)
    r = s.get(KRATOS + "/self-service/login/browser", headers=JSON, timeout=30)
    if cloudflare_blocked(r):
        raise CloudflareBlock("Cloudflare check")
    if r.status_code != 200:
        raise RuntimeError(f"login page: HTTP {r.status_code}")
    flow = r.json()
    csrf = next((n["attributes"].get("value") for n in flow["ui"]["nodes"]
                 if n["attributes"].get("name") == "csrf_token"), None)
    body = {"method": "password", "identifier": USERNAME, "password": password(), "csrf_token": csrf}
    r = s.post(flow["ui"]["action"], json=body, headers={**JSON, "Content-Type": "application/json"}, timeout=30)
    if r.status_code != 200:
        try:
            msgs = [m["text"] for m in r.json().get("ui", {}).get("messages", [])]
            msgs += [m["text"] for n in r.json().get("ui", {}).get("nodes", []) for m in n.get("messages", [])]
        except Exception:
            msgs = []
        raise RuntimeError(f"login refused: HTTP {r.status_code} {'; '.join(msgs)}".strip())
    cookies = {c.name: c.value for c in s.cookies.jar}
    if "ory_kratos_session" not in cookies:
        raise RuntimeError("login answered OK but gave no session cookie")
    return cookies


def verify(cookies, ua):
    """Same test the uploader uses: /api/profile with plain requests."""
    s = requests.Session()
    a._apply_cookies(s, cookies, ua, "mangadot.net")
    return a.verify_session(s, "https://mangadot.net")


CLOUDFLARE_MSG = ("Cloudflare wants a human check on mangadot.net. On the home PC, tick \"Verify you are a human\" "
                  "on mangadot.net in Firefox (it's open there now); uploads work again once it has loaded.")


def alert(text):
    state = {}
    try:
        state = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        pass
    if time.time() - state.get("last_alert", 0) < 12 * 3600:
        return
    try:
        from mangadot_owed import dm
        dm(text)
        state["last_alert"] = time.time()
        json.dump(state, open(STATE, "w", encoding="utf-8"))
    except Exception as e:
        log(f"could not send the Discord alert: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="only report, never log in")
    ap.add_argument("--force", action="store_true", help="log in even if the current login is fine")
    args = ap.parse_args()
    ua = a._browser_ua("firefox")

    try:
        best = current_login(ua)
    except CloudflareBlock:
        # Opening the site in Firefox clears the automatic "Just a moment..." check by itself;
        # the "Verify you are a human" checkbox needs a person.
        log("Cloudflare check needed: opening mangadot.net in Firefox")
        refresh_cloudflare()
        try:
            best = current_login(ua)
        except CloudflareBlock:
            log("still blocked by Cloudflare: needs the checkbox ticked in Firefox")
            alert(CLOUDFLARE_MSG)
            return 1
    if best and not args.force:
        exp, source, cookies = best
        left = (exp - time.time()) / 86400
        until = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))
        if left > RENEW_DAYS or args.check:
            a.save_cache(CACHE, cookies, ua)  # keeps the uploader on Firefox's newest Cloudflare pass
            log(f"logged in until {until} ({left:.1f} days, {source}); nothing to do")
            return 0
        log(f"login ends {until} ({left:.1f} days left); renewing")
    elif args.check:
        log("not logged in (cache and Firefox)")
        return 1
    elif args.force:
        log("--force: logging in")
    else:
        log("not logged in; logging in")

    try:
        cookies = login(ua)
        user = verify(cookies, ua)
        if not user:
            raise RuntimeError("new login was not accepted by /api/profile")
        a.save_cache(CACHE, cookies, ua)
        info = whoami(cookies, ua)
        until = time.strftime("%Y-%m-%d %H:%M", time.localtime(expires(info))) if info else "?"
        log(f"logged in as {user.get('username', '?')} until {until}; saved to the uploader's cache")
        return 0
    except CloudflareBlock:
        log("login blocked by Cloudflare: needs the checkbox ticked in Firefox")
        refresh_cloudflare()
        alert(CLOUDFLARE_MSG)
        return 1
    except Exception as e:
        log(f"FAILED: {e}")
        alert(f"MangaDot auto-login failed on the home PC: {e}. Log in at mangadot.net in Firefox when you can.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
