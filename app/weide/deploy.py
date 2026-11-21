"""`weide deploy`: pull approved generations, export the static site, leak-check it, report changes.

Purely local: nothing is committed or pushed. The result says whether the site directory changed, so
automation can skip no-op deploys (`"changed": false`). A pull failure stops before the export; a leak
check hit is an error (the files are written, but must not be committed).
"""
from __future__ import annotations

import hashlib
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


def deploy(site: Path, db_path: str | Path | None = None, *, pull: bool = True, today: str | None = None,
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
            res["pull"] = pull_approved(con, settings, transport=transport, log=log)
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
