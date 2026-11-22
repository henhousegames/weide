"""Curated feed files for the private preview (HENHOUSE_FEED_DIR). Read-only: nothing here ever writes.

The feed dir is written by another tool; this module only reads it (see docs/PREVIEW.md, "Curated feed"):

  <feed_dir>/posts.jsonl         {"id", "generation_id", "author", "kind": "video"|"image", "caption",
                                  "posted_at", "story"?, "hidden"?}     (append-only; a later line with the
                                  same id replaces the earlier one)
  <feed_dir>/interactions.jsonl  {"type": "comment"|"like", "post", "author", "text"?, "at"}
  <characters_dir>/<slug>/identity.md
                                 front matter (name, slug, handle, kind, bio, avatar) between two `---`
                                 lines, then a private markdown body

Timestamps must carry an offset (naive ones are skipped). Malformed lines are skipped with one log line
(line number and reason, never the content). Files are re-read only when their mtime or size changes.

The body of identity.md is private character notes: the reader stops at the closing `---` of the front
matter, so the body is never read into memory, returned or logged.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("weide.feed")

POSTS_FILE = "posts.jsonl"
INTERACTIONS_FILE = "interactions.jsonl"
IDENTITY_FILE = "identity.md"
POST_KINDS = ("video", "image")
INTERACTION_TYPES = ("comment", "like")
ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")         # account slugs (e.g. "steffi.amt")
HANDLE = re.compile(r"^@?[A-Za-z0-9._-]{1,64}$")
MAX_LINE = 64 * 1024                  # longer lines are skipped
MAX_CAPTION = 2200
MAX_COMMENT = 2200
MAX_STORY = 200
MAX_NAME = 80
BIO_MAX = 150
FRONT_MATTER_MAX_LINES = 64           # a front matter without a closing `---` in this many lines is ignored
# identity.md `kind` -> account kind (existing kinds: animal | human | robot | other | follower | house)
KIND_MAP = {"animal": "animal", "robot": "robot", "thing": "other", "human": "human", "house": "house", "other": "other"}


def parse_aware(value: Any) -> datetime | None:
    """ISO 8601 with an offset (or Z); None for anything else, including naive timestamps."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None and dt.utcoffset() is not None else None


def _text(v: Any, limit: int) -> str | None:
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v[:limit] if v else None


@dataclass(frozen=True)
class FeedPost:
    id: str
    generation_id: str
    author: str
    kind: str
    caption: str | None
    posted_at: datetime
    story: str | None
    hidden: bool
    line: int

    @classmethod
    def parse(cls, d: Any, line: int) -> "FeedPost":
        if not isinstance(d, dict):
            raise ValueError("not an object")
        pid, gid, author = d.get("id"), d.get("generation_id"), d.get("author")
        if not isinstance(pid, str) or not ID.match(pid):
            raise ValueError("invalid id")
        if not isinstance(gid, str) or not ID.match(gid):
            raise ValueError("invalid generation_id")
        if not isinstance(author, str) or not SLUG.match(author):
            raise ValueError("invalid author")
        if d.get("kind") not in POST_KINDS:
            raise ValueError("unknown kind")
        at = parse_aware(d.get("posted_at"))
        if at is None:
            raise ValueError("posted_at missing or without offset")
        caption = d.get("caption")
        if caption is not None and not isinstance(caption, str):
            raise ValueError("invalid caption")
        hidden = d.get("hidden")
        if hidden is not None and not isinstance(hidden, bool):
            raise ValueError("invalid hidden")
        return cls(id=pid, generation_id=gid, author=author, kind=d["kind"], caption=_text(caption, MAX_CAPTION),
                   posted_at=at, story=_text(d.get("story"), MAX_STORY), hidden=hidden is True, line=line)


@dataclass(frozen=True)
class Interaction:
    type: str
    post: str
    author: str
    text: str | None
    at: datetime
    line: int

    @classmethod
    def parse(cls, d: Any, line: int) -> "Interaction":
        if not isinstance(d, dict):
            raise ValueError("not an object")
        typ, post, author = d.get("type"), d.get("post"), d.get("author")
        if typ not in INTERACTION_TYPES:
            raise ValueError("unknown type")
        if not isinstance(post, str) or not ID.match(post):
            raise ValueError("invalid post")
        if not isinstance(author, str) or not SLUG.match(author):
            raise ValueError("invalid author")
        at = parse_aware(d.get("at"))
        if at is None:
            raise ValueError("at missing or without offset")
        text = _text(d.get("text"), MAX_COMMENT)
        if typ == "comment" and not text:
            raise ValueError("comment without text")
        return cls(type=typ, post=post, author=author, text=text if typ == "comment" else None, at=at, line=line)


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


