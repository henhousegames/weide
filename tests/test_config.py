import importlib

from weide import config


def test_app_name_single_constant():
    assert config.APP_NAME == "Weide"
    assert config.APP_SLUG == "weide"


def test_paths_relative_to_repo():
    assert (config.REPO_ROOT / "app" / "weide" / "config.py").is_file()
    assert config.TEMPLATE_DIR.joinpath("index.html").is_file()
    assert config.PRIVATE_DIR == config.REPO_ROOT / "private" or "WEIDE_PRIVATE_DIR" in __import__("os").environ


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("WEIDE_PRIVATE_DIR", str(tmp_path / "priv"))
    monkeypatch.delenv("WEIDE_DB", raising=False)
    try:
        c = importlib.reload(config)
        assert c.PRIVATE_DIR == tmp_path / "priv"
        assert c.DEFAULT_DB == tmp_path / "priv" / "weide.db"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_template_has_placeholders_only():
    html = config.TEMPLATE_DIR.joinpath("index.html").read_text(encoding="utf-8")
    assert "{{APP_NAME}}" in html and "{{APP_TAGLINE}}" in html
