"""Public generation summaries: sanitizer, live endpoint, static export and leak check."""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from weide import db
from weide.export_static import export_static, leak_check
from weide.generation import PRIVATE_INPUT, clean_string, is_private_key, sanitize
from weide.importer import import_public
from weide.server import create_app

from .conftest import REPO

# A realistic internal record: every public setting plus planted private bits (obviously fake values).
RECORD = {
    "model_label": "Model X 1.0",
    "model": {"name": "model-x", "version": "1.0", "license": "Apache-2.0",
              "weights_path": "/home/EXAMPLE/models/model-x.safetensors", "file": "model-x.safetensors"},
    "task": "image-to-video", "preset": "reel-9x16",
    "parameters": {"seed": 1234, "steps": 30, "sampler": "euler", "sigmas": [1.0, 0.5, 0.0], "cfg": 3.5,
                   "width": 720, "height": 1280, "fps": 24, "frames": 97, "keyframe_strengths": [1.0, 0.6],
                   "quantization": "fp8", "key": "first-frame"},
    "prompt": "A cow presents a monkey-shaped key ring, warm light",
    "negative_prompt": "blurry",
    "workflow": {"nodes": {"1": {"class_type": "LoadImage", "inputs": {"image": "/home/EXAMPLE/in/ref.png"}},
                           "2": {"class_type": "Sampler", "inputs": {"seed": 1234, "key": 2,
                                                                      "server": "http://192.168.1.20:8188/prompt"}}}},
    "api_key": "EXAMPLE_KEY", "token": "EXAMPLE_TOKEN", "agentId": "agent-EXAMPLE", "run_id": "run-EXAMPLE",
    "logs": ["/home/EXAMPLE/run.log"], "output_path": "/home/EXAMPLE/out.mp4", "host": "gpu-box.local",
    "notes": "rendered on gpu-box.local:8188 from C:\\Users\\EXAMPLE\\x.png, see generation:run-EXAMPLE",
}
PLANTED = ("/home/", "EXAMPLE_KEY", "EXAMPLE_TOKEN", "agent-EXAMPLE", "run-EXAMPLE", "192.168.", "gpu-box",
           "C:\\", "Users", "generation:", "weights_path", "output_path", "logs")


def test_private_key_names():
    for k in ("api_key", "apiKey", "token", "secret", "password", "agent_id", "agentId", "run_id", "path", "paths",
              "file", "filename", "log", "logs", "workflow_path", "output_file", "secret_key", "host"):
        assert is_private_key(k), k
    for k in ("key", "seed", "steps", "prompt", "sampler", "cfg", "keyframe_strengths", "model", "license", "frames"):
        assert not is_private_key(k), k


def test_clean_string():
    assert clean_string("/home/EXAMPLE/wf.json") == "wf.json"
    assert clean_string("C:\\Users\\EXAMPLE\\a.png") == "a.png"
    assert clean_string("https://example.org/models/x.gguf?download=1") == "x.gguf"
    assert clean_string("generation:run-1") is None
    assert "10.0.0.7" not in clean_string("worker 10.0.0.7 done")
    assert clean_string("load /srv/EXAMPLE/x.png now") == "load x.png now"
    assert clean_string("A cow, 16:9, w/ hat, key light") == "A cow, 16:9, w/ hat, key light"


def test_sanitize_keeps_settings_and_drops_private_bits():
    s = sanitize(RECORD)
    blob = json.dumps(s, ensure_ascii=False)
    for p in PLANTED:
        assert p not in blob, p
    assert s["parameters"] == RECORD["parameters"]
    assert s["prompt"] == RECORD["prompt"] and s["model"] == {"name": "model-x", "version": "1.0", "license": "Apache-2.0"}
    assert s["workflow"]["nodes"]["1"]["inputs"]["image"] == "ref.png"
    assert s["workflow"]["nodes"]["2"]["inputs"]["key"] == 2
    assert s["workflow"]["nodes"]["2"]["inputs"]["server"] == "prompt"


@pytest.fixture(scope="module")
def base_db(tmp_path_factory):
    p = tmp_path_factory.mktemp("gen") / "base.db"
    con = db.connect(p)
    import_public(con, REPO)
    con.close()
    return p


