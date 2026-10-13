"""Refresh MangaDot's Cloudflare pass in Firefox, then confirm with mangadot_check.py.

Run before any upload that includes MangaDot:  py mangadot_refresh.py  (in the MangaDot-Uploader folder)
- If the check already passes, it does nothing.
- Otherwise it opens https://mangadot.net in Firefox (the uploader reads Firefox's cookies, and the
  Cloudflare pass is tied to Firefox's user agent), then re-checks every 15 s for up to ~2 minutes.
Exit code 0 = OK, 1 = still failing (open the site in Firefox by hand / log in).
"""
import os, subprocess, sys, time, winreg

CHECK = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mangadot_check.py")]


def check():
    r = subprocess.run(CHECK, capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip().splitlines()
    return out[-1] if out else "(no output)"


def firefox_path():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\firefox.exe") as k:
            return winreg.QueryValue(k, None)
    except OSError:
        return r"C:\Program Files\Mozilla Firefox\firefox.exe"


status = check()
print(status)
if "OK" in status:
    sys.exit(0)
subprocess.Popen([firefox_path(), "https://mangadot.net"])
print("Opened mangadot.net in Firefox, waiting for the Cloudflare pass...")
for _ in range(8):
    time.sleep(15)
    status = check()
    print(status)
    if "OK" in status:
        sys.exit(0)
print("Still not OK: open https://mangadot.net in Firefox by hand (and log in if asked), then re-run.")
sys.exit(1)
