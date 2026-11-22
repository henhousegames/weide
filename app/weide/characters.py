"""Seed characters from the public, committed files in app/weide/seed/*.json (`weide seed-characters`).

Each file describes one posting account (slug, handle, display_name, kind, world, bio, followers, color)
with its in-world profile in the same fields the API returns (about, facts, values, day, people) and an
optional `character` block for the characters row. Everything in these files is public by design; the
private style guidance of a seeded character starts empty. Seeding is idempotent: accounts are matched
by slug, the characters row by account, and private profile keys already in the database are kept.
A seeded character is not public (API, export) until its first post is published; the preview shows it.

`account_from_identity` creates a missing posting account from a character's identity.md front matter
(the private stories repo, HENHOUSE_CHARACTERS_DIR): `weide pull-approved` uses it for publications of a
character that was never seeded. Only allowlisted front matter fields are used (slug = directory name,
name -> display_name, handle, kind, bio capped at feed.BIO_MAX); the markdown body is never read and the
`avatar` (a generation id) is never copied. It only inserts: an existing account is never changed.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import config
from .db import jload, now_iso, upsert_account, world_of

SEED_DIR = config.PACKAGE_DIR / "seed"


def load_seeds(seed_dir: Path = SEED_DIR) -> list[dict]:
    return [json.loads(f.read_text(encoding="utf-8")) for f in sorted(Path(seed_dir).glob("*.json"))]


def seed_character(con: sqlite3.Connection, s: dict) -> int:
    kind = s["kind"]
    if kind not in config.CHARACTER_KINDS:
        raise ValueError(f"{s['slug']}: character kind must be one of {config.CHARACTER_KINDS}, got {kind!r}")
    clash = con.execute("SELECT slug FROM accounts WHERE handle=? AND slug!=?", (s["handle"], s["slug"])).fetchone()
    if clash:
        raise ValueError(f"handle {s['handle']} already belongs to account {clash['slug']!r}")
    row = con.execute("SELECT profile_json FROM accounts WHERE slug=?", (s["slug"],)).fetchone()
    profile = (jload(row["profile_json"], {}) if row else {}) or {}
    profile["inworld"] = {
        "ueber_mich": s.get("about") or [],
        "steckbrief": s.get("facts"),
        "werte": s.get("values") or [],
        "mein_tag": s.get("day") or [],
        "leute": [{"slug": p["slug"], "text": p["text"]} for p in s.get("people") or []],
    }
    aid = upsert_account(con, s["slug"], kind=kind, world=s.get("world"), handle=s["handle"],
                         display_name=s["display_name"], bio=s.get("bio"), followers=int(s.get("followers") or 0),
                         color=s.get("color"), profile=profile)
    ch = s.get("character") or {}
    now = now_iso()
    existing = con.execute("SELECT id FROM characters WHERE account_id=?", (aid,)).fetchone()
    if existing:
        con.execute("UPDATE characters SET kind=?, identity=?, updated_at=? WHERE id=?",
                    (kind, ch.get("identity"), now, existing["id"]))       # style_guidance is never overwritten
    else:
        con.execute("INSERT INTO characters (account_id, kind, identity, style_guidance, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)", (aid, kind, ch.get("identity"), ch.get("style_guidance") or "", now, now))
    return aid


def seed_characters(con: sqlite3.Connection, seed_dir: Path = SEED_DIR) -> dict:
    slugs = []
    try:
        for s in load_seeds(seed_dir):
            seed_character(con, s)
            slugs.append(s["slug"])
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return {"characters": slugs}


class IdentityError(ValueError):
    pass


def identity_fields(root: Path, slug: str) -> dict:
    """Allowlisted account fields from <root>/<slug>/identity.md front matter. IdentityError when the file
    is missing/unreadable or its kind is not a posting character kind."""
    from .feed import BIO_MAX, HANDLE, IDENTITY_FILE, KIND_MAP, MAX_NAME, SLUG, read_front_matter

    if not SLUG.match(slug):
        raise IdentityError(f"invalid character slug {slug[:64]!r}")
    fm = read_front_matter(Path(root) / slug / IDENTITY_FILE)
    if fm is None:
        raise IdentityError(f"no readable identity front matter for {slug!r}")
    kind = KIND_MAP.get((fm.get("kind") or "").strip().lower())
    if kind not in config.CHARACTER_KINDS:
        raise IdentityError(f"identity of {slug!r}: kind must map to one of {', '.join(config.CHARACTER_KINDS)}")
    name = " ".join((fm.get("name") or "").split())[:MAX_NAME] or slug
    handle = (fm.get("handle") or "").strip()
    handle = "@" + handle.lstrip("@") if handle and HANDLE.match(handle) else "@" + slug
    bio = " ".join((fm.get("bio") or "").split()) or None
    if bio and len(bio) > BIO_MAX:
        bio = bio[:BIO_MAX - 1].rstrip() + "…"
    return {"slug": slug, "display_name": name, "handle": handle, "kind": kind, "bio": bio}


def account_from_identity(con: sqlite3.Connection, root: Path, slug: str) -> int | None:
    """Create the posting account `slug` from its identity.md (identity_fields) when it does not exist.
    Returns the new account id, or None when the slug already exists (nothing is changed then). Not listed:
    like a seeded character it becomes public with its first published post. The caller commits."""
    if con.execute("SELECT 1 FROM accounts WHERE slug=?", (slug,)).fetchone():
        return None
    f = identity_fields(root, slug)
    clash = con.execute("SELECT slug FROM accounts WHERE handle=?", (f["handle"],)).fetchone()
    if clash:
        raise IdentityError(f"handle {f['handle']} already belongs to account {clash[0]!r}")
    aid = upsert_account(con, slug, kind=f["kind"], world=world_of(f["kind"], None), handle=f["handle"],
                         display_name=f["display_name"], bio=f["bio"], listed=0)
    now = now_iso()
    con.execute("INSERT INTO characters (account_id, kind, identity, style_guidance, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)", (aid, f["kind"], None, "", now, now))
    return aid
