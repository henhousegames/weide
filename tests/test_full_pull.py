"""Daily full pull in `weide deploy`: due decision, credits refresh, no-op idempotence, revocation, retry."""
import json
import shutil
import subprocess
from datetime import datetime, timedelta

import httpx
import pytest

from weide import config, db
from weide.deploy import FULL_PULL_INTERVAL, deploy, full_pull_due, snapshot
from weide.publish import LAST_FULL_KEY, ServiceSettings

from .test_publish import API, BASE, KEY, PID, SUMMARY, FakeService, env, posts, publication, run  # noqa: F401

PID2 = "pub_EXAMPLE02"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=config.TZ)
CREDIT = {"kind": "model", "text": "Model X by Example Lab", "license": {"id": "Apache-2.0"}}


def _deploy(env, site, **kw):
    con, svc, *_, dbp = env
    con.commit()
    return deploy(site, dbp, settings=ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc),
                  today="2026-11-17", **kw)


def _last_full(con):
    return db.get_meta(con, LAST_FULL_KEY)


def _age(con, delta):
    db.set_meta(con, LAST_FULL_KEY, (datetime.now(config.TZ) - delta).isoformat(timespec="seconds"))
    con.commit()


def _listings(svc):
    return [c for c in svc.calls if c.startswith(BASE + API) and "/summary" not in c and "/media/" not in c]


def test_full_pull_due_decision(env):
    con = env[0]
    assert full_pull_due(con, NOW) is True                                        # never ran
    for value, due in [((NOW - timedelta(hours=1)).isoformat(), False),
                       ((NOW - FULL_PULL_INTERVAL + timedelta(seconds=1)).isoformat(), False),
                       ((NOW - FULL_PULL_INTERVAL).isoformat(), True),
                       ((NOW - timedelta(days=3)).isoformat(), True),
                       ((NOW + timedelta(hours=2)).isoformat(), True),            # clock went back
                       ("garbage", True), (42, True)]:
        db.set_meta(con, LAST_FULL_KEY, value)
        assert full_pull_due(con, NOW) is due, value


def test_deploy_runs_full_once_per_day(env, tmp_path):
    con, svc, *_ = env
    site = tmp_path / "site"
    first = _deploy(env, site)
    assert first["pull"]["full"] is True and first["pull"]["new"] == 1 and first["errors"] == []
    stamp = _last_full(con)
    assert stamp and not full_pull_due(con)
    n = len(_listings(svc))
    second = _deploy(env, site)                                                   # within 24 h: incremental
    assert second["pull"]["full"] is False and "since=" in _listings(svc)[n] and _last_full(con) == stamp
    n = len(_listings(svc))
    _age(con, timedelta(hours=25))
    third = _deploy(env, site)
    assert third["pull"]["full"] is True and "since=" not in _listings(svc)[n]
    assert not full_pull_due(con)
    assert _deploy(env, site, full=True)["pull"]["full"] is True                  # forced
    _age(con, timedelta(days=2))
    assert _deploy(env, site, full=False)["pull"]["full"] is False                # forced incremental


def test_full_pull_updates_changed_credits(env, tmp_path):
    con, svc, *_ = env
    site = tmp_path / "site"
    svc.summaries[PID] = {**SUMMARY, "credits": [CREDIT]}
    assert _deploy(env, site)["pull"]["new"] == 1
    # the service recomputes the summary's credits; the list item (approved_at) does not change
    new_credit = {**CREDIT, "text": "Model X by Example Lab, licence notice v2"}
    svc.summaries[PID] = {**SUMMARY, "credits": [new_credit, {"kind": "compose", "text": "Musik: Beispiel"}]}
    inc = _deploy(env, site)
    assert inc["pull"]["full"] is False and inc["changed"] is False               # incremental cannot see it
    _age(con, timedelta(days=1, minutes=1))
    full = _deploy(env, site)
    assert full["pull"]["full"] is True and full["pull"]["updated"] == 1 and full["changed"] is True
    assert full["errors"] == []
    gen = json.loads((site / f"data/gen/{PID}.json").read_text(encoding="utf-8"))
    assert json.dumps(gen, ensure_ascii=False).count("licence notice v2") >= 1
    record = json.loads(posts(con)[0]["generation_json"])
    assert record["credits"] == [new_credit, {"kind": "compose", "text": "Musik: Beispiel"}]
    summaries = [c for c in svc.calls if c.endswith("/summary")]
    assert len(summaries) == 2                                                    # full pull re-fetched it


def test_unchanged_full_pull_is_a_byte_identical_noop(env, tmp_path):
    con, svc, *_ = env
    svc.pubs.append(publication(PID2, approved_at="2026-11-17T18:00:00+00:00"))
    svc.summaries[PID] = {**SUMMARY, "credits": [CREDIT]}
    site = tmp_path / "site"
    assert _deploy(env, site)["changed"] is True
    if shutil.which("git"):
        git = lambda *a: subprocess.run(["git", "-C", str(site), *a], check=True, capture_output=True, text=True)
        git("init", "-q")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qm", "site")
    before = snapshot(site)
    stamp = _last_full(con)
    _age(con, timedelta(days=2))
    res = _deploy(env, site)
    assert res["pull"]["full"] is True and res["pull"]["unchanged"] == 2 and res["pull"]["updated"] == 0
    assert res["errors"] == [] and res["changed"] is False
    assert res["changes"] == {"added": 0, "removed": 0, "modified": 0}
    assert snapshot(site) == before
    assert _last_full(con) >= stamp                                               # recorded again
    if shutil.which("git"):
        assert git("status", "--porcelain").stdout == ""                          # nothing to commit
    assert _deploy(env, site, full=True)["changed"] is False


