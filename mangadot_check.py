# MangaDot pre-flight check: is the Firefox login still good (session + Cloudflare pass)?
# Run: py mangadot_check.py   (in the MangaDot-Uploader folder)
# Exit code 0 = OK, 1 = Cloudflare check expired (open mangadot.net in Firefox), 2 = not logged in / other problem.
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
import auth_strategy as a
from mangadot_settings import CACHE

try:
    cookies, _ = a._read_firefox_cookies_direct('mangadot.net')
except Exception as e:
    print('No MangaDot cookies in Firefox:', e); sys.exit(2)
s = requests.Session(); s.headers['User-Agent'] = a._browser_ua('firefox')
for k, v in cookies.items():
    s.cookies.set(k, v, domain='mangadot.net')
r = s.get('https://mangadot.net/api/profile', timeout=30)
if r.status_code != 200:  # Firefox logged out: the uploader also accepts its own cache (kept fresh by mangadot_login.py)
    cache = a.load_cache(CACHE)
    if cache:
        c = requests.Session(); c.headers['User-Agent'] = cache.get('user_agent') or s.headers['User-Agent']
        for k, v in cache['cookies'].items():
            c.cookies.set(k, v, domain='mangadot.net')
        rc = c.get('https://mangadot.net/api/profile', timeout=30)
        if rc.status_code == 200:
            r = rc
if r.status_code == 200:
    try: j = r.json(); name = (j.get('profile') or j).get('username')
    except Exception: name = '?'
    print('MangaDot OK, logged in as', name); sys.exit(0)
if r.status_code == 403 and 'Just a moment' in r.text:
    print('MangaDot: Cloudflare check expired. Open https://mangadot.net in Firefox, let the "Just a moment..." page pass, then re-run this check.')
    sys.exit(1)
if 'ory_kratos_session' not in cookies:
    print('MangaDot: not logged in in Firefox (no session cookie). Log in at https://mangadot.net in Firefox.'); sys.exit(2)
print('MangaDot: HTTP', r.status_code, '- not usable right now.'); sys.exit(2)