@pytest.fixture
def gen_db(base_db, tmp_path):
    p = tmp_path / "weide.db"
    shutil.copy(base_db, p)
    con = db.connect(p)
    lisa = con.execute("SELECT id FROM accounts WHERE slug='lisa'").fetchone()[0]
    draft = db.upsert_post(con, account_id=lisa, kind="image", caption="Entwurf", status="draft",
                           posted_at="2026-11-17T20:00:00+01:00", source="cli:draft-input")
    first = db.upsert_post(con, account_id=lisa, kind="image", caption="Erstes Bild", status="published",
                           posted_at="2026-11-17T20:30:00+01:00", source="generation:run-EXAMPLE-1",
                           public_id="gfirst", generation={"model_label": "Model X 1.0", "prompt": "a cow"})
    rec = {**RECORD, "inputs": [
        {"role": "reference", "kind": "image", "post_id": 1},                   # published legacy post
        {"role": "start frame", "kind": "image", "public_id": "gfirst"},         # published generation
        {"role": "pose", "kind": "image", "post_id": draft},                     # unpublished -> private
        {"role": "sketch", "kind": "image", "path": "/home/EXAMPLE/sketch.png"}, # local file -> private
        {"role": "keyframe", "kind": "image", "generated": True, "strength": 0.6, "file": "/home/EXAMPLE/k.png"},
    ]}
    pid = db.upsert_post(con, account_id=lisa, kind="reel", caption="Neu", status="published",
                         posted_at="2026-11-17T21:00:00+01:00", source="generation:run-EXAMPLE-2",
                         public_id="g7f3k2", generation=rec)
    hidden = db.upsert_post(con, account_id=lisa, kind="text", caption="nicht öffentlich", status="approved",
                            posted_at="2026-11-17T21:30:00+01:00", source="generation:run-EXAMPLE-3",
                            public_id="ghidden", generation={"model_label": "Model X 1.0"})
    con.commit()
    con.close()
    return p, {"draft": draft, "first": first, "pid": pid, "hidden": hidden}


def test_live_endpoint_and_card_badge(gen_db):
    p, ids = gen_db
    c = TestClient(create_app(p))
    card = c.get(f"/api/posts/{ids['pid']}").json()
    assert card["generation"] == {"public_id": "g7f3k2", "model_label": "Model X 1.0"}
    feed = c.get("/api/feed?limit=3").json()["items"]
    assert feed[0]["id"] == ids["pid"] and feed[0]["generation"]["public_id"] == "g7f3k2"
    g = c.get("/api/generations/g7f3k2").json()
    assert g["public_id"] == "g7f3k2" and g["post_id"] == ids["pid"]
    assert g["parameters"]["seed"] == 1234 and g["prompt"].startswith("A cow")
    assert g["inputs"] == [
        {"role": "reference", "kind": "image", "post_id": 1, "label": "Post 1"},
        {"role": "start frame", "kind": "image", "post_id": ids["first"], "label": f"Post {ids['first']}",
         "public_id": "gfirst"},
        {"role": "pose", "kind": "image", "private": True, "label": PRIVATE_INPUT},
        {"role": "sketch", "kind": "image", "private": True, "label": PRIVATE_INPUT},
        {"role": "keyframe", "kind": "image", "generated": True, "strength": 0.6},
    ]
    blob = json.dumps(g, ensure_ascii=False)
    for s in PLANTED:
        assert s not in blob, s
    assert c.get("/api/generations/ghidden").status_code == 404           # not published
    assert c.get("/api/generations/nope").status_code == 404
    assert c.get("/api/generations/..%2Fx").status_code == 404


def test_export_writes_sanitized_summaries(gen_db, tmp_path):
    p, ids = gen_db
    out = tmp_path / "site"
    res = export_static(out, p, today="2026-11-17", link=True)
    assert res["leaks"] == [] and res["generations"] == 2
    assert sorted(f.name for f in (out / "data/gen").iterdir()) == ["g7f3k2.json", "gfirst.json"]
    g = json.loads((out / "data/gen/g7f3k2.json").read_text())
    assert g["parameters"]["key"] == "first-frame" and g["workflow"]["nodes"]["1"]["inputs"]["image"] == "ref.png"
    blob = "\n".join(f.read_text() for f in (out / "data").rglob("*.json"))
    for s in PLANTED + ("nicht öffentlich",):
        assert s not in blob, s
    assert not (out / "data/gen/ghidden.json").exists()


def test_leak_check_on_planted_summary(tmp_path):
    (tmp_path / "data/gen").mkdir(parents=True)
    (tmp_path / "data/gen/gx.json").write_text(json.dumps({
        "prompt": "a monkey with a key", "parameters": {"seed": 1, "key": 2},
        "workflow_path": "x", "note": "/home/EXAMPLE/wf.json", "worker": "10.1.2.3", "id": "generation:run-1",
        "api_key": "EXAMPLE_KEY"}))
    hits = "\n".join(leak_check(tmp_path))
    for s in ("'workflow_path'", "/home/EXAMPLE", "10.1.2.3", "generation:run-1", "'api_key'"):
        assert s in hits, s
    assert "monkey" not in hits and "'seed'" not in hits and "'key'" not in hits


