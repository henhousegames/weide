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
  generation itself); anything else becomes {"private": true, "label": "privater Input"}.

`summary_leaks()` re-checks a summary with the same rules (used by the export leak check).
"""
from __future__ import annotations

import ast
import posixpath
import re
import sqlite3
from typing import Any, Callable
from urllib.parse import urlsplit

from .db import jload

PUBLIC_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REDACTED = "[redacted]"
PRIVATE_INPUT = "privater Input"
_TRAILING_PAREN = re.compile(r"\s*\([^()]*\)\s*$")


def model_short_label(model: Any) -> str | None:
    """Short, human model label for cards, captions, badges and summaries ("FLUX.2 Klein 4B").

    `model` is the service's {id, display_name} object or a plain string. Uses display_name (then name,
    then id) with a trailing parenthetical such as "(text + reference to image)" removed. Never returns
    the repr of a dict: a string that is one (stored by older code) is parsed back first."""
    if isinstance(model, str):
        text = model.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                return None
            return model_short_label(parsed) if isinstance(parsed, dict) else None
        short = _TRAILING_PAREN.sub("", text).strip()
        return short or text or None
    if isinstance(model, dict):
        for k in ("display_name", "name", "id"):
            label = model_short_label(model.get(k)) if isinstance(model.get(k), str) else None
            if label:
                return label
    return None


# --- credits ----------------------------------------------------------------------------------------
# The service's `credits` list on publication items, summaries and review items:
#   [{kind: "license_notice" | "audio_source", text, model, license: {id, url}}]
# A license_notice text is a licence obligation ("Powered by Stability AI"): it is shown wherever the
# model line is shown. Only these keys survive, every value must be a string, the license URL must be
# http(s) (the one URL a public summary may carry; summary_leaks checks it with these rules).

CREDITS_KEY = "credits"
CREDIT_FIELDS = ("kind", "text", "model")
_CREDIT_MAX = 500
_MAX_CREDITS = 50


def _credit_str(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    s = clean_string(" ".join(v.split()))
    return s[:_CREDIT_MAX] if s else None


def _license_url(v: Any) -> str | None:
    if not isinstance(v, str) or len(v) > 2000 or any(c.isspace() or ord(c) < 32 for c in v):
        return None
    try:
        u = urlsplit(v)
        host = u.hostname
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not host or u.username or u.password or "@" in u.netloc:
        return None
    if _IP.fullmatch(host) or not _HOST_PORT.sub("", host) or "." not in host:   # no IPs / internal hosts
        return None
    return v


def sanitize_credits(raw: Any) -> list[dict]:
    """The public form of a `credits` list: entries with a text, keys kind/text/model/license{id,url}
    only, strings only, http(s) license URL only. Missing/null/garbage -> []."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:_MAX_CREDITS]:
        if not isinstance(item, dict):
            continue
        c = {k: s for k in CREDIT_FIELDS if (s := _credit_str(item.get(k)))}
        if "text" not in c:
            continue                       # nothing to show
        lic = item.get("license")
        if isinstance(lic, dict):
            lic_out = {k: s for k, s in (("id", _credit_str(lic.get("id"))), ("url", _license_url(lic.get("url"))))
                       if s}
            if lic_out:
                c["license"] = lic_out
        if c not in out:
            out.append(c)
    return out


# Internal details a credit text/model must never carry to the public site (the service sometimes
# names its own takes and asset archives, e.g. "own generation: ... audio of gen_<hex> (h004 b4 t1)" or
# "pack (pack.zip) :: Audio/file.ogg"). Ingest keeps them (the preview is private); the export refuses.
_PATH_DIRS = (r"home|tmp|users|var|mnt|srv|opt|root|etc|data|private|workspace|outputs?|inputs?|assets|audio|sfx|"
              r"sounds?|samples|models?|ckpts?|checkpoints|loras?|runs?|takes?|renders?|comfyui|archives?")
_CREDIT_INTERNAL = re.compile(
    r"gen_[0-9a-f]{6,}"                                               # internal run id
    r"| :: "                                                          # archive member separator
    r"|\.(?:zip|7z|tar|gz|tgz|rar|ogg|flac|wav|mp3|m4a|aac|mp4|mov|webm|png|jpe?g|webp|json|ya?ml|safetensors|"
    r"ckpt|pt|pth|bin|gguf)\b"                                       # file names
    r"|(?<![\w.:/-])(?:~|[a-z]:)?/[\w.-]+/"                         # absolute path: /dir/... or ~/dir/...
    r"|(?<![\w./-])(?:" + _PATH_DIRS + r")[\\/][\w.-]"              # relative path in a known dir (Audio/x)
    r"|[a-z]:\\|\\\\|\w\\\w"                                     # Windows paths
    r"|\bh\d{3}\s+b\d+\s+t\d+\b",                                     # internal take label (h004 b4 t1)
    re.I)
# Not internal: "AC/DC", "24/7", "freesound.org/s/4711" (a slash alone is no path), a bare "h123".


def credit_internal(c: Any) -> bool:
    """True when a credit's text or model names internal run ids, take labels, files or paths."""
    return isinstance(c, dict) and any(isinstance(c.get(k), str) and _CREDIT_INTERNAL.search(c[k])
                                       for k in ("text", "model"))


