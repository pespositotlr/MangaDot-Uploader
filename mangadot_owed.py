r"""MangaDot catch-up + login watch (2026-10-12).

  py mangadot_owed.py add --post-title "Gintama Volume 75" --volume 75 --chapter 676 --chapter-name "..." --file "K:\...\676.zip"
  py mangadot_owed.py run      # upload everything owed (if logged in)
  py mangadot_owed.py list

`run`: checks the Firefox MangaDot login.
  - Cloudflare pass expired -> runs mangadot_refresh.py (opens mangadot.net in Firefox and waits for the pass).
  - Logged out -> runs mangadot_login.py (auto-login); if that fails, opens the login page in Firefox and DMs
    the user on Discord (at most once every 12 h).
  - Logged in -> uploads every owed chapter (MangaDot only, via release-updater.py), removes the ones that worked,
    and DMs a summary.
Personal settings (Discord IDs, Release Manager folder) come from config.ini via mangadot_settings.py.
"""
import argparse, json, os, re, subprocess, sys, time
import requests
from mangadot_settings import HERE, DISCORD_USER_ID, DISCORD_BOT_FILE, RELEASE_MANAGER

OWED = os.path.join(HERE, "mangadot_owed.json")
STATE = os.path.join(HERE, "mangadot_owed_state.json")
LOG = os.path.join(HERE, "mangadot_owed_log.txt")
RM = RELEASE_MANAGER
USER_ID = DISCORD_USER_ID
FIREFOX = r"C:\Program Files\Mozilla Firefox\firefox.exe"


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M ") + msg
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def save(path, data):
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def dm(text):
    token = [l.split("=", 1)[1].strip() for l in open(DISCORD_BOT_FILE, encoding="utf-8") if l.startswith("ACCESS_TOKEN")][0]
    h = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    ch = requests.post("https://discord.com/api/v10/users/@me/channels", headers=h, json={"recipient_id": USER_ID}, timeout=30).json()["id"]
    requests.post(f"https://discord.com/api/v10/channels/{ch}/messages", headers=h, json={"content": text}, timeout=30).raise_for_status()


def check():
    r = subprocess.run(["py", os.path.join(HERE, "mangadot_check.py")], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def cmd_add(a):
    owed = load(OWED, [])
    key = (a.post_title, str(a.chapter))
    owed = [o for o in owed if (o["post_title"], str(o["chapter"])) != key]
    owed.append({"post_title": a.post_title, "volume": a.volume, "chapter": str(a.chapter), "chapter_name": a.chapter_name,
                 "file": a.file, "added": time.strftime("%Y-%m-%d %H:%M")})
    save(OWED, owed)
    print(f"owed: {len(owed)} chapter(s)")


def cmd_run(a):
    lock = os.path.join(HERE, "mangadot_owed.lock")
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 3 * 3600:
        log("another run is active; skipping"); return
    open(lock, "w").write(str(os.getpid()))
    try:
        _run(a)
    finally:
        try: os.remove(lock)
        except OSError: pass


def _run(a):
    owed = load(OWED, [])
    state = load(STATE, {})
    code, msg = check()
    if code == 1:  # Cloudflare pass only: the refresh script fixes that by itself
        subprocess.run(["py", os.path.join(HERE, "mangadot_refresh.py")], capture_output=True, text=True)
        code, msg = check()
    if code != 0:  # try the auto-login before bothering the user
        subprocess.run(["py", os.path.join(HERE, "mangadot_login.py")], capture_output=True, text=True)
        code, msg = check()
    if code != 0:
        last = state.get("last_alert", 0)
        if time.time() - last > 12 * 3600 or a.force_alert:
            try:
                subprocess.Popen([FIREFOX, "https://mangadot.net/login"])
            except Exception:
                pass
            dm(f"MangaDot is logged out in Firefox on the home PC ({len(owed)} chapter(s) waiting). "
               "Log in at mangadot.net (the login page is open in Firefox); owed uploads go out automatically within 30 min.")
            state["last_alert"] = time.time(); save(STATE, state)
            log(f"logged out; alerted ({len(owed)} owed)")
        else:
            log(f"logged out; already alerted ({len(owed)} owed)")
        return
    state["last_alert"] = 0; save(STATE, state)
    if not owed:
        log("logged in; nothing owed")
        return
    done, failed = [], []
    for o in list(owed):
        lf = os.path.join(HERE, f"mangadot_owed_{re.sub(r'[^A-Za-z0-9]+', '_', o['post_title'])}_{o['chapter']}.log")
        cmd = ["py", "-u", "release-updater.py", "--post-title", o["post_title"], "--file", o["file"], "--chapter", o["chapter"],
               "--chapter-name", o["chapter_name"], "--skip", "catbox", "mega", "cubari", "mangadex", "mangataro", "wordpress"]
        if o.get("volume"):
            cmd += ["--volume", str(o["volume"])]
        with open(lf, "w", encoding="utf-8", errors="replace") as fh:
            subprocess.run(cmd, cwd=RM, stdout=fh, stderr=subprocess.STDOUT)
        out = re.sub(r"\x1b\[[0-9;]*m", "", open(lf, encoding="utf-8", errors="replace").read())
        m = re.search(r"^\| MangaDot: *(https?://\S+)", out, re.M)
        if m:
            done.append(f"{o['post_title']} ch{o['chapter']}")
            owed = [x for x in owed if not (x["post_title"] == o["post_title"] and x["chapter"] == o["chapter"])]
            save(OWED, owed)
        else:
            failed.append(f"{o['post_title']} ch{o['chapter']}")
    log(f"uploaded {len(done)}, failed {len(failed)}: {failed}")
    dm(f"MangaDot catch-up: uploaded {len(done)} chapter(s)" + (f"; failed: {', '.join(failed)}" if failed else "") + ".")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    ad = sub.add_parser("add")
    ad.add_argument("--post-title", required=True); ad.add_argument("--volume", default="")
    ad.add_argument("--chapter", required=True); ad.add_argument("--chapter-name", required=True); ad.add_argument("--file", required=True)
    rn = sub.add_parser("run"); rn.add_argument("--force-alert", action="store_true")
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "add": cmd_add(a)
    elif a.cmd == "run": cmd_run(a)
    else:
        for o in load(OWED, []): print(o["post_title"], o["chapter"], o["chapter_name"], o["added"])


if __name__ == "__main__":
    main()
