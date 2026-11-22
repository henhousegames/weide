"""Central constants and paths.

The app name lives in APP_NAME only (plus APP_TAGLINE); the web UI reads it from /api/meta and the
server/export inject it into index.html. Every path is relative to the repository root or set by an
environment variable, so nothing here assumes a particular machine layout.

Environment variables use the app prefix (HENHOUSE_*, derived from APP_NAME); the old WEIDE_* names
are still read as a fallback, so existing setups keep working. The prefixed name wins when both are set.
  HENHOUSE_PRIVATE_DIR  private, gitignored data (database, later prompts/personas). Default: <repo>/private
  HENHOUSE_DB           SQLite database file. Default: $HENHOUSE_PRIVATE_DIR/henhouse.db, or weide.db there
                        if only that legacy file exists
  HENHOUSE_MEDIA_ROOT   web-size media files (post videos/images). Default: <repo>/media
  HENHOUSE_THUMB_ROOT   pre-rendered poster thumbnails, <width>/<path>.jpg. Default: <repo>/thumb
  HENHOUSE_AVATAR_ROOT  square avatar images. Default: <repo>/avatar
  HENHOUSE_STATIC_DIR   frontend assets (app.css, js/). Default: <repo>/static
  HENHOUSE_TODAY        YYYY-MM-DD, the world's "today" (ranking windows); default: local date
  HENHOUSE_FEED_DIR     preview only: curated feed dir (posts.jsonl, interactions.jsonl; read-only).
                        Unset: the preview shows published posts, approvals and drafts (no curated feed)
  HENHOUSE_CHARACTERS_DIR  preview only: character identities (<slug>/identity.md).
                        Default: <feed dir>/../characters
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

APP_NAME = "henhouse"
APP_TAGLINE = "Das Netzwerk für Creator mit Fell, Feierabend oder Firmware"
# Shown in the footer of every page and on robot posts. static/js/util.js keeps the same text as a
# fallback, because the live site's data/meta.json may predate this field.
DISCLAIMER = ("Fiktive, KI-generierte Inhalte. Keine Verbindung zu Unitree Robotics oder NVIDIA; "
              "Marken gehören ihren Inhabern.")
APP_SLUG = re.sub(r"[^a-z0-9]+", "", APP_NAME.lower()) or "app"
ENV_PREFIX = APP_SLUG.upper() + "_"
LEGACY_SLUG = "weide"                      # previous app name: env vars, db file, browser storage keys
LEGACY_ENV_PREFIX = LEGACY_SLUG.upper() + "_"

# --- paths -------------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent              # app/weide
REPO_ROOT = PACKAGE_DIR.parent.parent                       # repository root
TEMPLATE_DIR = PACKAGE_DIR / "templates"


def env(name: str) -> str | None:
    """HENHOUSE_<name>, falling back to the legacy WEIDE_<name>."""
    return os.environ.get(ENV_PREFIX + name) or os.environ.get(LEGACY_ENV_PREFIX + name) or None


def _env_path(name: str, default: Path) -> Path:
    v = env(name)
    return Path(v).expanduser().resolve() if v else default


def _default_db(private: Path) -> Path:
    new, legacy = private / f"{APP_SLUG}.db", private / f"{LEGACY_SLUG}.db"
    return legacy if legacy.is_file() and not new.exists() else new


PRIVATE_DIR = _env_path("PRIVATE_DIR", REPO_ROOT / "private")
DEFAULT_DB = _env_path("DB", _default_db(PRIVATE_DIR))
MEDIA_ROOT = _env_path("MEDIA_ROOT", REPO_ROOT / "media")
THUMB_ROOT = _env_path("THUMB_ROOT", REPO_ROOT / "thumb")
AVATAR_ROOT = _env_path("AVATAR_ROOT", REPO_ROOT / "avatar")
STATIC_DIR = _env_path("STATIC_DIR", REPO_ROOT / "static")

TZ = ZoneInfo("Europe/Berlin")
DEFAULT_PORT = 8740

# "Today" in the world. Defaults to the machine's local date; HENHOUSE_TODAY=YYYY-MM-DD overrides.
TODAY_OVERRIDE = env("TODAY")



def feed_dir() -> Path | None:
    """HENHOUSE_FEED_DIR (no legacy fallback), read when the preview starts; None = no curated feed."""
    v = os.environ.get(ENV_PREFIX + "FEED_DIR")
    return Path(v).expanduser().resolve() if v else None


def characters_dir(feed: Path) -> Path:
    v = os.environ.get(ENV_PREFIX + "CHARACTERS_DIR")
    return Path(v).expanduser().resolve() if v else feed.parent / "characters"


# --- media -------------------------------------------------------------------------------
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".ogg"}
MEDIA_EXT = VIDEO_EXT | IMAGE_EXT | AUDIO_EXT

# --- reactions ---------------------------------------------------------------------------
# key -> (label, emoji, ranking weight). "chew" = "Wiederkäuen": I chewed on this twice (rewatched it).
REACTIONS = {
    "lol":   {"label": "Lachen",      "emoji": "😂", "weight": 1.0},
    "fire":  {"label": "Feuer",       "emoji": "🔥", "weight": 0.5},
    "chew":  {"label": "Wiederkäuen", "emoji": "🌾", "weight": 0.5},
    "heart": {"label": "Herz",        "emoji": "❤️", "weight": 0.2},
}
REACTION_TYPES = tuple(REACTIONS)

# --- ranking -----------------------------------------------------------------------------
# Curated posts (funny_score F, 0-25) and uncurated posts share one 0-25 scale; see ranking.py.
COMMENT_WEIGHT = 25.0      # one comment (not by the author) counts like 25 lol reactions
ENGAGEMENT_REF = 6000.0    # E = 25 * (1 - exp(-W / ENGAGEMENT_REF))
CURATED_WEIGHT = 0.9       # with a curated funny_score F: score = 0.9*F + 0.1*E   (0..25)
UNCURATED_MAX = 15.0       # without F: score = UNCURATED_MAX * E / 25             (0..15)
SCORE_MAX = 25.0
FUNNY_MIN, FUNNY_MAX = 0.0, 25.0

POST_KINDS = ("story", "post", "reel", "image", "text", "collection")
POST_STATUSES = ("draft", "approved", "published")

# Posting characters are animal | human | robot | other; follower and house are audience/system accounts.
ACCOUNT_KINDS = ("animal", "human", "robot", "other", "follower", "house")
CHARACTER_KINDS = ("animal", "human", "robot", "other")
CONTROLLER_TYPES = ("agent", "human")

# A "world" groups characters for feed filters. accounts.world wins; otherwise derived from the kind.
WORLD_BY_KIND = {"animal": "weide", "human": "menschen", "robot": "roboter"}

# Card fields of a generation summary that may become public (whitelist; see export_static.py).
PUBLIC_GENERATION_KEYS = ("model_label", "start_source_sim")
