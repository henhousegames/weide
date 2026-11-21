"""Read-only adapter for the generation service's review API (used by the private preview only).

The review API is not final yet, so everything that knows its shape lives here (`ReviewClient`,
`ReviewItem.parse`). Assumed shape (bearer key with the read-only scope `review`):

  GET {base}/api/v1/review/generations[?since=<cursor>]
      -> {"items": [{id, status, task, model, model_label?, labels{}, prompt_original,
                     created_at?, finished_at?, updated_at?,
                     output: {kind: video|image, mime?, ...} | null,
                     media: {output: url, poster: url?}          (or top-level output_url / poster_url),
                     publication: null | {public_id, character, caption, approved_at, revoked_at},
                     lab_path?}],
          "next_since": <opaque cursor>}
      ("generations" is accepted instead of "items", "since" instead of "next_since".)
  GET <media url> (Range supported) -> the output video/image or the poster.

Without `since` the answer is the full current set; with it, only items changed after the cursor (upserts).
Relative URLs are resolved against the base URL. The key is only ever sent to the service's own origin
and never appears in errors, logs or responses. This module never imports the publishing code and
never reads HENHOUSE_SERVICE_KEY (the publish key).

Settings (environment only, e.g. a private systemd EnvironmentFile):
  HENHOUSE_SERVICE_URL   base URL of the service (without /api/v1)
  HENHOUSE_REVIEW_KEY    bearer key with scope `review` (read-only)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from . import config

GENERATIONS_PATH = "/api/v1/review/generations"
DRAFT_STATUS = "ready_for_review"
HIDDEN_STATUSES = {"failed", "cancelled", "canceled", "unavailable"}
GEN_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ReviewError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status        # HTTP status of the service, None when it was not reached


def _origin(url: str) -> tuple[str, str, int | None]:
    u = urlsplit(url)
    return u.scheme, (u.hostname or "").lower(), u.port or {"http": 80, "https": 443}.get(u.scheme)


@dataclass(frozen=True)
class ReviewSettings:
    base_url: str
    key: str = field(repr=False)

    @classmethod
    def load(cls) -> "ReviewSettings | None":
        """None when the service is not configured (the preview then shows published posts only)."""
        base, key = config.env("SERVICE_URL"), config.env("REVIEW_KEY")
        if not base or not key:
            return None
        if urlsplit(base).scheme not in ("http", "https"):
            raise ReviewError(f"{config.ENV_PREFIX}SERVICE_URL must be an http(s) URL")
        return cls(base.rstrip("/"), key)


def _str(v: Any) -> str | None:
    return str(v) if v not in (None, "") else None


@dataclass
class Publication:
    public_id: str
    character: str | None
    caption: str | None
    approved_at: str | None
    revoked_at: str | None

    @property
    def active(self) -> bool:
        return not self.revoked_at

    @classmethod
    def parse(cls, d: Any) -> "Publication | None":
        if not isinstance(d, dict) or not _str(d.get("public_id")):
            return None
        revoked = _str(d.get("revoked_at")) or ("revoked" if d.get("status") == "revoked" else None)
        return cls(public_id=str(d["public_id"]), character=_str(d.get("character")), caption=_str(d.get("caption")),
                   approved_at=_str(d.get("approved_at")), revoked_at=revoked)


@dataclass
class ReviewItem:
    id: str
    status: str
    task: str | None
    model: str | None
    model_label: str | None
    labels: dict
    prompt: str | None
    created_at: str | None
    finished_at: str | None
    output_kind: str | None            # video | image | None
    output_url: str | None
    poster_url: str | None
    publication: Publication | None

    @property
    def visible(self) -> bool:
        return self.status not in HIDDEN_STATUSES

    @classmethod
    def parse(cls, d: Any) -> "ReviewItem":
        if not isinstance(d, dict):
            raise ReviewError("review item is not an object")
        gid = _str(d.get("id"))
        if not gid or not GEN_ID.match(gid):
            raise ReviewError("review item has no valid id")
        media = d.get("media") if isinstance(d.get("media"), dict) else {}
        output = d.get("output") if isinstance(d.get("output"), dict) else {}
        out_url = _str(media.get("output")) or _str(d.get("output_url")) or _str(output.get("url"))
        poster = _str(media.get("poster")) or _str(d.get("poster_url")) or _str(output.get("poster_url"))
        kind = _str(output.get("kind")) or _str(output.get("type"))
        mime = _str(output.get("mime")) or _str(output.get("content_type")) or ""
        if kind not in ("video", "image"):
            kind = ("video" if mime.startswith("video/") else "image" if mime.startswith("image/")
                    else "image" if d.get("task") == "t2i" else "video" if out_url else None)
        labels = d.get("labels") if isinstance(d.get("labels"), dict) else {}
        return cls(id=gid, status=str(d.get("status") or ""), task=_str(d.get("task")), model=_str(d.get("model")),
                   model_label=_str(d.get("model_label")), labels=labels,
                   prompt=_str(d.get("prompt_original")) or _str(d.get("prompt")),
                   created_at=_str(d.get("created_at")),
                   finished_at=_str(d.get("finished_at")) or _str(d.get("updated_at")),
                   output_kind=kind, output_url=out_url, poster_url=poster,
                   publication=Publication.parse(d.get("publication")))


class ReviewClient:
    """The only code that talks to the service's review API (adapt here when it is final)."""

    def __init__(self, settings: ReviewSettings, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 30):
        self.settings = settings
        self.origin = _origin(settings.base_url)
        self.http = httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False)

    async def aclose(self) -> None:
        await self.http.aclose()

    def redact(self, text: str) -> str:
        return text.replace(self.settings.key, "[key]") if self.settings.key else text

    def url(self, ref: str) -> str:
        u = urljoin(self.settings.base_url + "/", ref)
        if urlsplit(u).scheme not in ("http", "https"):
            raise ReviewError("media URL must be http(s)")
        return u

    def _headers(self, url: str) -> dict[str, str]:
        # the key only goes to the service itself, never to another host (e.g. signed storage URLs)
        return {"Authorization": f"Bearer {self.settings.key}"} if _origin(url) == self.origin else {}

    async def generations(self, since: str | None = None) -> tuple[list[dict], str | None]:
        url = self.settings.base_url + GENERATIONS_PATH
        try:
            r = await self.http.get(url, params={"since": since} if since else None, headers=self._headers(url))
        except httpx.HTTPError as e:
            raise ReviewError(self.redact(f"review API unreachable: {type(e).__name__}")) from None
        if r.status_code != 200:
            raise ReviewError(f"review API answered HTTP {r.status_code}", r.status_code)
        try:
            data = r.json()
        except ValueError:
            raise ReviewError("review API answered no JSON") from None
        if not isinstance(data, dict):
            raise ReviewError("review API answered an unexpected shape")
        items = data.get("items", data.get("generations"))
        if not isinstance(items, list):
            raise ReviewError("review API answer has no item list")
        cursor = data.get("next_since", data.get("since"))
        return items, (str(cursor) if cursor not in (None, "") else None)

    async def open_media(self, ref: str, range_header: str | None = None) -> httpx.Response:
        """Start streaming a media URL (caller must close the response). Range is passed through."""
        url = self.url(ref)
        headers = self._headers(url)
        if range_header:
            headers["Range"] = range_header
        try:
            req = self.http.build_request("GET", url, headers=headers)
            return await self.http.send(req, stream=True)
        except httpx.HTTPError as e:
            raise ReviewError(self.redact(f"media fetch failed: {type(e).__name__}")) from None
