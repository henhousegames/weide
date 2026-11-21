"""Public generation summaries: how a published post was made, with every setting but nothing private.

posts.generation_json is the internal record a generation run stores (model, version, license, task,
preset, parameters such as seed/steps/sampler/sigmas/cfg/resolution/fps/frames/keyframe strengths/
quantization, prompt, the workflow graph, inputs, and possibly internal bookkeeping). `public_summary()`
turns it into the public summary served at /api/generations/<public_id> and exported to
data/gen/<public_id>.json:

* keys that name secrets or machine details are dropped wherever they occur: api keys, tokens,
  secrets, passwords, agent and run ids, logs, hosts/IPs and file-system locations (path, paths, file,
  filename, *_path, *_file, *_dir ...). A key that is merely called `key` (a workflow parameter) stays.
* string values are cleaned: absolute paths and URLs become their basename, IP addresses, host:port
  pairs and token-looking strings become "[redacted]", internal run ids (`generation:<run>`) are removed.
* inputs are listed only when they are public themselves (a published post, or produced by the
  generation itself); anything else becomes {"private": true, "label": "private input"}.

`summary_leaks()` re-checks a summary with the same rules (used by the export leak check).
"""
from __future__ import annotations

import posixpath
import re
import sqlite3
from typing import Any

from .db import jload

PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REDACTED = "[redacted]"
PRIVATE_INPUT = "private input"

_DROP_EXACT = {
    "path", "paths", "file", "files", "filename", "filenames", "filepath", "dir", "directory", "cwd", "log", "logs",
    "api_key", "apikey", "token", "tokens", "access_token", "refresh_token", "auth", "authorization", "secret",
    "secrets", "password", "passwd", "credentials", "agent", "agent_id", "agentid", "run_id", "runid",
    "key_hash", "host", "hostname", "ip", "ip_address", "user", "username", "machine", "node_name",
}
_DROP_SUFFIX = ("_path", "_paths", "_file", "_files", "_filename", "_dir", "_log", "_logs", "_token", "_secret",
                "_password", "_run_id", "_agent_id", "_host", "_hostname", "_ip")
_SECRET_KEY = re.compile(r"(^|_)(api|secret|private|access|auth|ssh)_?key$")


def _norm_key(k: str) -> str:
    k = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(k))          # camelCase -> camel_case
    return k.lower().replace("-", "_").replace(" ", "_")


def is_private_key(k: str) -> bool:
    n = _norm_key(k)
    return n in _DROP_EXACT or n.endswith(_DROP_SUFFIX) or bool(_SECRET_KEY.search(n))


# --- values -------------------------------------------------------------------------------------------

_WHOLE_PATH = re.compile(r"^(~?/|[A-Za-z]:[\\/]|\\\\|file://)")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.I)
_EMBEDDED_PATH = re.compile(r"(?<![\w/.:])(~?/[^\s\"'<>/]+(?:/[^\s\"'<>/]*)+|[A-Za-z]:\\[^\s\"'<>]+|\\\\[^\s\"'<>]+)")
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[0-9a-f]{1,4}:){4,7}[0-9a-f]{1,4}\b", re.I)
_HOST_PORT = re.compile(r"\b(?:localhost|[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:local|lan|internal|home|corp))(?::\d{2,5})?\b"
                        r"|\b(?:[a-z0-9-]+\.)+[a-z]{2,}:\d{2,5}\b", re.I)
_TOKEN = re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghs|github_pat|hf|xox[abp])[-_][A-Za-z0-9_-]{12,}\b|\bBearer\s+\S+|"
                    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+")
_RUN_ID = re.compile(r"\bgeneration:\S+")


def _basename(s: str) -> str:
    s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/\\")
    return posixpath.basename(s.replace("\\", "/"))


