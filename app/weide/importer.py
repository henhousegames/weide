"""Import a public static export (data/*.json, as written by export-static) into the database.

Idempotent: accounts are matched by slug, posts by source (or id), comments by id; post and comment
ids from the export are kept, so links (#/p/<id>) stay valid. Imported posts are `published` and
carry no generation data. Re-running the import never duplicates rows; reaction totals are restored
through an `import` bucket per (post, type) that tops up whatever other reactions exist.

What the public JSON does not contain is not invented: follow edges are unknown, so the public
"following" count is kept in profile_json (`public_following`) and shown when no edges exist; the
single named reaction behind `uwe_reacted` is stored as one `lol` (first type with a count > 0).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import config
from .db import (add_named_reaction, check_account_kind, jload, now_iso, parse_time, set_bucket, set_meta,
                 upsert_account, upsert_post)

IMPORT_SOURCE = "import"
_MEDIA = re.compile(r"^/?media/(.+)$")
_THUMB = re.compile(r"^/?thumb/\d+/(.+)$")
_AVATAR = re.compile(r"^/?avatar/(.+)$")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _strip(url: str | None, rx: re.Pattern) -> str | None:
    if not url:
        return None
    m = rx.match(url.split("?")[0])
    if not m:
        raise ValueError(f"unexpected url {url!r}")
    return m.group(1)


def _data_dir(root: Path) -> Path:
    for d in (root / "data", root):
        if (d / "meta.json").is_file() and (d / "accounts.json").is_file():
            return d
    raise FileNotFoundError(f"no export found in {root} (expected data/meta.json and data/accounts.json)")


def _account_from_brief(con: sqlite3.Connection, brief: dict) -> int:
    row = con.execute("SELECT id FROM accounts WHERE slug=?", (brief["slug"],)).fetchone()
    if row:
        return row["id"]
    return upsert_account(con, brief["slug"], kind=check_account_kind(brief["kind"]), handle=brief["handle"],
                          display_name=brief["display_name"], color=brief.get("color"),
                          avatar=_strip(brief.get("avatar_url"), _AVATAR))


def _import_account(con: sqlite3.Connection, item: dict, detail: dict) -> int:
    slug = item["slug"]
    clash = con.execute("SELECT slug FROM accounts WHERE handle=? AND slug!=?", (item["handle"], slug)).fetchone()
    if clash:
        raise ValueError(f"handle {item['handle']} of {slug!r} already belongs to account {clash['slug']!r}")
    row = con.execute("SELECT profile_json FROM accounts WHERE slug=?", (slug,)).fetchone()
    profile = (jload(row["profile_json"], {}) if row else {}) or {}    # keep private writer keys
    profile["inworld"] = {
        "ueber_mich": detail.get("about") or [],
        "steckbrief": detail.get("facts"),
        "werte": detail.get("values") or [],
        "mein_tag": detail.get("day") or [],
        "leute": [{"slug": p["slug"], "text": p["text"]} for p in detail.get("people") or []],
    }
    profile["public_following"] = int(detail.get("following") or 0)
    return upsert_account(
        con, slug, kind=check_account_kind(item["kind"]), handle=item["handle"], display_name=item["display_name"],
        bio=item.get("bio"), followers=int(item.get("followers") or 0), followers_note=detail.get("followers_note"),
        avatar=_strip(item.get("avatar_url"), _AVATAR), color=item.get("color"), profile=profile)


def _poster(card: dict, media: str | None) -> str | None:
    """None when the poster thumbnail is the one derived from the media file, else its thumb path."""
    thumb = _strip(card.get("poster_url"), _THUMB)
    if not thumb:
        return None
    if media and Path(thumb).with_suffix("") == Path(media).with_suffix(""):
        return None
    return thumb


def _import_post(con: sqlite3.Connection, card: dict, accounts: dict[str, int]) -> int:
    slug = card["account"]["slug"]
    aid = accounts.get(slug) or _account_from_brief(con, card["account"])
    media = _strip(card.get("media_url"), _MEDIA)
    parent = card.get("parent") or None
    row = con.execute("SELECT meta_json FROM posts WHERE id=?", (int(card["id"]),)).fetchone()
    meta = (jload(row["meta_json"], {}) if row else {}) or {}       # keep private writer keys (legacy import)
    if card.get("episode"):
        meta["episode"] = card["episode"]
    else:
        meta.pop("episode", None)
    return upsert_post(
        con, id=int(card["id"]), account_id=aid, parent_id=int(parent["id"]) if parent else None, kind=card["kind"],
        title=card.get("title"), caption=card.get("caption"), hashtags=card.get("hashtags"), media=media,
        poster=_poster(card, media), posted_at=card["posted_at"], source=card.get("source"),
        clip_id=card.get("clip_id"), funny_score=card.get("funny_score"), why=card.get("why"),
        meta=meta or None,
        status="published", generation_json=None)


def _sync_reactions(con: sqlite3.Connection, pid: int, totals: dict[str, int], uwe_reacted: bool,
                    uwe_id: int | None) -> None:
    if uwe_id is not None:
        con.execute("DELETE FROM reactions WHERE post_id=? AND account_id=? AND source=?", (pid, uwe_id, IMPORT_SOURCE))
        named = con.execute("SELECT 1 FROM reactions WHERE post_id=? AND account_id=?", (pid, uwe_id)).fetchone()
        if uwe_reacted and not named:          # real named reactions (legacy import) win over the placeholder
            rtype = next((t for t in config.REACTION_TYPES if totals.get(t, 0) > 0), "lol")
            add_named_reaction(con, pid, uwe_id, rtype, source=IMPORT_SOURCE)
    for rtype in config.REACTION_TYPES:
        other = con.execute(
            "SELECT COALESCE(SUM(count),0) FROM reactions WHERE post_id=? AND type=? "
            "AND NOT (account_id IS NULL AND source=?)", (pid, rtype, IMPORT_SOURCE)).fetchone()[0]
        set_bucket(con, pid, rtype, max(0, int(totals.get(rtype, 0)) - int(other)), IMPORT_SOURCE)


def _import_comments(con: sqlite3.Connection, pid: int, nodes: list[dict], accounts: dict[str, int],
                     reply_to: int | None = None) -> int:
    n = 0
    for c in nodes:
        cid = int(c["id"])
        aid = accounts.get(c["account"]["slug"]) or _account_from_brief(con, c["account"])
        accounts[c["account"]["slug"]] = aid
        dt = parse_time(c["created_at"])
        parent = c.get("reply_to") if c.get("reply_to") is not None else reply_to
        row = con.execute("SELECT post_id FROM comments WHERE id=?", (cid,)).fetchone()
        vals = (aid, parent, c["text"], dt.isoformat(timespec="seconds"), int(dt.timestamp()), c.get("source") or "seed")
        if row:
            if row["post_id"] != pid:
                raise ValueError(f"comment {cid} belongs to post {row['post_id']}, not {pid}")
            con.execute("UPDATE comments SET account_id=?, reply_to=?, text=?, created_at=?, ts=?, source=? WHERE id=?",
                        (*vals, cid))
        else:
            con.execute("INSERT INTO comments (account_id, reply_to, text, created_at, ts, source, id, post_id) "
                        "VALUES (?,?,?,?,?,?,?,?)", (*vals, cid, pid))
        n += 1 + _import_comments(con, pid, c.get("replies") or [], accounts, cid)
    return n


def import_public(con: sqlite3.Connection, root: Path) -> dict:
    data = _data_dir(Path(root))
    meta = _read(data / "meta.json")
    items = _read(data / "accounts.json")["items"]
    stats = {"accounts": 0, "posts": 0, "comments": 0}
    try:
        accounts: dict[str, int] = {}
        for item in items:
            detail_file = data / "accounts" / f"{item['slug']}.json"
            detail = _read(detail_file) if detail_file.is_file() else {}
            accounts[item["slug"]] = _import_account(con, item, detail)
            stats["accounts"] += 1

        cards = [_read(f) for f in sorted((data / "posts").glob("*.json"), key=lambda f: int(f.stem))]
        cards.sort(key=lambda c: (bool(c.get("parent")), int(c["id"])))         # stories before their moments
        uwe_id = accounts.get("uwe")
        for card in cards:
            pid = _import_post(con, card, accounts)
            _sync_reactions(con, pid, card.get("reactions") or {}, bool(card.get("uwe_reacted")), uwe_id)
            stats["comments"] += _import_comments(con, pid, card.get("comments") or [], accounts)
            stats["posts"] += 1
        set_meta(con, "import_public", {"today": meta.get("today"), "week": meta.get("week"),
                                        "posts": stats["posts"], "at": now_iso()})
        con.commit()
    except BaseException:
        con.rollback()
        raise
    stats["today"] = meta.get("today")
    return stats

