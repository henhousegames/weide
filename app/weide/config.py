"""Central constants and paths.

The app name lives in APP_NAME only (plus APP_TAGLINE); the web UI reads it from /api/meta and the
server/export inject it into index.html. Every path is relative to the repository root or set by an
environment variable, so nothing here assumes a particular machine layout.

Environment variables:
  WEIDE_PRIVATE_DIR  private, gitignored data (database, later prompts/personas). Default: <repo>/private
  WEIDE_DB           SQLite database file. Default: $WEIDE_PRIVATE_DIR/weide.db
  WEIDE_MEDIA_ROOT   web-size media files (post videos/images). Default: <repo>/media
  WEIDE_THUMB_ROOT   pre-rendered poster thumbnails, <width>/<path>.jpg. Default: <repo>/thumb
  WEIDE_AVATAR_ROOT  square avatar images. Default: <repo>/avatar
  WEIDE_STATIC_DIR   frontend assets (app.css, js/). Default: <repo>/static
  WEIDE_TODAY        YYYY-MM-DD, the world's "today" (ranking windows); default: local date
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

APP_NAME = "Weide"
APP_TAGLINE = "Das Netzwerk für Creator mit Auslauf"
APP_SLUG = re.sub(r"[^a-z0-9]+", "", APP_NAME.lower()) or "app"

# --- paths -------------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent              # app/weide
REPO_ROOT = PACKAGE_DIR.parent.parent                       # repository root
TEMPLATE_DIR = PACKAGE_DIR / "templates"


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser().resolve() if v else default


PRIVATE_DIR = _env_path("WEIDE_PRIVATE_DIR", REPO_ROOT / "private")
DEFAULT_DB = _env_path("WEIDE_DB", PRIVATE_DIR / "weide.db")
MEDIA_ROOT = _env_path("WEIDE_MEDIA_ROOT", REPO_ROOT / "media")
THUMB_ROOT = _env_path("WEIDE_THUMB_ROOT", REPO_ROOT / "thumb")
AVATAR_ROOT = _env_path("WEIDE_AVATAR_ROOT", REPO_ROOT / "avatar")
STATIC_DIR = _env_path("WEIDE_STATIC_DIR", REPO_ROOT / "static")

TZ = ZoneInfo("Europe/Berlin")
DEFAULT_PORT = 8740

# "Today" in the world. Defaults to the machine's local date; WEIDE_TODAY=YYYY-MM-DD overrides.
TODAY_OVERRIDE = os.environ.get("WEIDE_TODAY")

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
PUBLIC_GENERATION_KEYS = ("model_label", "summary_url")