def clean_string(s: str) -> str | None:
    """Public form of one string value; None when nothing public is left."""
    if s.startswith("generation:"):
        return None
    if _WHOLE_PATH.match(s) or _URL.fullmatch(s):
        return _basename(s) or None
    s = _URL.sub(lambda m: _basename(m.group(0)) or REDACTED, s)
    s = _EMBEDDED_PATH.sub(lambda m: _basename(m.group(0)) or REDACTED, s)
    s = _TOKEN.sub(REDACTED, s)
    s = _IP.sub(REDACTED, s)
    s = _HOST_PORT.sub(REDACTED, s)
    s = _RUN_ID.sub(REDACTED, s)
    return s


def sanitize(obj: Any) -> Any:
    """Recursively drop private keys and clean string values (see module doc). Empty results vanish."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if is_private_key(k):
                continue
            v = sanitize(v)
            if v is not None:
                out[str(k)] = v
        return out
    if isinstance(obj, (list, tuple)):
        return [x for x in (sanitize(v) for v in obj) if x is not None]
    if isinstance(obj, str):
        return clean_string(obj)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return clean_string(str(obj))


# --- inputs --------------------------------------------------------------------------------------------

def _published(con: sqlite3.Connection, ref: dict) -> sqlite3.Row | None:
    if ref.get("post_id") is not None and str(ref["post_id"]).isdigit():
        return con.execute("SELECT id, public_id, generation_json FROM posts WHERE id=? AND status='published'",
                           (int(ref["post_id"]),)).fetchone()
    if ref.get("public_id"):
        return con.execute("SELECT id, public_id, generation_json FROM posts WHERE public_id=? AND status='published'",
                           (str(ref["public_id"]),)).fetchone()
    return None


def public_input(con: sqlite3.Connection, item: Any) -> dict:
    """An input is shown when it is a published post (by post_id/public_id) or was produced by the
    generation itself (`generated: true`); everything else is a private input (role/kind kept)."""
    ref = item if isinstance(item, dict) else {}
    base = {k: v for k, v in sanitize({k: ref.get(k) for k in ("role", "kind", "type")}).items() if v}
    row = _published(con, ref)
    if row is not None:
        out = {**base, "post_id": row["id"], "label": clean_string(str(ref.get("label") or "")) or f"Post {row['id']}"}
        if row["public_id"] and row["generation_json"]:
            out["public_id"] = row["public_id"]
        return out
    if ref.get("generated") is True and not ref.get("post_id") and not ref.get("public_id"):
        rest = sanitize({k: v for k, v in ref.items() if k not in ("role", "kind", "type", "generated")})
        return {**base, "generated": True, **rest}
    return {**base, "private": True, "label": PRIVATE_INPUT}


# --- summary --------------------------------------------------------------------------------------------

def public_summary(con: sqlite3.Connection, row: sqlite3.Row) -> dict | None:
    """Public summary of a published post's generation record (None for legacy posts)."""
    gen = jload(row["generation_json"], None)
    if not isinstance(gen, dict) or not row["public_id"]:
        return None
    raw_inputs = gen.get("inputs")
    body = sanitize({k: v for k, v in gen.items() if k not in ("inputs", "public_id", "post_id")})
    out = {"public_id": row["public_id"], "post_id": row["id"], **body}
    if raw_inputs is not None:
        items = raw_inputs if isinstance(raw_inputs, list) else [raw_inputs]
        out["inputs"] = [public_input(con, x) for x in items]
    return out


def summary_leaks(obj: Any, where: str, hits: list[str]) -> None:
    """Leak scan for a public summary: private key names, paths, URLs with hosts, IPs, tokens, run ids.
    Prompts and settings (seed, steps, a parameter called `key` ...) are allowed."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if is_private_key(k):
                hits.append(f"{where}: private key {k!r}")
            summary_leaks(v, where, hits)
    elif isinstance(obj, list):
        for x in obj:
            summary_leaks(x, where, hits)
    elif isinstance(obj, str):
        if clean_string(obj) != obj:
            hits.append(f"{where}: private value {obj[:80]!r}")
