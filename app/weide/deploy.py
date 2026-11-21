"""`weide deploy`: pull approved generations, export the static site, leak-check it, report changes.

Purely local: nothing is committed or pushed. The result says whether the site directory changed, so
automation can skip no-op deploys (`"changed": false`). A pull failure stops before the export; a leak
check hit is an error (the files are written, but must not be committed).

Daily full pull: the pull is incremental (cursor), except once per FULL_PULL_INTERVAL: when the DB meta
`last_full_pull_at` (written by pull_approved after a fully successful full listing) is missing, unreadable
or older than that, the pull is a full one (re-fetches every publication and its summary, so changed
credits reach existing posts, and unpublishes posts that vanished from the service). `full=True/False`
forces either way; the CLI maps `--full` / `--incremental` and HENHOUSE_FULL_PULL=1|0 onto it.
A full pull that changes nothing gives a byte-identical site (`"changed": false`).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .export_static import export_static


def snapshot(site: Path) -> dict[str, str]:
    """relative path -> sha256 of every file under site (git metadata excluded)."""
    out: dict[str, str] = {}
    if not site.is_dir():
        return out
    for p in sorted(site.rglob("*")):
        if p.is_file() and ".git" not in p.relative_to(site).parts:
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            out[p.relative_to(site).as_posix()] = h.hexdigest()
    return out


def diff(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    return {"added": sorted(after.keys() - before.keys()),
            "removed": sorted(before.keys() - after.keys()),
            "modified": sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}


FULL_PULL_INTERVAL = timedelta(hours=24)


def full_pull_due(con, now: datetime | None = None, interval: timedelta = FULL_PULL_INTERVAL) -> bool:
    """True when the last successful full listing is missing, unreadable, in the future or >= interval old."""
    from . import config
    from .db import get_meta, parse_time
    from .publish import LAST_FULL_KEY

    last = get_meta(con, LAST_FULL_KEY)
    try:
        last_dt = parse_time(last) if isinstance(last, str) and last else None
    except ValueError:
        last_dt = None
    if last_dt is None:
        return True
    now = now or datetime.now(config.TZ)
    age = now - last_dt
    return age >= interval or age < timedelta(0)


def deploy(site: Path, db_path: str | Path | None = None, *, pull: bool = True, full: bool | None = None,
           today: str | None = None,
           settings=None, transport=None, log: Callable[[str], None] = lambda s: None) -> dict:
    from .db import connect
    from .publish import ServiceSettings, pull_approved

    site = Path(site)
    res: dict = {"site": site.name, "pull": None, "export": None, "errors": [], "changed": False}
    before = snapshot(site)
    if pull:
        settings = settings or ServiceSettings.load()
        con = connect(db_path)
        try:
            if full is None:
                full = full_pull_due(con)
            res["pull"] = pull_approved(con, settings, full=full, transport=transport, log=log)
        finally:
            con.close()
        if res["pull"]["failed"]:
            res["errors"] += [f"pull: {m}" for m in res["pull"]["failed"]]
            return res
    exp = export_static(site, db_path, today=today)
    res["export"] = {k: exp[k] for k in ("posts", "accounts", "generations", "files", "bytes", "today")}
    res["export"]["missing"] = len(exp["missing"])
    if exp["leaks"]:
        res["errors"] += [f"leak: {h}" for h in exp["leaks"]]
    d = diff(before, snapshot(site))
    res["changes"] = {k: len(v) for k, v in d.items()}
    res["changed"] = any(d.values())
    return res
