import importlib
import os

from weide import config


def test_app_name_single_constant():
    assert config.APP_NAME == "henhouse"
    assert config.APP_SLUG == "henhouse"
    assert config.ENV_PREFIX == "HENHOUSE_"
    assert config.APP_TAGLINE


def test_paths_relative_to_repo():
    assert (config.REPO_ROOT / "app" / "weide" / "config.py").is_file()
    assert config.TEMPLATE_DIR.joinpath("index.html").is_file()
    assert config.PRIVATE_DIR == config.REPO_ROOT / "private" or config.env("PRIVATE_DIR")


def _reload(monkeypatch, **env):
    for k in ("PRIVATE_DIR", "DB"):
        monkeypatch.delenv("HENHOUSE_" + k, raising=False)
        monkeypatch.delenv("WEIDE_" + k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(config)


def test_env_overrides(monkeypatch, tmp_path):
    try:
        c = _reload(monkeypatch, HENHOUSE_PRIVATE_DIR=str(tmp_path / "priv"))
        assert c.PRIVATE_DIR == tmp_path / "priv"
        assert c.DEFAULT_DB == tmp_path / "priv" / "henhouse.db"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_legacy_env_names_and_db_file(monkeypatch, tmp_path):
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "weide.db").write_bytes(b"")
    try:
        c = _reload(monkeypatch, WEIDE_PRIVATE_DIR=str(tmp_path / "old"))
        assert c.PRIVATE_DIR == tmp_path / "old"
        assert c.DEFAULT_DB == tmp_path / "old" / "weide.db"          # existing legacy database is kept
        c = _reload(monkeypatch, WEIDE_PRIVATE_DIR=str(tmp_path / "old"), HENHOUSE_PRIVATE_DIR=str(tmp_path / "new"))
        assert c.PRIVATE_DIR == tmp_path / "new"                       # the new name wins
    finally:
        monkeypatch.undo()
        importlib.reload(config)
    assert os.environ.get("HENHOUSE_PRIVATE_DIR") is None or config.PRIVATE_DIR


def test_template_has_placeholders_only():
    html = config.TEMPLATE_DIR.joinpath("index.html").read_text(encoding="utf-8")
    assert "{{APP_NAME}}" in html and "{{APP_TAGLINE}}" in html