def test_revocation_via_full_pull(env, tmp_path):
    con, svc, *_ = env
    svc.pubs.append(publication(PID2, approved_at="2026-11-17T18:00:00+00:00"))
    svc.pubs.append(publication("pub_EXAMPLE03", approved_at="2026-11-17T19:00:00+00:00"))
    site = tmp_path / "site"
    assert _deploy(env, site)["pull"]["new"] == 3
    assert (site / f"data/gen/{PID}.json").is_file() and (site / f"data/gen/{PID2}.json").is_file()
    # PID: revoked at a time before the stored cursor (an incremental pull never sees it);
    # PID2: gone from the service without any revocation entry
    svc.pubs = [p for p in svc.pubs if p["public_id"] == "pub_EXAMPLE03"]
    svc.revoked = [{"public_id": PID, "revoked_at": "2026-11-17T17:30:00+00:00"}]
    inc = _deploy(env, site)
    assert inc["pull"]["full"] is False and inc["pull"]["revoked"] == 0 and inc["changed"] is False
    _age(con, timedelta(days=1, hours=1))
    res = _deploy(env, site)
    assert res["pull"]["full"] is True and res["pull"]["revoked"] == 2 and res["errors"] == []
    assert any(PID2 in n and "missing" in n for n in res["pull"]["notes"])
    status = {p["public_id"]: p["status"] for p in posts(con)}
    assert status == {PID: "draft", PID2: "draft", "pub_EXAMPLE03": "published"}
    for pid in (PID, PID2):
        assert not (site / f"data/gen/{pid}.json").exists() and not (site / f"media/gen/{pid}.mp4").exists()
        assert pid not in (site / "data/feed.json").read_text(encoding="utf-8")
    assert (site / "data/gen/pub_EXAMPLE03.json").is_file()
    again = _deploy(env, site, full=True)
    assert again["pull"]["revoked"] == 0 and again["changed"] is False            # idempotent


def test_empty_full_listing_unpublishes_nothing(env):
    con, svc, *_ = env
    run(con, svc)
    svc.pubs = []
    res = run(con, svc, full=True)
    assert res["revoked"] == 0 and res["failed"] == [] and any("not unpublished" in n for n in res["notes"])
    assert posts(con)[0]["status"] == "published"


def test_failed_full_pull_does_not_advance_timestamp(env, tmp_path):
    con, svc, *_ = env
    site = tmp_path / "site"
    assert _deploy(env, site)["errors"] == []
    _age(con, timedelta(days=2))
    stamp = _last_full(con)
    # one item fails (unknown character): a warning, the rest is exported, the timestamp stays, the next run
    # is full again
    svc.pubs.append(publication(PID2, character="nobody", approved_at="2026-11-18T09:00:00+00:00"))
    bad = _deploy(env, site)
    assert bad["pull"]["full"] is True and bad["errors"] == [] and bad["changed"] is False
    assert bad["failed_items"] == [PID2] and len(bad["warnings"]) == 1 and bad["export"]["generations"] == 1
    assert _last_full(con) == stamp and full_pull_due(con)
    # the listing itself fails
    svc.pubs.pop()
    ok_transport = svc.__call__
    svc_down = lambda req: httpx.Response(503) if req.url.path == API else ok_transport(req)
    con.commit()
    down = deploy(site, env[4], settings=ServiceSettings(BASE, KEY), transport=httpx.MockTransport(svc_down),
                  today="2026-11-17")
    assert down["errors"] and _last_full(con) == stamp
    # fixed: the retry is full and records the time
    res = _deploy(env, site)
    assert res["pull"]["full"] is True and res["errors"] == [] and _last_full(con) != stamp
    assert not full_pull_due(con)


def test_dry_run_full_pull_does_not_record(env):
    con, svc, *_ = env
    run(con, svc, dry_run=True, full=True)
    assert _last_full(con) is None


def test_cli_deploy_full_flags(env, tmp_path, monkeypatch, capsys):
    from weide import cli
    import weide.deploy as deploy_mod
    seen = []
    monkeypatch.setattr(deploy_mod, "deploy", lambda *a, **kw: seen.append(kw["full"]) or
                        {"errors": [], "changed": False})
    site = str(tmp_path / "site")
    monkeypatch.delenv("HENHOUSE_FULL_PULL", raising=False)
    monkeypatch.delenv("WEIDE_FULL_PULL", raising=False)
    cli.main(["deploy", site])
    cli.main(["deploy", site, "--full"])
    cli.main(["deploy", site, "--incremental"])
    monkeypatch.setenv("HENHOUSE_FULL_PULL", "1")
    cli.main(["deploy", site])
    monkeypatch.setenv("HENHOUSE_FULL_PULL", "0")
    cli.main(["deploy", site])
    assert seen == [None, True, False, True, False]
    with pytest.raises(SystemExit):
        cli.main(["deploy", site, "--full", "--incremental"])