class JsonlFile:
    """One jsonl file, parsed line by line with `parse(obj, line_no)`; re-read when mtime/size change."""

    def __init__(self, path: Path, parse: Callable[[Any, int], Any]):
        self.path = path
        self.parse = parse
        self.stamp: tuple[int, int] | None = None
        self.rows: list = []
        self.skipped = 0

    def load(self) -> list:
        stamp = _stamp(self.path)
        if stamp == self.stamp:
            return self.rows
        rows, skipped = [], 0
        if stamp is not None:
            try:
                with self.path.open("r", encoding="utf-8", errors="replace") as f:
                    for n, line in enumerate(f, 1):
                        if not line.strip():
                            continue
                        try:
                            if len(line) > MAX_LINE:
                                raise ValueError("line too long")
                            rows.append(self.parse(json.loads(line), n))
                        except (ValueError, TypeError) as e:       # json.JSONDecodeError is a ValueError
                            skipped += 1
                            reason = "invalid JSON" if isinstance(e, json.JSONDecodeError) else str(e)
                            log.warning("%s line %d skipped: %s", self.path.name, n, reason)
            except OSError as e:
                log.warning("%s not readable: %s", self.path.name, type(e).__name__)
                rows, skipped = [], 0
        self.stamp, self.rows, self.skipped = stamp, rows, skipped
        return rows


# --- identity.md front matter ------------------------------------------------------------------------

def _scalar(raw: str) -> str | None:
    """A YAML-ish scalar: "double" or 'single' quoted, or plain (a ` #` starts a comment)."""
    v = raw.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        if v[0] == '"':
            try:
                inner = json.loads(v)
            except ValueError:
                pass
        else:
            inner = inner.replace("''", "'")
        return inner if isinstance(inner, str) else None
    v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
    return None if v in ("", "~", "null", "Null", "NULL") else v


def read_front_matter(path: Path) -> dict[str, str] | None:
    """The `key: value` pairs between the first two `---` lines, or None. Reads line by line and stops at
    the closing `---`: the markdown body after it is never read. Nested YAML is not supported (ignored)."""
    out: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            if f.readline().strip() != "---":
                return None
            for _ in range(FRONT_MATTER_MAX_LINES):
                line = f.readline(4096)
                if not line:
                    return None
                if line.strip() == "---":
                    return out
                if not line.strip() or line.lstrip().startswith("#") or line[0] in " \t-":
                    continue
                key, sep, value = line.partition(":")
                key = key.strip()
                if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
                    v = _scalar(value)
                    if v is not None:
                        out[key] = v
    except OSError:
        return None
    return None


@dataclass(frozen=True)
class Identity:
    slug: str
    name: str | None
    handle: str | None
    kind: str | None              # account kind (thing -> other)
    bio: str | None
    avatar: str | None            # generation id of an image

    @classmethod
    def from_front_matter(cls, slug: str, fm: dict[str, str]) -> "Identity":
        handle = fm.get("handle")
        handle = ("@" + handle.lstrip("@")) if handle and HANDLE.match(handle) else None
        bio = fm.get("bio")
        if bio and len(bio) > BIO_MAX:
            bio = bio[:BIO_MAX - 1].rstrip() + "…"
        avatar = fm.get("avatar")
        kind = (fm.get("kind") or "").lower()
        return cls(slug=slug, name=_text(fm.get("name"), MAX_NAME), handle=handle,
                   kind=KIND_MAP.get(kind, "other") if kind else None, bio=bio or None,
                   avatar=avatar if avatar and ID.match(avatar) else None)


class Characters:
    """identity.md front matter per character slug (cached by mtime)."""

    def __init__(self, root: Path):
        self.root = root
        self.cache: dict[str, tuple[tuple[int, int] | None, Identity | None]] = {}

    def get(self, slug: str) -> Identity | None:
        if not SLUG.match(slug):
            return None
        path = self.root / slug / IDENTITY_FILE
        stamp = _stamp(path)
        hit = self.cache.get(slug)
        if hit and hit[0] == stamp:
            return hit[1]
        ident = None
        if stamp is not None:
            fm = read_front_matter(path)
            if fm is None:
                log.warning("characters/%s/%s: no readable front matter", slug, IDENTITY_FILE)
            else:
                if fm.get("slug") not in (None, slug):
                    log.warning("characters/%s/%s: front matter slug differs from the directory; using the directory",
                                slug, IDENTITY_FILE)
                ident = Identity.from_front_matter(slug, fm)
        self.cache[slug] = (stamp, ident)
        return ident

    def slugs(self) -> list[str]:
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        return sorted(n for n in names if SLUG.match(n) and (self.root / n / IDENTITY_FILE).is_file())


class FeedSource:
    """The curated feed dir. `now` returns an aware datetime (tests inject a clock)."""

    def __init__(self, feed_dir: Path, characters_dir: Path | None = None,
                 now: Callable[[], datetime] | None = None):
        self.dir = Path(feed_dir)
        self.characters = Characters(Path(characters_dir) if characters_dir else self.dir.parent / "characters")
        self.now = now or (lambda: datetime.now().astimezone())
        self._posts = JsonlFile(self.dir / POSTS_FILE, FeedPost.parse)
        self._interactions = JsonlFile(self.dir / INTERACTIONS_FILE, Interaction.parse)

    def posts(self) -> dict[str, FeedPost]:
        """Every post by id; a later line with the same id replaces the earlier one."""
        out: dict[str, FeedPost] = {}
        for p in self._posts.load():
            out[p.id] = p
        return out

    def due_posts(self, now: datetime) -> list[FeedPost]:
        """Posts that are due (posted_at <= now) and not hidden (readability is checked by the caller)."""
        return [p for p in self.posts().values() if not p.hidden and p.posted_at <= now]

    def interactions(self, now: datetime) -> list[Interaction]:
        return [i for i in self._interactions.load() if i.at <= now]

    @property
    def skipped(self) -> int:
        return self._posts.skipped + self._interactions.skipped
