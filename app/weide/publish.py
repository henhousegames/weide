"""`weide pull-approved`: fetch publications approved in the generation service and publish them as posts.

Everything that knows the service's publish API ("Publishing" in the service's docs/API.md) lives in
`ServiceClient` and `Publication`. A key with scope `publish` reads:

  GET {base}/api/v1/publications[?since=<ISO>]
      -> {publications: [{public_id, character, caption, approved_at, task, model{id, display_name},
                          sha256, mime, video_url | image_url, poster_url, media[], links{summary}}],
          revoked: [{public_id, revoked_at}], since, next_since}
      Without `since`: every active publication and every revoked one. With `since`: approvals (and
      caption updates, same public_id, newer approved_at) and revocations after that time. We follow
      `next_since` until a page comes back empty.
  GET {base}/api/v1/publications/{public_id}/summary      sanitized by the service; we sanitize again
  GET {base}/api/v1/publications/{public_id}/media/{output|poster|input-N}

Cursor: API.md defines `revoked` as the withdrawals after `since`, so incremental polling sees every
revocation. After a fully successful (non-dry) pull `next_since` is stored in the DB meta
(`publications_since`) and the next pull asks only for changes after it. Any failed item keeps the old
cursor, so the item is fetched again next time; `--full` ignores the cursor (full reconcile).

Full listing (`--full`, or no cursor yet): every active publication is fetched again, including its
summary (credits and licence notices are computed by the service at summary time, so they can change),
and already-imported posts are updated when their record changed. Revoked ids are unpublished as usual,
and so is every published generation post whose public_id is in neither list any more (it vanished from
the service). That last step is skipped when the listing is empty or contains an entry without a valid
public_id (a broken listing must not unpublish everything). After a fully successful non-dry full
listing the time is stored in the DB meta (`last_full_pull_at`); `weide deploy` uses it to run one full
pull per day (deploy.full_pull_due). A failed run leaves it alone, so the full pull is retried.

Relative URLs are resolved against the base URL. The bearer key is only sent to the service's own
origin (media URLs on another host get no key) and is never printed: every error text goes through
`_redact`. Settings come from the environment or from a gitignored `.env` in the private dir:
  HENHOUSE_SERVICE_URL   base URL of the service (scheme://host:port; a trailing /api/v1 is fine too)
  HENHOUSE_SERVICE_KEY   bearer key (scope `publish`)

Each publication becomes one post (status published: approval happened in the service) matched by
source `generation:<public_id>` (the API exposes no run id, and the internal run id never reaches us),
so a rerun never duplicates; caption/character/summary changes update it (keeping the first posted_at).
`video/mp4` becomes a `reel`, `image/png|jpeg|webp` an `image` post. The character account must already
exist. Media go to <media root>/gen/<public_id>.<ext> (sha256 from the list verified), a 540 px poster to
<thumb root>/540/gen/<public_id>.jpg: the service poster (scaled with ffmpeg, or kept as is when it is a
JPEG and ffmpeg is missing), else a frame of the media (ffmpeg; skipped without it).
Start source: when the summary's whitelisted field `start_source` is "isaac-sim" (top level, already inherited
from any compose parent by the service, or any `composition.segments[].start_source`), the record gets
`start_source_sim: true` (generation.start_source_sim); the public card shows "Startbild aus eigener Simulation".
Labels are never read for this (the public summary carries none).
Inputs: only inputs the summary marks `published: true` with a `source_public_id` are kept, as a
reference to that publication's own post (shown at export only if that post is published here);
every other input is a "privater Input". Input media are not downloaded (they are posts of their own).
Every item is validated on its own, fetched into a temp dir and written in one DB transaction; a failed
item leaves no post and no file behind and does not stop the others.

Credits: the summary's `credits` list (else the list item's) is kept in generation_json as `credits`
(generation.sanitize_credits: kind/text/model/license{id,url}, strings only, http(s) URL only; [] when
missing). A re-pull (`--full`) of an already-imported post updates generation_json when they changed.
Audio-only publications (`audio_url` without video/image, or an audio/* mime) are not posts: skipped
with a log line, no failure.

Revoked public_ids are unpublished: the post goes back to status draft (meta `revoked_at` notes why), so
it vanishes from the API and the export; the media file stays on disk. Unknown ids are a no-op. When an
id is both approved and revoked in one pull, the later timestamp wins (revoked on a tie or bad time).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from . import config
from .db import get_meta, jdump, jload, now_iso, parse_time, set_meta, upsert_post
from .generation import (CREDITS_KEY, PRIVATE_INPUT, PUBLIC_ID, REDACTED, START_SOURCE_KEY, model_short_label,
                         sanitize, sanitize_credits, start_source_sim)

PUBLICATIONS_PATH = "/api/v1/publications"
CURSOR_KEY = "publications_since"  # meta key: next_since of the last fully successful pull
LAST_FULL_KEY = "last_full_pull_at"  # meta key: time of the last fully successful full listing
MAX_PAGES = 1000
# mime -> (post kind, file extension)
MEDIA_TYPES = {"video/mp4": ("reel", ".mp4"), "image/png": ("image", ".png"), "image/jpeg": ("image", ".jpg"),
               "image/webp": ("image", ".webp")}
MAX_MEDIA_BYTES = 1 << 30          # 1 GiB: a web-size reel is far below this
POSTER_WIDTH = 540


class PublishError(Exception):
    pass


# --- settings ------------------------------------------------------------------------------------

def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (comments, blank lines, optional quotes, optional `export`)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.removeprefix("export ").split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


@dataclass(frozen=True)
class ServiceSettings:
    base_url: str
    key: str = field(repr=False)

    @classmethod
    def load(cls, dotenv: Path | None = None) -> "ServiceSettings":
        file = read_dotenv(dotenv or config.PRIVATE_DIR / ".env")

        def get(name: str) -> str | None:
            return config.env(name) or file.get(config.ENV_PREFIX + name) or file.get(config.LEGACY_ENV_PREFIX + name)

        base, key = get("SERVICE_URL"), get("SERVICE_KEY")
        if not base or not key:
            raise PublishError(f"set {config.ENV_PREFIX}SERVICE_URL and {config.ENV_PREFIX}SERVICE_KEY "
                               "(environment or .env in the private dir)")
        if urlsplit(base).scheme not in ("http", "https"):
            raise PublishError(f"{config.ENV_PREFIX}SERVICE_URL must be an http(s) URL")
        return cls(base.rstrip("/"), key)


# --- service adapter -------------------------------------------------------------------------------

def _sha_ok(sha: Any) -> bool:
    return isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha.lower())


@dataclass
class Publication:
    public_id: str
    character: str
    caption: str
    approved_at: str
    mime: str
    sha256: str
    media_url: str
    summary_url: str
    poster_url: str | None = None
    start_source_sim: bool = False     # the item's own start_source field is "isaac-sim" (list items lack it today)
    credits: list = field(default_factory=list)   # the list item's credits (sanitized); the summary's win

    @property
    def kind(self) -> str:
        return MEDIA_TYPES[self.mime][0]

    @property
    def ext(self) -> str:
        return MEDIA_TYPES[self.mime][1]

    @classmethod
    def parse(cls, d: Any) -> "Publication":
        if not isinstance(d, dict):
            raise PublishError("publication is not an object")
        missing = [k for k in ("public_id", "character", "approved_at", "mime", "sha256") if not d.get(k)]
        if missing:
            raise PublishError(f"publication {str(d.get('public_id'))[:64]!r} lacks {', '.join(missing)}")
        pid = str(d["public_id"])
        if not PUBLIC_ID.match(pid):
            raise PublishError("publication has an invalid public_id")
        mime = str(d["mime"]).split(";", 1)[0].strip().lower()
        if mime not in MEDIA_TYPES:
            raise PublishError(f"{pid}: unsupported mime {mime[:40]!r}")
        url = d.get("video_url") if MEDIA_TYPES[mime][0] == "reel" else d.get("image_url")
        if not url or not isinstance(url, str):
            raise PublishError(f"{pid}: no {'video_url' if MEDIA_TYPES[mime][0] == 'reel' else 'image_url'}")
        if not _sha_ok(d["sha256"]):
            raise PublishError(f"{pid}: invalid sha256")
        try:
            parse_time(d["approved_at"])
        except ValueError:
            raise PublishError(f"{pid}: invalid approved_at") from None
        links = d.get("links") if isinstance(d.get("links"), dict) else {}
        summary = links.get("summary") if isinstance(links.get("summary"), str) else None
        return cls(public_id=pid, character=str(d["character"]), caption=str(d.get("caption") or ""),
                   approved_at=str(d["approved_at"]), mime=mime, sha256=str(d["sha256"]).lower(), media_url=url,
                   summary_url=summary or f"{PUBLICATIONS_PATH}/{pid}/summary",
                   poster_url=(str(d["poster_url"]) if d.get("poster_url") else None),
                   start_source_sim=start_source_sim(d), credits=sanitize_credits(d.get(CREDITS_KEY)))


class ServiceClient:
    """The only code that talks to the service's publish API."""

    def __init__(self, settings: ServiceSettings, transport: httpx.BaseTransport | None = None, timeout: float = 60):
        self.settings = settings
        self.origin = _origin(settings.base_url)
        self.http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self.http.close()

    def url(self, u: str) -> str:
        return urljoin(self.settings.base_url + "/", u)

    def _headers(self, url: str) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if _origin(url) == self.origin:            # URLs elsewhere get no key
            h["Authorization"] = f"Bearer {self.settings.key}"
        return h

    def _fail(self, what: str, r: httpx.Response) -> PublishError:
        detail = ""
        try:
            err = r.json().get("error") or {}
            detail = ": " + " ".join(str(err[k]) for k in ("code", "message") if err.get(k))
        except Exception:
            pass
        return PublishError(f"{what}: HTTP {r.status_code}{detail}")

    def get_json(self, url: str, what: str, params: dict[str, str] | None = None) -> Any:
        url = self.url(url)
        try:
            r = self.http.get(url, params=params, headers=self._headers(url))
        except httpx.HTTPError as e:
            raise PublishError(f"{what}: {type(e).__name__}") from None
        if r.status_code != 200:
            raise self._fail(what, r)
        try:
            return r.json()
        except ValueError:
            raise PublishError(f"{what}: not JSON") from None

    def publications(self, since: str | None = None) -> tuple[list[Any], list[Any], str | None]:
        """(raw publications, raw revocations, cursor) for everything after `since` (all without it),
        following `next_since` until a page is empty. Items are validated one by one by the caller."""
        pubs: list[Any] = []
        revoked: list[Any] = []
        cursor = since
        for _ in range(MAX_PAGES):
            data = self.get_json(PUBLICATIONS_PATH, "publications", {"since": cursor} if cursor else None)
            if not isinstance(data, dict) or not isinstance(data.get("publications"), list):
                raise PublishError("publications: no publications array")
            page_p, page_r = data["publications"], data.get("revoked") or []
            if not isinstance(page_r, list):
                raise PublishError("publications: revoked is not an array")
            pubs += page_p
            revoked += page_r
            nxt = data.get("next_since")
            nxt = nxt if isinstance(nxt, str) and nxt else None
            if nxt is not None:
                try:
                    parse_time(nxt)
                except ValueError:
                    raise PublishError("publications: invalid next_since") from None
            if not (page_p or page_r):
                return pubs, revoked, nxt or cursor
            if nxt is None or nxt == cursor:
                return pubs, revoked, None       # no usable cursor: the next pull is a full one
            cursor = nxt
        raise PublishError(f"publications: still not exhausted after {MAX_PAGES} pages")

    def summary(self, pub: Publication) -> dict:
        s = self.get_json(pub.summary_url, f"{pub.public_id}: summary")
        if not isinstance(s, dict):
            raise PublishError(f"{pub.public_id}: summary is not an object")
        return s

    def download(self, url: str, dest: Path, what: str, sha256: str | None = None) -> None:
        """Stream to dest; verify Content-Length and (when given) sha256."""
        url = self.url(url)
        h = hashlib.sha256()
        n = 0
        try:
            with self.http.stream("GET", url, headers={**self._headers(url), "Accept": "*/*"}) as r:
                if r.status_code != 200:
                    r.read()
                    raise self._fail(what, r)
                expected = r.headers.get("content-length")
                with dest.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        n += len(chunk)
                        if n > MAX_MEDIA_BYTES:
                            raise PublishError(f"{what}: larger than {MAX_MEDIA_BYTES} bytes")
                        h.update(chunk)
                        f.write(chunk)
        except httpx.HTTPError as e:
            raise PublishError(f"{what}: {type(e).__name__}") from None
        if n == 0:
            raise PublishError(f"{what}: empty file")
        if expected is not None and expected.isdigit() and int(expected) != n:
            raise PublishError(f"{what}: got {n} of {expected} bytes")
        if sha256 and h.hexdigest() != sha256:
            raise PublishError(f"{what}: sha256 mismatch")