def credits_leaks(obj: Any, where: str, hits: list[str], post: str | None = None) -> None:
    """Leak check for a credits list: it must be exactly its sanitized form, and no text/model may
    carry internal details (credit_internal). The hit names the post's public_id, never the text."""
    if not isinstance(obj, list) or sanitize_credits(obj) != obj:
        hits.append(f"{where}: credits not in public form {str(obj)[:80]!r}")
        return
    n = sum(1 for c in obj if credit_internal(c))
    if n:
        hits.append(f"{where}: post {post or '?'}: {n} credit(s) with internal details "
                    "(run id, take label, file name or path) - fix in the video-service")


# --- start source -----------------------------------------------------------------------------------

START_SOURCE_LABEL = "start_source"
START_SOURCE_SIM = "isaac-sim"           # the only value that counts (exact match)
START_SOURCE_NOTE = "Startbild aus eigener Simulation"
START_SOURCE_KEY = "start_source_sim"    # boolean in generation_json / the public card and summary
# where an older review item (without the service's start_source field) names its parents
_PARENT_KEYS = ("parents", "compose", "composition", "segments", "effective")
_PARENT_ID_KEYS = ("generation_id", "id", "source")
_MAX_DEPTH = 32


def _is_sim(v: Any) -> bool:
    return isinstance(v, str) and v == START_SOURCE_SIM


def start_source_sim(summary: Any) -> bool:
    """Publications path: True when the service's whitelisted field says `start_source: "isaac-sim"`.

    `summary` is a public summary (or a publication list item, or our stored generation_json). Only the
    field `start_source` counts: at the top level (the service already inherits it from any compose
    parent at any depth) or on any `composition.segments[]` (per cut). Labels are never read here."""
    if not isinstance(summary, dict):
        return False
    if _is_sim(summary.get(START_SOURCE_LABEL)):
        return True
    comp = summary.get("composition")
    segments = comp.get("segments") if isinstance(comp, dict) else None
    return isinstance(segments, list) and any(isinstance(s, dict) and _is_sim(s.get(START_SOURCE_LABEL))
                                              for s in segments)


def review_start_source_sim(record: Any, lookup: Callable[[str], Any] | None = None) -> bool:
    """Review/preview path: the item's top-level `start_source` (already inherited by the service) decides
    when the item has that field. Older review items without it fall back to `labels.start_source` on the
    item or any parent of a compose (`parents`, `compose.parents`, `effective.parents`,
    `composition.segments`, recursively); a parent given only by its generation id is resolved with
    `lookup(id)` (the review feed), and a parent that has the field is decided by it."""
    seen: set[str] = set()

    def walk(obj: Any, depth: int) -> bool:
        if depth > _MAX_DEPTH:
            return False
        if isinstance(obj, str):
            if lookup is None or obj in seen:
                return False
            seen.add(obj)
            return walk(lookup(obj), depth + 1)
        if isinstance(obj, (list, tuple)):
            return any(walk(x, depth + 1) for x in obj)
        if not isinstance(obj, dict):
            return False
        if START_SOURCE_LABEL in obj:                 # the service's field: authoritative (null = no)
            return _is_sim(obj[START_SOURCE_LABEL])
        labels = obj.get("labels")
        if isinstance(labels, dict) and _is_sim(labels.get(START_SOURCE_LABEL)):
            return True
        for k in _PARENT_KEYS:
            if k in obj and walk(obj[k], depth + 1):
                return True
        if depth and lookup is not None:          # a parent entry: also look its generation up by id
            gid = next((obj[k] for k in _PARENT_ID_KEYS if isinstance(obj.get(k), str)), None)
            if gid and walk(gid, depth + 1):
                return True
        return False

    return walk(record, 0)


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
    body = sanitize({k: v for k, v in gen.items() if k not in ("inputs", "public_id", "post_id", CREDITS_KEY)})
    out = {"public_id": row["public_id"], "post_id": row["id"], **body}
    if CREDITS_KEY in gen:                 # records imported before credits existed have none
        out[CREDITS_KEY] = sanitize_credits(gen.get(CREDITS_KEY))
    label = model_short_label(gen.get("model_label")) or model_short_label(gen.get("model"))
    if label:
        out["model_label"] = label
    if gen.get(START_SOURCE_KEY) is True or start_source_sim(gen):
        out[START_SOURCE_KEY] = True
    else:
        out.pop(START_SOURCE_KEY, None)
    if raw_inputs is not None:
        items = raw_inputs if isinstance(raw_inputs, list) else [raw_inputs]
        out["inputs"] = [public_input(con, x) for x in items]
    return out


def summary_leaks(obj: Any, where: str, hits: list[str], *, top: bool = True, post: str | None = None) -> None:
    """Leak scan for a public summary: private key names, paths, URLs with hosts, IPs, tokens, run ids.
    Prompts and settings (seed, steps, a parameter called `key` ...) are allowed. The top-level
    `credits` list is checked by credits_leaks (its license URL is public)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if is_private_key(k):
                hits.append(f"{where}: private key {k!r}")
            if top and k == CREDITS_KEY:
                credits_leaks(v, where, hits, post)
                continue
            summary_leaks(v, where, hits, top=False)
    elif isinstance(obj, list):
        for x in obj:
            summary_leaks(x, where, hits, top=False)
    elif isinstance(obj, str):
        if clean_string(obj) != obj:
            hits.append(f"{where}: private value {obj[:80]!r}")