# --- start source ("Startbild aus eigener Simulation") -------------------------------------------------

from weide.generation import review_start_source_sim, start_source_sim  # noqa: E402

SIM_SUMMARY = json.loads((Path(__file__).parent / "fixtures/publication_summary.json").read_text(encoding="utf-8"))


def test_start_source_summary_top_level_only_exact_value():
    assert start_source_sim(SIM_SUMMARY)                                       # API.md-shaped compose summary
    assert start_source_sim({"start_source": "isaac-sim"})
    for other in ({"start_source": None}, {}, {"start_source": "Isaac-Sim"}, {"start_source": "blender"},
                  {"start_source": ["isaac-sim"]}, {"start_source": True}, None, "isaac-sim"):
        assert not start_source_sim(other), other


def test_start_source_summary_segment_only():
    summary = {**SIM_SUMMARY, "start_source": None}
    assert start_source_sim(summary)                                          # segment 1 says isaac-sim
    summary["composition"] = {"segments": [{**s, "start_source": None} for s in SIM_SUMMARY["composition"]["segments"]]}
    assert not start_source_sim(summary)
    assert not start_source_sim({"composition": {"segments": [{"start_source": "other"}, "isaac-sim", None]}})


def test_start_source_summary_ignores_labels_and_parents():
    for leaky in ({"labels": {"start_source": "isaac-sim"}},
                  {"start_source": None, "labels": {"start_source": "isaac-sim"}},
                  {"parents": [{"start_source": "isaac-sim"}], "compose": {"parents": [{"labels": {"start_source": "isaac-sim"}}]}},
                  {"composition": {"segments": [{"labels": {"start_source": "isaac-sim"}},
                                                {"parents": [{"start_source": "isaac-sim"}]}]}}):
        assert not start_source_sim(leaky), leaky


def test_review_start_source_top_level_field_decides():
    assert review_start_source_sim({"start_source": "isaac-sim", "labels": {}})
    # the service's field wins over labels and parents (it already inherits through compose parents)
    assert not review_start_source_sim({"start_source": None, "labels": {"start_source": "isaac-sim"}})
    assert not review_start_source_sim({"start_source": "Isaac Sim"})
    feed = {"g_new": {"start_source": "isaac-sim"}, "g_newnull": {"start_source": None, "labels": {"start_source": "isaac-sim"}}}
    assert review_start_source_sim({"labels": {}, "compose": {"parents": ["g_new"]}}, feed.get)
    assert not review_start_source_sim({"labels": {}, "compose": {"parents": ["g_newnull"]}}, feed.get)


def test_review_start_source_fallback_labels_for_older_items():
    assert review_start_source_sim({"labels": {"start_source": "isaac-sim"}})
    for other in ({"labels": {}}, {}, {"labels": {"start_source": "Isaac-Sim"}}, {"labels": {"start_source": "blender"}},
                  {"labels": {"start_source": ["isaac-sim"]}}, {"labels": {"spec": "isaac-sim"}}, {"labels": None}):
        assert not review_start_source_sim(other), other
    nested = {"labels": {"story": "h001"}, "compose": {"parents": [
        {"segment": 1, "model": "LTX-2.5"},
        {"segment": 2, "models": ["Wan"], "parents": [{"labels": {"start_source": "isaac-sim"}}]}]}}
    assert review_start_source_sim(nested)
    nested["compose"]["parents"][1]["parents"][0]["labels"]["start_source"] = "other"
    assert not review_start_source_sim(nested)


def test_review_start_source_parent_ids_resolved_recursively():
    feed = {"g_a": {"labels": {}, "compose": {"parents": ["g_b"]}},
            "g_b": {"labels": {}, "compose": {"parents": [{"generation_id": "g_c"}]}},
            "g_c": {"labels": {"start_source": "isaac-sim"}},
            "g_loop": {"labels": {}, "compose": {"parents": ["g_loop", "g_missing"]}}}
    look = feed.get
    assert review_start_source_sim(feed["g_a"], look) and review_start_source_sim(feed["g_b"], look)
    assert not review_start_source_sim(feed["g_a"])                # ids alone say nothing without the feed
    assert not review_start_source_sim(feed["g_loop"], look)       # cycles and unknown ids end quietly