def audio_only(d: Any) -> bool:
    """An audio-only publication (`audio_url` and no video/image, or an audio/* mime): not a post (yet)."""
    if not isinstance(d, dict):
        return False
    mime = d.get("mime") if isinstance(d.get("mime"), str) else ""
    if mime.split(";", 1)[0].strip().lower().startswith("audio/"):
        return True
    return bool(d.get("audio_url")) and not d.get("video_url") and not d.get("image_url")


def _origin(url: str) -> tuple[str, str]:
    p = urlsplit(url)
    return p.scheme.lower(), p.netloc.lower()


# --- helpers -------------------------------------------------------------------------------------

def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, REDACTED)
    return text


SUMMARY_DROP = ("public_id", "post_id", "id", "media", "links", "summary_version", CREDITS_KEY)
INPUT_KEEP = ("label", "role", "position", "strength", "frame_idx", "at_seconds")


def _input(x: Any) -> dict:
    """A summary input: a reference to its own publication when the service marks it published,
    else a private input (role kept). Hashes and media URLs are not stored."""
    d = x if isinstance(x, dict) else {}
    spid = d.get("source_public_id")
    if d.get("published") is True and isinstance(spid, str) and PUBLIC_ID.match(spid):
        return {**(sanitize({k: d.get(k) for k in INPUT_KEEP}) or {}), "public_id": spid}
    role = sanitize(d.get("role")) if isinstance(d.get("role"), str) else None
    return {**({"role": role} if role else {}), "private": True, "label": PRIVATE_INPUT}


