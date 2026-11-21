"""Read-only adapter for the generation service's review API (used by the private preview only).

Everything that knows the API's shape lives here (`ReviewClient`, `ReviewItem.parse`). Bearer key with the
read-only scope `review` (see the service's docs/API.md, "Review (private preview)"):

  GET {base}/api/v1/review/generations[?since=<finished_at>&limit=<n>]
      -> {"items": [{id, status: ready_for_review|published, created_at, finished_at, task,
                     model: {id, display_name}, labels: {}, prompt_original,
                     output: {mime, width, height, fps, duration_s, has_audio, sha256},   (images: fps/duration_s null)
                     media: {output: "/api/v1/review/generations/<id>/media/output", poster: ".../media/poster"},
                     publication: null | {public_id, character, caption, approved_at, revoked_at},
                     lab_path: "/#/g/<id>",
                     compose: {parents: [<generation id> | {...}]}   (task "compose" only)}],
          "next_since": <finished_at of the last item>, "has_more": bool}
  GET <media url> (Range supported) -> the output video/image or the poster.

Ordered by (finished_at, id); `next_since` is passed back as `since` and pages are fetched while `has_more`.
`status` is not sent (the default is both listable statuses). The cursor follows finished_at, so a later
approval or revocation of an item already seen does not come back: the caller re-reads the full list now
and then. Relative URLs are resolved against the base URL. The key is only ever sent to the service's own
origin and never appears in errors, logs or responses. This module never imports the publishing code and
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
PUBLISHED_STATUS = "published"
LISTED_STATUSES = {DRAFT_STATUS, PUBLISHED_STATUS}    # the only statuses the API lists; anything else is ignored
PAGE_LIMIT = 500                  # the API's maximum page size
MAX_PAGES = 1000                  # guard against a cursor that never ends
LAB_PATH = re.compile(r"^/#/g/[A-Za-z0-9_-]{1,80}$")
GEN_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
START_SOURCE = "start_source"
PROVENANCE_KEYS = ("compose", "parents", "effective")   # where a compose names its parents (kept for the note)
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
        return cls(public_id=str(d["public_id"]), character=_str(d.get("character")), caption=_str(d.get("caption")),
                   approved_at=_str(d.get("approved_at")), revoked_at=_str(d.get("revoked_at")))


@dataclass
class ReviewItem:
    id: str
    status: str
    task: str | None
    model: str | None                  # model id
    model_label: str | None            # model display_name
    labels: dict
    prompt: str | None
    created_at: str | None
    finished_at: str | None
    output_kind: str | None            # video | image | None
    output_url: str | None
    poster_url: str | None
    publication: Publication | None
    lab_path: str | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    has_audio: bool | None = None
    fps: float | None = None
    provenance: dict = field(default_factory=dict)   # start_source, labels + parent refs (review_start_source_sim)

    @property
    def visible(self) -> bool:
        return self.status in LISTED_STATUSES

    @classmethod
    def parse(cls, d: Any) -> "ReviewItem":
        if not isinstance(d, dict):
            raise ReviewError("review item is not an object")
        gid = _str(d.get("id"))
        if not gid or not GEN_ID.match(gid):
            raise ReviewError("review item has no valid id")
        media = d.get("media") if isinstance(d.get("media"), dict) else {}
        output = d.get("output") if isinstance(d.get("output"), dict) else {}
        model = d.get("model")
        model_id, model_label = ((_str(model.get("id")), _str(model.get("display_name"))) if isinstance(model, dict)
                                 else (_str(model), None))
        out_url, poster = _str(media.get("output")), _str(media.get("poster"))
        mime = _str(output.get("mime")) or ""
        kind = ("video" if mime.startswith("video/") else "image" if mime.startswith("image/")
                else "image" if d.get("task") == "t2i" else "video" if out_url else None)
        labels = d.get("labels") if isinstance(d.get("labels"), dict) else {}
        lab_path = _str(d.get("lab_path"))
        provenance = {"labels": labels, **{k: d[k] for k in PROVENANCE_KEYS if d.get(k) is not None}}
        if START_SOURCE in d:              # the service's field (also null): decides over labels/parents
            provenance[START_SOURCE] = d[START_SOURCE]
        return cls(id=gid, status=str(d.get("status") or ""), task=_str(d.get("task")), model=model_id,
                   model_label=model_label, labels=labels, prompt=_str(d.get("prompt_original")),
                   created_at=_str(d.get("created_at")), finished_at=_str(d.get("finished_at")),
                   output_kind=kind, output_url=out_url, poster_url=poster,
                   publication=Publication.parse(d.get("publication")),
                   lab_path=lab_path if lab_path and LAB_PATH.match(lab_path) else None,
                   width=_num(output.get("width"), int), height=_num(output.get("height"), int),
                   duration_s=_num(output.get("duration_s"), float), fps=_num(output.get("fps"), float),
                   has_audio=output.get("has_audio") if isinstance(output.get("has_audio"), bool) else None,
                   provenance=provenance)


def _num(v: Any, typ: type) -> Any:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return typ(v)


class ReviewClient:
    """The only code that talks to the service's review API."""

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

    async def _page(self, since: str | None) -> tuple[list, str | None, bool]:
        url = self.settings.base_url + GENERATIONS_PATH
        params = {"limit": str(PAGE_LIMIT)}
        if since:
            params["since"] = since
        try:
            r = await self.http.get(url, params=params, headers=self._headers(url))
        except httpx.HTTPError as e:
            raise ReviewError(self.redact(f"review API unreachable: {type(e).__name__}")) from None
        if r.status_code != 200:
            raise ReviewError(f"review API answered HTTP {r.status_code}", r.status_code)
        try:
            data = r.json()
        except ValueError:
            raise ReviewError("review API answered no JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ReviewError("review API answer has no item list")
        cursor = data.get("next_since")
        return data["items"], (str(cursor) if cursor not in (None, "") else since), data.get("has_more") is True

    async def generations(self, since: str | None = None) -> tuple[list[dict], str | None]:
        """All items finished after `since` (None: the full list), paging while `has_more`.
        Returns the raw items and the cursor for the next poll."""
        items: list = []
        cursor = since
        for _ in range(MAX_PAGES):
            page, nxt, more = await self._page(cursor)
            items += page
            if not more:
                return items, nxt
            if nxt == cursor:
                raise ReviewError("review API cursor does not advance")
            cursor = nxt
        raise ReviewError("review API has too many pages")

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
