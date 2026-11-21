"""Command line: `uv run weide <command>`.

Commands: migrate, import-public, serve, export-static.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config


def _db_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=None, help="SQLite file (default: $WEIDE_DB or <private dir>/weide.db)")


def _die(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def cmd_migrate(a: argparse.Namespace) -> None:
    import sqlite3

    from .db import SCHEMA_VERSION, migrate, schema_version
    path = Path(a.db or config.DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), isolation_level=None)
    try:
        before = schema_version(con)
        applied = migrate(con)
    finally:
        con.close()
    print(json.dumps({"from": before, "to": SCHEMA_VERSION, "applied": applied}))


def cmd_import_public(a: argparse.Namespace) -> None:
    from .db import connect
    from .importer import import_public
    con = connect(a.db)
    try:
        stats = import_public(con, Path(a.dir))
    except (ValueError, LookupError, FileNotFoundError) as e:
        _die(str(e))
    finally:
        con.close()
    print(json.dumps(stats, ensure_ascii=False))


def cmd_serve(a: argparse.Namespace) -> None:
    import uvicorn

    from .server import create_app
    uvicorn.run(create_app(a.db), host=a.host, port=a.port, log_level="info")


def cmd_export_static(a: argparse.Namespace) -> None:
    from .export_static import export_static
    res = export_static(Path(a.outdir), a.db, today=a.today, link=a.link)
    print(json.dumps(res, ensure_ascii=False))
    if res["missing"]:
        print(f"warning: {len(res['missing'])} media files missing", file=sys.stderr)
    if res["leaks"]:
        _die(f"leak check failed ({len(res['leaks'])} hits): " + "; ".join(res["leaks"][:10]), code=1)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="weide", description=f"{config.APP_NAME}: {config.APP_TAGLINE}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("migrate", help="create or upgrade the database schema")
    _db_arg(s)
    s.set_defaults(fn=cmd_migrate)

    s = sub.add_parser("import-public", help="import a public static export (its data/*.json) into the database; idempotent")
    s.add_argument("dir", help="export root (containing data/) or the data/ dir itself")
    _db_arg(s)
    s.set_defaults(fn=cmd_import_public)

    s = sub.add_parser("serve", help=f"run the web app (default port {config.DEFAULT_PORT})")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    _db_arg(s)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("export-static", help="write a static snapshot (published posts only) for a plain file host")
    s.add_argument("outdir")
    s.add_argument("--today", default=None, help="world date YYYY-MM-DD for the ranking pages (default: $WEIDE_TODAY or local date)")
    s.add_argument("--link", action="store_true", help="hard-link media files instead of copying (same filesystem)")
    _db_arg(s)
    s.set_defaults(fn=cmd_export_static)
    return ap


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
