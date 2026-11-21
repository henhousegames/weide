"""Funniest-post ranking for a day / week / range (published posts only).

One 0-25 scale for curated and uncurated posts:
    W = sum(weight[type] * reactions[type]) + COMMENT_WEIGHT * comments_by_others
        weights: lol 1.0, fire 0.5, chew 0.5, heart 0.2; COMMENT_WEIGHT 25
    E = 25 * (1 - exp(-W / ENGAGEMENT_REF))       # engagement 0..25, ENGAGEMENT_REF = 6000
    score = 0.9 * F + 0.1 * E    with a curated funny_score F (0..25)
    score = 15 * E / 25          without F (max 15)
A moment (best single beat of a story) uses its own F and the engagement E of its story post.
"""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any

from . import config
from .db import comment_counts, parse_time, reaction_totals

WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def today() -> date:
    if config.TODAY_OVERRIDE:
        return date.fromisoformat(config.TODAY_OVERRIDE)
    return datetime.now(config.TZ).date()


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=config.TZ)


def _parse_day(s: str, flag: str) -> date:
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        raise ValueError(f"{flag} must be a date like 2026-11-17, got {s!r}") from None


def window(day: str | None = None, week: str | None = None, since: str | None = None,
           until: str | None = None) -> dict[str, Any]:
    """Return {start, end (exclusive), label, key, kind}. Default: today.
    day, week and since/until are mutually exclusive (ValueError otherwise)."""
    given = [n for n, v in (("day", day), ("week", week), ("since/until", since or until)) if v]
    if len(given) > 1:
        raise ValueError(f"use only one of day, week or since/until (got {' and '.join(given)})")
    if week:
        m = re.fullmatch(r"(\d{4})-?W(\d{1,2})", week.strip(), re.I)
        if not m:
            raise ValueError(f"week must look like 2026-W47, got {week!r}")
        y, w = int(m.group(1)), int(m.group(2))
        try:
            mon = date.fromisocalendar(y, w, 1)
        except ValueError:
            raise ValueError(f"week: {y} has no week {w}") from None
        sun = mon + timedelta(days=6)
        return {"start": _day_start(mon), "end": _day_start(mon + timedelta(days=7)), "key": f"{y}-W{w:02d}",
                "kind": "week", "label": f"KW {w} ({mon:%d.%m.}–{sun:%d.%m.%Y})"}
    if since or until:
        def _bound(s: str, is_until: bool) -> datetime:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s.strip()):
                d = _parse_day(s, "until" if is_until else "since")
                return _day_start(d + timedelta(days=1)) if is_until else _day_start(d)
            try:
                return parse_time(s)
            except ValueError as e:
                raise ValueError(f"{'until' if is_until else 'since'}: {e}") from None
        start = _bound(since, False) if since else datetime(1970, 1, 1, tzinfo=config.TZ)
        end = _bound(until, True) if until else datetime(2100, 1, 1, tzinfo=config.TZ)
        if start >= end:
            raise ValueError(f"since ({since}) must be before until ({until})")
        key = f"{(since or 'start')[:10]}_{(until or 'now')[:10]}"
        return {"start": start, "end": end, "key": key, "kind": "range",
                "label": f"{start:%d.%m.%Y} bis {(end - timedelta(seconds=1)):%d.%m.%Y}"}
    d = _parse_day(day, "day") if day else today()
    return {"start": _day_start(d), "end": _day_start(d + timedelta(days=1)), "key": d.isoformat(), "kind": "day",
            "label": f"{WEEKDAYS[d.weekday()]}, {d:%d.%m.%Y}"}


def engagement(reactions: dict[str, int], comments: int) -> float:
    w = sum(config.REACTIONS[t]["weight"] * reactions.get(t, 0) for t in config.REACTIONS)
    return w + config.COMMENT_WEIGHT * comments


def engagement_score(w: float) -> float:
    """0..25; roughly linear up to a few thousand weighted reactions, then saturating."""
    return config.SCORE_MAX * (1 - math.exp(-max(w, 0) / config.ENGAGEMENT_REF))


def combine(funny: float | None, e: float) -> float:
    if funny is None:
        return config.UNCURATED_MAX * e / config.SCORE_MAX
    f = min(config.FUNNY_MAX, max(config.FUNNY_MIN, float(funny)))
    return config.CURATED_WEIGHT * f + (1 - config.CURATED_WEIGHT) * e


def top(con: sqlite3.Connection, win: dict, limit: int = 10, include_collections: bool = False) -> list[dict]:
    """Ranked published top-level posts in the window: [{post_id, rank, score, E, engagement, moments}].
    Moments carry no file paths (only ids, clip, funny_score, why, score)."""
    q = ("SELECT p.* FROM posts p WHERE p.parent_id IS NULL AND p.status='published' AND p.ts >= ? AND p.ts < ?")
    args: list[Any] = [int(win["start"].timestamp()), int(win["end"].timestamp())]
    if not include_collections:
        q += " AND p.kind != 'collection'"
    rows = con.execute(q, args).fetchall()
    ids = [r["id"] for r in rows]
    rx = reaction_totals(con, ids)
    cc = comment_counts(con, ids, exclude_author=True)
    scored = []
    for r in rows:
        w = engagement(rx[r["id"]], cc[r["id"]])
        e = engagement_score(w)
        scored.append({"row": r, "engagement": round(w, 1), "E": round(e, 2), "score": round(combine(r["funny_score"], e), 2)})
    scored.sort(key=lambda x: (-x["score"], -x["engagement"], x["row"]["ts"]))
    scored = scored[:limit] if limit else scored
    result = []
    for rank, s in enumerate(scored, 1):
        r = s["row"]
        moments = con.execute("SELECT * FROM posts WHERE parent_id=? AND status='published' "
                              "ORDER BY COALESCE(funny_score, 0) DESC, id", (r["id"],)).fetchall()
        result.append({
            "post_id": r["id"], "rank": rank, "score": s["score"], "E": s["E"], "engagement": s["engagement"],
            "moments": [{"post_id": m["id"], "clip": m["clip_id"], "funny_score": m["funny_score"], "why": m["why"],
                         "score": round(combine(m["funny_score"], s["E"]), 2)} for m in moments],
        })
    return result
