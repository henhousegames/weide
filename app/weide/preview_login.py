"""Login without handling a raw key: owner auto-login over Tailscale and one-time login links.

Owner auto-login: a request whose *peer address* (the TCP connection, never X-Forwarded-For or any other
header) is a Tailscale address (100.64.0.0/10, fd7a:115c:a1e0::/48) is resolved with
`tailscale whois --json <ip>` (no shell, timeout); if the node's UserProfile.LoginName is listed in
`owner_tailscale_logins` of the private config file, the session cookie is set without asking. Results
are cached per IP for WHOIS_TTL seconds. Loopback never auto-logs in; a whois error, timeout, tagged
node or unknown user never grants anything (the login page is shown).

Config file (default ~/.config/henhouse/preview.toml, override HENHOUSE_PREVIEW_CONFIG; must not be
group/world-readable, otherwise it is ignored):
    owner_tailscale_logins = ["owner@example.com"]

One-time login links: `weide preview-login-link` stores sha256(nonce) + expiry (LINK_TTL) in a 600 file
next to the key (default ~/.config/henhouse/preview-links.json, override HENHOUSE_PREVIEW_LINKS_FILE)
and prints http://<host>:<port>/login#link=<nonce>. The nonce sits in the URL fragment, so it never
reaches a server log; the login page posts it to /login/link, where it is checked, consumed (single use)
and exchanged for the session cookie.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import shutil
import stat
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Callable, Iterator

from . import config
from .preview_auth import key_file

log = logging.getLogger("weide.preview")

TAILSCALE_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
WHOIS_TTL = 60.0
WHOIS_TIMEOUT = 3.0
LINK_TTL = 10 * 60


def config_file() -> Path:
    v = config.env("PREVIEW_CONFIG")
    return Path(v).expanduser() if v else key_file().parent / "preview.toml"


def links_file() -> Path:
    v = config.env("PREVIEW_LINKS_FILE")
    return Path(v).expanduser() if v else key_file().parent / "preview-links.json"


def owner_logins(path: Path | None = None) -> set[str]:
    """owner_tailscale_logins from the private config; empty (auto-login off) if missing or unsafe."""
    path = Path(path or config_file())
    if not path.is_file():
        return set()
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        log.warning("%s is readable by others; ignored (chmod 600 it to enable owner auto-login)", path.name)
        return set()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        log.warning("%s is not valid TOML; owner auto-login is off", path.name)
        return set()
    logins = data.get("owner_tailscale_logins") or []
    return {str(x).strip() for x in logins if isinstance(x, str) and x.strip()} if isinstance(logins, list) else set()


def tailscale_ip(host: str | None) -> str | None:
    """The peer address if it is a Tailscale address (IPv4-mapped IPv6 unwrapped), else None."""
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return None
    return str(ip) if any(ip in net for net in TAILSCALE_NETS if net.version == ip.version) else None


def run_whois(ip: str) -> dict | None:
    """`tailscale whois --json <ip>` (argv list, no shell). None on any error."""
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "whois", "--json", ip], capture_output=True, text=True, timeout=WHOIS_TIMEOUT,
                           check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class OwnerAutoLogin:
    def __init__(self, owners: set[str], whois: Callable[[str], dict | None] = run_whois, ttl: float = WHOIS_TTL,
                 clock: Callable[[], float] = time.monotonic):
        self.owners, self.whois, self.ttl, self.clock = set(owners), whois, ttl, clock
        self.cache: dict[str, tuple[float, str | None]] = {}

    def login_name(self, ip: str) -> str | None:
        now = self.clock()
        hit = self.cache.get(ip)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        try:
            data = self.whois(ip)
        except Exception:                                  # noqa: BLE001 - any failure denies
            data = None
        profile = data.get("UserProfile") if isinstance(data, dict) else None
        name = profile.get("LoginName") if isinstance(profile, dict) else None
        name = name if isinstance(name, str) and name else None
        if len(self.cache) > 1000:
            self.cache.clear()
        self.cache[ip] = (now, name)
        return name

    def grants(self, peer: str | None) -> bool:
        """True only for an owner's Tailscale node; loopback, other networks and errors deny."""
        if not self.owners:
            return False
        ip = tailscale_ip(peer)
        if ip is None:
            return False
        name = self.login_name(ip)
        return name is not None and name in self.owners


# --- one-time login links ------------------------------------------------------------------------------

def _hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


class LinkStore:
    """Hashed single-use nonces with expiry in a small 600 JSON file (shared by CLI and server)."""

    def __init__(self, path: Path | None = None, ttl: float = LINK_TTL, clock: Callable[[], float] = time.time):
        self.path, self.ttl, self.clock = Path(path or links_file()), ttl, clock

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        lock = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []
        items = data.get("links") if isinstance(data, dict) else None
        return [x for x in items or [] if isinstance(x, dict) and isinstance(x.get("hash"), str)]

    def _write(self, links: list[dict]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"links": links}, f)
        os.replace(tmp, self.path)

    def create(self) -> str:
        nonce = secrets.token_urlsafe(32)
        now = self.clock()
        with self._locked():
            links = [x for x in self._read() if float(x.get("expires", 0)) > now]
            links.append({"hash": _hash(nonce), "expires": now + self.ttl})
            self._write(links)
        return nonce

    def consume(self, nonce: str) -> bool:
        """True once for a valid, unexpired nonce; the nonce is removed either way."""
        if not nonce or len(nonce) > 200:
            return False
        h = _hash(nonce)
        now = self.clock()
        with self._locked():
            links = self._read()
            keep, ok = [], False
            for x in links:
                match = hmac.compare_digest(x["hash"], h)
                if match:
                    ok = float(x.get("expires", 0)) > now
                elif float(x.get("expires", 0)) > now:
                    keep.append(x)
            if len(keep) != len(links):
                self._write(keep)
        return ok


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines (comments, blank lines, optional quotes) of a systemd-style EnvironmentFile."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.removeprefix("export ").split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out
