"""`weide pull-approved`: fetch generations approved in the generation service and publish them as posts.

The service's publish API is not final yet, so everything that knows its shape lives in `ServiceClient`
(one small adapter). Assumed shape (read-only key with scope `publish`):

  GET {base}/api/v1/publish/approved
      -> {"items": [{public_id, run_id, character_slug, caption, title?, approved_at,
                     media_url, poster_url?, summary_url, sha256?, status?}],
          "revoked": [{public_id, revoked_at}]}
      An item with status "revoked" counts like an entry in `revoked`.
  GET summary_url  -> public generation summary JSON (sanitized by the service; we sanitize again)
  GET media_url    -> the final video (streamed to disk; Content-Length and sha256 verified when given)

Relative URLs are resolved against the base URL. The bearer key is only sent to the service's own
origin (signed media URLs on another host get no key) and is never printed: every error text goes
through `_redact`. Settings come from the environment or from a gitignored `.env` in the private dir:
  HENHOUSE_SERVICE_URL   base URL of the service
  HENHOUSE_SERVICE_KEY   bearer key (scope `publish`)

Each item becomes one post (kind reel, status published: approval happened in the service) matched by
source `generation:<run_id>`, so a rerun never duplicates; caption/title/summary changes update it.
The character account must already exist. Media go to <media root>/gen/<public_id>.mp4, a 540 px
poster to <thumb root>/540/gen/<public_id>.jpg: the service poster (scaled with ffmpeg, or kept as is
when it is a JPEG and ffmpeg is missing), else a frame of the video (ffmpeg; skipped without it).
Every item is validated on its own, fetched into a temp dir and written in one DB transaction; a failed
item leaves no post and no file behind and does not stop the others.

Revoked public_ids are unpublished: the post goes back to status draft (meta `revoked_at` notes why), so
it vanishes from the API and the export; the media file stays on disk. Unknown ids are a no-op.
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
from .db import jdump, jload, now_iso, parse_time, upsert_post
from .generation import PUBLIC_ID, REDACTED, sanitize

APPROVED_PATH = "/api/v1/publish/approved"
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

@dataclass
class ApprovedItem:
    public_id: str
    run_id: str
    character_slug: str
    caption: str
    approved_at: str
    media_url: str
    summary_url: str
    title: str | None = None
    poster_url: str | None = None
    sha256: str | None = None

    @classmethod
    def parse(cls, d: Any) -> "ApprovedItem":
        if not isinstance(d, dict):
            raise PublishError("approved item is not an object")
        missing = [k for k in ("public_id", "run_id", "character_slug", "approved_at", "media_url", "summary_url")
                   if not d.get(k)]
        if missing:
            raise PublishError(f"approved item {d.get('public_id')!r} lacks {', '.join(missing)}")
        if not PUBLIC_ID.match(str(d["public_id"])):
            raise PublishError("approved item has an invalid public_id")
        sha = d.get("sha256")
        if sha is not None and not (isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha.lower())):
            raise PublishError(f"{d['public_id']}: invalid sha256")
        return cls(public_id=str(d["public_id"]), run_id=str(d["run_id"]), character_slug=str(d["character_slug"]),
                   caption=str(d.get("caption") or ""), approved_at=str(d["approved_at"]),
                   media_url=str(d["media_url"]), summary_url=str(d["summary_url"]),
                   title=(str(d["title"]) if d.get("title") else None),
                   poster_url=(str(d["poster_url"]) if d.get("poster_url") else None),
                   sha256=(sha.lower() if sha else None))


class ServiceClient:
    """The only code that knows the service's publish API (adapt here when it is final)."""

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
        if _origin(url) == self.origin:            # signed URLs elsewhere get no key
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

    def get_json(self, url: str, what: str) -> Any:
        url = self.url(url)
        try:
            r = self.http.get(url, headers=self._headers(url))
        except httpx.HTTPError as e:
            raise PublishError(f"{what}: {type(e).__name__}") from None
        if r.status_code != 200:
            raise self._fail(what, r)
        try:
            return r.json()
        except ValueError:
            raise PublishError(f"{what}: not JSON") from None

    def approved(self) -> tuple[list[Any], list[Any]]:
        """(raw items, raw revocations); items are validated one by one by the caller."""
        data = self.get_json(APPROVED_PATH, "approved list")
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise PublishError("approved list: no items array")
        revoked = data.get("revoked") or []
        if not isinstance(revoked, list):
            raise PublishError("approved list: revoked is not an array")
        return items, revoked

    def summary(self, item: ApprovedItem) -> dict:
        s = self.get_json(item.summary_url, f"{item.public_id}: summary")
        if not isinstance(s, dict):
            raise PublishError(f"{item.public_id}: summary is not an object")
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


