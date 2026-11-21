"""Media access: whitelisted paths inside the configured media/thumb/avatar roots.

Media are already web-sized (the public export), so nothing is re-encoded and ffmpeg is not needed:
posters are served from pre-rendered thumbnails (<thumb root>/<width>/<path>.jpg).
"""
from __future__ import annotations

from pathlib import Path

from . import config


class MediaError(Exception):
    pass


def safe_path(root: Path, rel: str, *, exts: set[str] = config.MEDIA_EXT) -> Path:
    """Resolve `rel` inside `root`; refuse traversal, hidden parts, other file types, missing files."""
    if not rel or "\x00" in rel:
        raise MediaError("empty path")
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise MediaError("path not allowed")
    base = root.resolve()
    p = (base / "/".join(parts)).resolve()
    try:
        p.relative_to(base)
    except ValueError as e:
        raise MediaError("path outside media root") from e
    if p.suffix.lower() not in exts:
        raise MediaError("file type not allowed")
    if not p.is_file():
        raise MediaError("not found")
    return p


def media_file(rel: str) -> Path:
    return safe_path(config.MEDIA_ROOT, rel)


def thumb_file(width: int, rel: str) -> Path:
    """Pre-rendered poster for media `rel` (or an explicit poster path) at `width`."""
    return safe_path(config.THUMB_ROOT / str(int(width)), str(Path(rel).with_suffix(".jpg")), exts={".jpg"})


def avatar_file(rel: str) -> Path:
    return safe_path(config.AVATAR_ROOT, rel, exts=config.IMAGE_EXT)


def media_kind(rel: str | None) -> str | None:
    if not rel:
        return None
    ext = Path(rel).suffix.lower()
    if ext in config.VIDEO_EXT:
        return "video"
    if ext in config.IMAGE_EXT:
        return "image"
    if ext in config.AUDIO_EXT:
        return "audio"
    return None


def media_exists(rel: str | None) -> bool:
    if not rel:
        return False
    try:
        media_file(rel)
    except MediaError:
        return False
    return True
