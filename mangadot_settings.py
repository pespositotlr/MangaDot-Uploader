"""Personal settings for the MangaDot helper scripts (mangadot_login / _check / _refresh / _owed).

Everything personal is read from config.ini, which is gitignored:
  [auth]    username, cache_file
  [alerts]  discord_user_id, discord_bot_file   (Discord DMs when something needs you)
  [release] manager_dir                         (Manga-Release-Manager checkout, for release-updater.py)
"""
import configparser, os

HERE = os.path.dirname(os.path.abspath(__file__))
_cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
_cfg.read(os.path.join(HERE, "config.ini"), encoding="utf-8")


def get(section, key, default=""):
    return _cfg.get(section, key, fallback="").strip() or default


USERNAME = get("auth", "username")
CACHE = get("auth", "cache_file") or os.path.join(HERE, ".auth-cache.json")
DISCORD_USER_ID = get("alerts", "discord_user_id")
DISCORD_BOT_FILE = get("alerts", "discord_bot_file")
RELEASE_MANAGER = get("release", "manager_dir")
