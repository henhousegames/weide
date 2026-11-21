"""Seed characters from the public, committed files in app/weide/seed/*.json (`weide seed-characters`).

Each file describes one posting account (slug, handle, display_name, kind, world, bio, followers, color)
with its in-world profile in the same fields the API returns (about, facts, values, day, people) and an
optional `character` block for the characters row. Everything in these files is public by design; the
private style guidance of a seeded character starts empty. Seeding is idempotent: accounts are matched
by slug, the characters row by account, and private profile keys already in the database are kept.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import config
from .db import jload, now_iso, upsert_account

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