def public_record(summary: dict, pub: Publication) -> dict:
    """generation_json for the post: the service summary sanitized again, ids/URLs that belong to the
    service removed, inputs reduced to public references, model_label filled from the model,
    start_source_sim set when the summary's start_source field (or a segment's) says so (only true is stored),
    `credits` always a list (sanitize_credits; the summary's, else the list item's, else [])."""
    inputs = summary.get("inputs")
    s = sanitize({k: v for k, v in summary.items() if k not in SUMMARY_DROP and k != "inputs"}) or {}
    if inputs is not None:
        s["inputs"] = [_input(x) for x in (inputs if isinstance(inputs, list) else [inputs])]
    label = model_short_label(s.get("model_label")) or model_short_label(s.get("model"))
    if label:
        s["model_label"] = label
    else:
        s.pop("model_label", None)
    if pub.start_source_sim or start_source_sim(summary):
        s[START_SOURCE_KEY] = True
    else:
        s.pop(START_SOURCE_KEY, None)
    raw_credits = summary.get(CREDITS_KEY)
    s[CREDITS_KEY] = sanitize_credits(raw_credits) if isinstance(raw_credits, list) else list(pub.credits)
    return s


def _content_ok(path: Path, mime: str) -> bool:
    with path.open("rb") as f:
        head = f.read(16)
    if mime == "video/mp4":
        return len(head) >= 8 and head[4:8] == b"ftyp"
    if mime == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if mime == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    return False


