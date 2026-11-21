"""Command line: `uv run weide <command>`.

Commands: migrate, import-public, seed-characters, serve, export-static, pull-approved, deploy.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config


def _db_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=None, help="SQLite file (default: $HENHOUSE_DB / $WEIDE_DB or <private dir>/henhouse.db)")


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


def cmd_seed_characters(a: argparse.Namespace) -> None:
    from .characters import seed_characters
    from .db import connect
    con = connect(a.db)
    try:
        stats = seed_characters(con)
    except ValueError as e:
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


def cmd_pull_approved(a: argparse.Namespace) -> None:
    from .db import connect
    from .publish import PublishError, ServiceSettings, pull_approved
    try:
        settings = ServiceSettings.load()
    except PublishError as e:
        _die(str(e))
    con = connect(a.db)
    try:
        res = pull_approved(con, settings, dry_run=a.dry_run, log=lambda s: print(s, file=sys.stderr))
    finally:
        con.close()
    print(json.dumps(res, ensure_ascii=False))
    if res["failed"]:
        _die(f"{len(res['failed'])} item(s) failed", code=1)


def cmd_deploy(a: argparse.Namespace) -> None:
    from .deploy import deploy
    from .publish import PublishError
    try:
        res = deploy(Path(a.site), a.db, pull=not a.skip_pull, today=a.today,
                     log=lambda s: print(s, file=sys.stderr))
    except PublishError as e:
        _die(str(e))
    print(json.dumps(res, ensure_ascii=False))
    if res["errors"]:
        _die("; ".join(res["errors"][:10]), code=1)


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

    s = sub.add_parser("seed-characters", help="create/update the public seed characters (app/weide/seed); idempotent")
    _db_arg(s)
    s.set_defaults(fn=cmd_seed_characters)

    s = sub.add_parser("serve", help=f"run the web app (default port {config.DEFAULT_PORT})")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    _db_arg(s)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("export-static", help="write a static snapshot (published posts only) for a plain file host")
    s.add_argument("outdir")
    s.add_argument("--today", default=None, help="world date YYYY-MM-DD for the ranking pages (default: $HENHOUSE_TODAY / $WEIDE_TODAY, else the date of the newest published post)")
    s.add_argument("--link", action="store_true", help="hard-link media files instead of copying (same filesystem)")
    _db_arg(s)
    s.set_defaults(fn=cmd_export_static)

    s = sub.add_parser("pull-approved", help="publish the generations approved in the generation service; idempotent")
    s.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    _db_arg(s)
    s.set_defaults(fn=cmd_pull_approved)

    s = sub.add_parser("deploy", help="pull-approved, export-static into a site dir, leak check, report changes (no git)")
    s.add_argument("site", nargs="?", default="site", help="site directory (default: site)")
    s.add_argument("--skip-pull", action="store_true", help="only re-export the current database")
    s.add_argument("--today", default=None, help="world date YYYY-MM-DD for the ranking pages (default: date of the newest published post)")
    _db_arg(s)
    s.set_defaults(fn=cmd_deploy)
    return ap


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
