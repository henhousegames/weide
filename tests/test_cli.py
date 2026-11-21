import pytest

from weide import cli


def test_subcommands():
    p = cli.build_parser()
    for cmd in ("migrate", "import-public", "serve", "export-static"):
        assert p.parse_args([cmd, "x"] if cmd in ("import-public", "export-static") else [cmd]).cmd == cmd
    with pytest.raises(SystemExit):
        p.parse_args(["simulate"])


def test_serve_runs_uvicorn(monkeypatch, db_path):
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    cli.main(["serve", "--db", str(db_path), "--port", "9999"])
    assert seen["port"] == 9999 and seen["host"] == "127.0.0.1" and seen["app"].title == cli.config.APP_NAME