def _is_jpeg(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(3) == b"\xff\xd8\xff"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def make_poster(src: Path, dest: Path, *, from_video: bool) -> bool:
    """540 px wide JPEG from a video frame or a downloaded poster image; False when ffmpeg is missing/fails."""
    if not ffmpeg_available():
        return False
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if from_video:
        cmd += ["-ss", "0.5"]
    cmd += ["-i", str(src), "-frames:v", "1", "-vf", f"scale={POSTER_WIDTH}:-2", "-q:v", "4", str(dest)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        return False
    return dest.is_file() and dest.stat().st_size > 0


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- publish -------------------------------------------------------------------------------------

def _character(con: sqlite3.Connection, slug: str) -> int:
    row = con.execute("SELECT id, kind FROM accounts WHERE slug=?", (slug.lstrip("@"),)).fetchone()
    if not row:
        raise PublishError(f"unknown character {slug!r} (create it first, e.g. seed-characters)")
    if row["kind"] not in config.CHARACTER_KINDS:
        raise PublishError(f"account {slug!r} is a {row['kind']}, not a posting character")
    return row["id"]


def _same(row: sqlite3.Row, fields: dict) -> bool:
    return all((jload(row[k], None) == jload(v, None)) if k == "generation_json" else row[k] == v
               for k, v in fields.items())


def _publish_item(con: sqlite3.Connection, client: ServiceClient, pub: Publication, *, dry_run: bool,
                  media_root: Path, thumb_root: Path) -> tuple[str, bool]:
    """Returns (new|updated|unchanged, poster_written)."""
    source = f"generation:{pub.public_id}"
    aid = _character(con, pub.character)
    clash = con.execute("SELECT source FROM posts WHERE public_id=?", (pub.public_id,)).fetchone()
    if clash and clash["source"] != source:
        raise PublishError(f"{pub.public_id}: public_id already belongs to another post")
    record = public_record(client.summary(pub), pub)
    row = con.execute("SELECT * FROM posts WHERE source=?", (source,)).fetchone()
    media_rel = f"gen/{pub.public_id}{pub.ext}"
    posted_at = row["posted_at"] if row is not None and row["posted_at"] else \
        parse_time(pub.approved_at).isoformat(timespec="seconds")       # first approval = post time
    fields = {"account_id": aid, "kind": pub.kind, "title": None, "caption": pub.caption, "media": media_rel,
              "poster": None, "posted_at": posted_at, "status": "published", "public_id": pub.public_id,
              "generation_json": jdump(record)}
    media_dest = media_root / media_rel
    poster_dest = thumb_root / str(POSTER_WIDTH) / "gen" / f"{pub.public_id}.jpg"
    have_media = media_dest.is_file() and _sha_file(media_dest) == pub.sha256
    if row is not None and _same(row, fields) and have_media:
        return "unchanged", False
    status = "new" if row is None else "updated"
    if dry_run:
        return status, False

    media_dest.parent.mkdir(parents=True, exist_ok=True)
    poster_dest.parent.mkdir(parents=True, exist_ok=True)
    # temp dirs next to the targets (same filesystem: atomic rename); hidden, so never served
    with tempfile.TemporaryDirectory(prefix=".incoming-", dir=media_dest.parent) as tmp, \
            tempfile.TemporaryDirectory(prefix=".incoming-", dir=poster_dest.parent) as ptmp:
        new_media = None
        if not have_media:
            new_media = Path(tmp) / ("media" + pub.ext)
            client.download(pub.media_url, new_media, f"{pub.public_id}: media", pub.sha256)
            if not _content_ok(new_media, pub.mime):
                raise PublishError(f"{pub.public_id}: media is not a {pub.mime} file")
        new_poster = None
        if new_media is not None or not poster_dest.is_file():
            cand = Path(ptmp) / "poster.jpg"
            ok = False
            if pub.poster_url:                     # the service poster first, the media itself as fallback
                raw = Path(tmp) / "poster.src"
                try:
                    client.download(pub.poster_url, raw, f"{pub.public_id}: poster")
                    if ffmpeg_available():
                        ok = make_poster(raw, cand, from_video=False)
                    elif _is_jpeg(raw):
                        os.replace(raw, cand)
                        ok = True
                except PublishError:
                    ok = False
            if not ok:
                ok = make_poster(new_media or media_dest, cand, from_video=pub.kind == "reel")
            new_poster = cand if ok else None

        backups: list[tuple[Path, Path | None]] = []
        try:
            upsert_post(con, source=source, **fields)
            for src, dst, bdir in ((new_media, media_dest, tmp), (new_poster, poster_dest, ptmp)):
                if src is None:
                    continue
                bak = None
                if dst.exists():
                    bak = Path(bdir) / ("old-" + dst.name)
                    os.replace(dst, bak)
                backups.append((dst, bak))
                os.replace(src, dst)
            con.commit()
        except BaseException:
            con.rollback()
            for dst, bak in reversed(backups):
                if bak is not None:
                    os.replace(bak, dst)
                else:
                    dst.unlink(missing_ok=True)
            raise
    return status, new_poster is not None


def _revoke(con: sqlite3.Connection, public_id: str, revoked_at: str | None, *, dry_run: bool) -> str:
    """revoked | unchanged (already unpublished) | unknown."""
    row = con.execute("SELECT id, status, meta_json FROM posts WHERE public_id=? AND source LIKE 'generation:%'",
                      (public_id,)).fetchone()
    if row is None:
        return "unknown"
    if row["status"] != "published":
        return "unchanged"
    if not dry_run:
        meta = jload(row["meta_json"], {}) or {}
        meta["revoked_at"] = revoked_at or now_iso()
        con.execute("UPDATE posts SET status='draft', meta_json=?, updated_at=? WHERE id=?",
                    (jdump(meta), now_iso(), row["id"]))
        con.commit()
    return "revoked"


def _later(a: str | None, b: str | None) -> bool:
    """a strictly after b (False when either is missing or unparsable)."""
    try:
        return a is not None and b is not None and parse_time(a) > parse_time(b)
    except ValueError:
        return False


def _merge(raw_pubs: list[Any], raw_revoked: list[Any], fail: Callable[[str], None]
           ) -> tuple[list[tuple[int, Any]], dict[str, str | None]]:
    """Latest state per public_id: (index, raw publication) to publish and {public_id: revoked_at}.
    Publications are upserts (the last listed wins); between an approval and a revocation of the same
    id the later timestamp wins, revoked on a tie or a missing/bad time."""
    revoked: dict[str, str | None] = {}
    for d in raw_revoked:
        pid = d.get("public_id") if isinstance(d, dict) else None
        if not isinstance(pid, str) or not PUBLIC_ID.match(pid):
            fail("revoked: entry without a valid public_id")
            continue
        at = str(d["revoked_at"]) if d.get("revoked_at") else None
        if pid not in revoked or _later(at, revoked[pid]):
            revoked[pid] = at
    pubs: dict[str, tuple[int, Any]] = {}
    for n, d in enumerate(raw_pubs):
        pid = d.get("public_id") if isinstance(d, dict) else None
        key = pid if isinstance(pid, str) and PUBLIC_ID.match(pid) else f"#{n}"
        pubs[key] = (n, d)
    for pid in list(pubs):
        if pid in revoked:
            if _later(pubs[pid][1].get("approved_at"), revoked[pid]):
                del revoked[pid]                       # re-approved after the revocation
            else:
                del pubs[pid]
    return sorted(pubs.values(), key=lambda t: t[0]), revoked


def _missing(con: sqlite3.Connection, raw_pubs: list[Any], revoked: dict[str, str | None]) -> list[str] | None:
    """Published generation posts whose public_id a full listing no longer mentions (neither active nor
    revoked), oldest first. None when the listing cannot be trusted for this (empty, or an entry without
    a valid public_id)."""
    listed: set[str] = set(revoked)
    for d in raw_pubs:
        pid = d.get("public_id") if isinstance(d, dict) else None
        if not isinstance(pid, str) or not PUBLIC_ID.match(pid):
            return None
        listed.add(pid)
    if not listed:
        return None
    rows = con.execute("SELECT public_id FROM posts WHERE status='published' AND source LIKE 'generation:%' "
                       "AND public_id IS NOT NULL ORDER BY id").fetchall()
    return [r[0] for r in rows if r[0] not in listed]


def pull_approved(con: sqlite3.Connection, settings: ServiceSettings, *, dry_run: bool = False, full: bool = False,
                  transport: httpx.BaseTransport | None = None, media_root: Path | None = None,
                  thumb_root: Path | None = None, log: Callable[[str], None] = lambda s: None) -> dict:
    """Publish every approved publication and unpublish every revoked one (since the stored cursor,
    or all of them with `full` / without a cursor).
    Returns {new, updated, unchanged, revoked, skipped, failed: [...], failed_ids: [...], listing_failed, notes: [...],
    items: [...], incremental, full}. `listing_failed` is True when the listing itself could not be read
    (nothing was processed); `failed_ids` are the public_ids of failed items (when the id was valid).
    A full listing also unpublishes published generation posts missing from it (see _missing) and,
    when nothing failed (and not dry_run), stores its time as LAST_FULL_KEY.
    Audio-only publications are skipped (counted in `skipped`, logged, no post).
    Items are independent: one failure does not block the others (but makes `failed` non-empty and
    keeps the cursor where it was)."""
    media_root = Path(media_root or config.MEDIA_ROOT)
    thumb_root = Path(thumb_root or config.THUMB_ROOT)
    since = None if full else get_meta(con, CURSOR_KEY)
    since = since if isinstance(since, str) and since else None
    client = ServiceClient(settings, transport)
    res: dict[str, Any] = {"new": 0, "updated": 0, "unchanged": 0, "revoked": 0, "skipped": 0, "failed": [],
                           "failed_ids": [], "listing_failed": False, "notes": [], "items": [], "dry_run": dry_run,
                           "incremental": since is not None, "full": since is None}

    def fail(msg: str, pid: Any = None) -> None:
        msg = _redact(msg, settings.key)
        res["failed"].append(msg)
        if isinstance(pid, str) and PUBLIC_ID.match(pid) and pid not in res["failed_ids"]:
            res["failed_ids"].append(pid)
        log(f"failed {msg}")

    try:
        try:
            raw_pubs, raw_revoked, cursor = client.publications(since)
        except PublishError as e:
            res["listing_failed"] = True
            fail(str(e))
            return res
        todo, revoked = _merge(raw_pubs, raw_revoked, fail)
        for n, d in todo:
            if audio_only(d):                  # not planned as posts: skip, no failure (the cursor moves on)
                pid = d.get("public_id")
                pid = pid if isinstance(pid, str) and PUBLIC_ID.match(pid) else f"#{n}"
                res["skipped"] += 1
                res["items"].append({"public_id": pid, "status": "skipped", "reason": "audio-only"})
                log(f"skipped {pid}: audio-only")
                continue
            try:
                pub = Publication.parse(d)
            except PublishError as e:
                fail(f"publication #{n}: {e}", d.get("public_id") if isinstance(d, dict) else None)
                continue
            try:
                status, poster = _publish_item(con, client, pub, dry_run=dry_run, media_root=media_root,
                                               thumb_root=thumb_root)
            except (PublishError, ValueError, OSError, sqlite3.Error) as e:
                con.rollback()
                fail(f"{pub.public_id}: {e}" if not str(e).startswith(pub.public_id) else str(e), pub.public_id)
                continue
            res[status] += 1
            res["items"].append({"public_id": pub.public_id, "kind": pub.kind, "status": status,
                                 **({"poster": poster} if status != "unchanged" and not dry_run else {})})
            log(f"{status} {pub.public_id}")
        revoke_list = list(revoked.items())
        if since is None:
            missing = _missing(con, raw_pubs, revoked)
            if missing is None:
                res["notes"].append("full listing is empty or has entries without a valid public_id; "
                                    "posts missing from it were not unpublished")
            else:
                for pid in missing:
                    res["notes"].append(f"{pid}: missing from the full listing, unpublished")
                    revoke_list.append((pid, None))
        for pid, at in revoke_list:
            try:
                r = _revoke(con, pid, at, dry_run=dry_run)
            except sqlite3.Error as e:
                con.rollback()
                fail(f"{pid}: revoke: {e}", pid)
                continue
            if r == "unknown":
                res["notes"].append(f"{pid}: revoked, but no such post (nothing to do)")
            elif r == "revoked":
                res["revoked"] += 1
                res["items"].append({"public_id": pid, "status": "revoked"})
                log(f"revoked {pid}")
        if not dry_run and not res["failed"]:
            if cursor and cursor != since:
                set_meta(con, CURSOR_KEY, cursor)
            if since is None:
                set_meta(con, LAST_FULL_KEY, now_iso())
            con.commit()
    finally:
        client.close()
    return res