def _origin(url: str) -> tuple[str, str]:
    p = urlsplit(url)
    return p.scheme.lower(), p.netloc.lower()


# --- helpers -------------------------------------------------------------------------------------

def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, REDACTED)
    return text


def _scrub(obj: Any, needles: tuple[str, ...]) -> Any:
    """Drop every string that mentions one of `needles` (the run id) from a summary."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(n in str(k) for n in needles):
                continue
            v = _scrub(v, needles)
            if v is not None:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [x for x in (_scrub(v, needles) for v in obj) if x is not None]
    if isinstance(obj, str) and any(n in obj for n in needles):
        return None
    return obj


def public_record(summary: dict, item: ApprovedItem) -> dict:
    """generation_json for the post: the summary sanitized again, ids that belong elsewhere removed,
    the run id scrubbed wherever it appears, model_label filled from the model when missing."""
    s = sanitize({k: v for k, v in summary.items() if k not in ("public_id", "post_id", "id")}) or {}
    s = _scrub(s, (item.run_id,)) if len(item.run_id) >= 4 else s
    if not s.get("model_label"):
        m = s.get("model")
        name = m.get("name") if isinstance(m, dict) else m if isinstance(m, str) else None
        ver = (m.get("version") if isinstance(m, dict) else None) or s.get("version")
        if name:
            s["model_label"] = f"{name} {ver}".strip() if ver else str(name)
    return s


def _is_mp4(path: Path) -> bool:
    with path.open("rb") as f:
        head = f.read(12)
    return len(head) >= 8 and head[4:8] == b"ftyp"


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


def _fields(item: ApprovedItem, aid: int, record: dict, media_rel: str) -> dict:
    return {"account_id": aid, "kind": "reel", "title": item.title, "caption": item.caption, "media": media_rel,
            "poster": None, "posted_at": parse_time(item.approved_at).isoformat(timespec="seconds"),
            "status": "published", "public_id": item.public_id, "generation_json": jdump(record)}


def _same(row: sqlite3.Row, fields: dict) -> bool:
    return all((jload(row[k], None) == jload(v, None)) if k == "generation_json" else row[k] == v
               for k, v in fields.items())


def _publish_item(con: sqlite3.Connection, client: ServiceClient, item: ApprovedItem, *, dry_run: bool,
                  media_root: Path, thumb_root: Path) -> tuple[str, bool]:
    """Returns (new|updated|unchanged, poster_written)."""
    source = f"generation:{item.run_id}"
    aid = _character(con, item.character_slug)
    clash = con.execute("SELECT source FROM posts WHERE public_id=?", (item.public_id,)).fetchone()
    if clash and clash["source"] != source:
        raise PublishError(f"{item.public_id}: public_id already belongs to another post")
    record = public_record(client.summary(item), item)
    media_rel = f"gen/{item.public_id}.mp4"
    fields = _fields(item, aid, record, media_rel)
    row = con.execute("SELECT * FROM posts WHERE source=?", (source,)).fetchone()
    media_dest = media_root / media_rel
    poster_dest = thumb_root / str(POSTER_WIDTH) / "gen" / f"{item.public_id}.jpg"
    have_media = media_dest.is_file() and (item.sha256 is None or _sha_file(media_dest) == item.sha256)
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
            new_media = Path(tmp) / "media.mp4"
            client.download(item.media_url, new_media, f"{item.public_id}: media", item.sha256)
            if not _is_mp4(new_media):
                raise PublishError(f"{item.public_id}: media is not an MP4 file")
        new_poster = None
        if new_media is not None or not poster_dest.is_file():
            cand = Path(ptmp) / "poster.jpg"
            ok = False
            if item.poster_url:                    # the service poster first, a video frame as fallback
                raw = Path(tmp) / "poster.src"
                try:
                    client.download(item.poster_url, raw, f"{item.public_id}: poster")
                    if ffmpeg_available():
                        ok = make_poster(raw, cand, from_video=False)
                    elif _is_jpeg(raw):
                        os.replace(raw, cand)
                        ok = True
                except PublishError:
                    ok = False
            if not ok:
                ok = make_poster(new_media or media_dest, cand, from_video=True)
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


def _revocations(raw_items: list[Any], raw_revoked: list[Any], failed: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for src, entries in (("revoked", raw_revoked), ("items", [d for d in raw_items
                                                               if isinstance(d, dict) and d.get("status") == "revoked"])):
        for d in entries:
            pid = d.get("public_id") if isinstance(d, dict) else None
            if not isinstance(pid, str) or not PUBLIC_ID.match(pid):
                failed.append(f"{src}: revocation without a valid public_id")
                continue
            out.setdefault(pid, str(d["revoked_at"]) if d.get("revoked_at") else None)
    return out


def pull_approved(con: sqlite3.Connection, settings: ServiceSettings, *, dry_run: bool = False,
                  transport: httpx.BaseTransport | None = None, media_root: Path | None = None,
                  thumb_root: Path | None = None, log: Callable[[str], None] = lambda s: None) -> dict:
    """Publish every approved item and unpublish every revoked one.
    Returns {new, updated, unchanged, revoked, failed: [...], notes: [...], items: [...]}.
    Items are independent: one failure does not block the others (but makes `failed` non-empty)."""
    media_root = Path(media_root or config.MEDIA_ROOT)
    thumb_root = Path(thumb_root or config.THUMB_ROOT)
    client = ServiceClient(settings, transport)
    res: dict[str, Any] = {"new": 0, "updated": 0, "unchanged": 0, "revoked": 0, "failed": [], "notes": [],
                           "items": [], "dry_run": dry_run}

    def fail(msg: str, *secrets: str) -> None:
        msg = _redact(msg, settings.key, *secrets)
        res["failed"].append(msg)
        log(f"failed {msg}")

    try:
        try:
            raw_items, raw_revoked = client.approved()
        except PublishError as e:
            fail(str(e))
            return res
        revoked = _revocations(raw_items, raw_revoked, res["failed"])
        seen: set[str] = set()
        for n, d in enumerate(raw_items):
            if isinstance(d, dict) and d.get("status") == "revoked":
                continue
            try:
                if isinstance(d, dict) and d.get("status") not in (None, "approved"):
                    raise PublishError(f"item #{n}: unknown status {str(d['status'])[:40]!r}")
                item = ApprovedItem.parse(d)
            except PublishError as e:
                rid = d.get("run_id") if isinstance(d, dict) and isinstance(d.get("run_id"), str) else ""
                fail(f"item #{n}: {e}" if not str(e).startswith("item #") else str(e), *([rid] if len(rid) >= 4 else []))
                continue
            if item.public_id in seen:
                fail(f"{item.public_id}: listed twice")
                continue
            seen.add(item.public_id)
            if item.public_id in revoked:
                continue                                   # revoked wins over a stale approval
            try:
                status, poster = _publish_item(con, client, item, dry_run=dry_run, media_root=media_root,
                                               thumb_root=thumb_root)
            except (PublishError, ValueError, OSError, sqlite3.Error) as e:
                con.rollback()
                fail(f"{item.public_id}: {e}" if not str(e).startswith(item.public_id) else str(e), item.run_id)
                continue
            res[status] += 1
            res["items"].append({"public_id": item.public_id, "status": status,
                                 **({"poster": poster} if status != "unchanged" and not dry_run else {})})
            log(f"{status} {item.public_id}")
        for pid, at in revoked.items():
            try:
                r = _revoke(con, pid, at, dry_run=dry_run)
            except sqlite3.Error as e:
                con.rollback()
                fail(f"{pid}: revoke: {e}")
                continue
            if r == "unknown":
                res["notes"].append(f"{pid}: revoked, but no such post (nothing to do)")
            elif r == "revoked":
                res["revoked"] += 1
                res["items"].append({"public_id": pid, "status": "revoked"})
                log(f"revoked {pid}")
    finally:
        client.close()
    return res
