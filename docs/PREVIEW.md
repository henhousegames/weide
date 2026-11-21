# Preview

`weide serve --preview` is a private, always-on view of the feed for the owner. It shows, in the
normal feed look with a fixed **PREVIEW** banner:

1. published posts (as on the public site, no marker);
2. generations approved in the service but not deployed yet: marker **FREIGEGEBEN – noch nicht live**;
3. drafts (`ready_for_review` generations without an active approval): marker **ENTWURF**, linking to the
   Lab run page on the same host the browser used (port 18500).

Failed and cancelled generations are never shown. A draft's author is its `labels.character` when that
is an existing account, else the pseudo account "Studio (Entwurf)" (not stored in the database); its
caption is `labels.caption`, else model name and date. Cards are ordered newest first.

## Safety

- `henhouse.db` is opened read-only (SQLite `mode=ro`) and never migrated; every non-GET request is
  refused (405, reactions 403). The preview never exports, deploys, publishes or runs git, and drops
  `HENHOUSE_SERVICE_KEY` (the publish key) from its environment.
- The service is read with a separate read-only `review` key. Draft media are streamed through the
  preview (`/preview/media/...`, Range supported); the key never reaches the browser or a log.
- Every path needs the session cookie, loopback included (HttpOnly, SameSite=Strict, Path=/, one year;
  not `Secure`, the preview is plain HTTP over the tailnet). The cookie value is an HMAC of the preview
  key, never the key itself. Login POSTs must come from the same origin and are rate-limited.
- It listens only on the listed addresses; `0.0.0.0` / `::` are refused.
- Preview assets live in `app/weide/preview_static/`, outside `static/`, so the public export is unchanged.

## Logging in

- **Owner auto-login (Tailscale):** a request from a Tailscale address (the TCP peer, never forwarding
  headers) is checked with `tailscale whois`; if the login is listed in the config file, the cookie is set
  without asking. Loopback never auto-logs in; whois errors deny.
- **One-time link:** `uv run weide preview-login-link [--host <host>]` prints
  `http://<host>:18510/login#link=<nonce>` (single use, 10 minutes). The nonce stays in the URL fragment,
  so it is never logged.
- **Fallback:** the key form at `/login/token` (not linked) with the content of the key file.

## Files (private, never committed)

| File | Content |
|---|---|
| `~/.config/henhouse/preview.key` | login key, created by `weide preview-key` (dir 700, file 600; `--rotate` replaces it and logs everyone out; restart the service afterwards) |
| `~/.config/henhouse/preview.toml` | owner logins for auto-login; must be mode 600 or it is ignored |
| `~/.config/henhouse/preview-links.json` | hashed one-time link nonces (written by the CLI, consumed by the server) |
| `~/.config/henhouse/preview.env` | systemd EnvironmentFile (mode 600) |

`preview.toml`:

```toml
owner_tailscale_logins = ["owner@example.com"]
```

`preview.env`:

```sh
HENHOUSE_PREVIEW_HOSTS=127.0.0.1,<tailscale-ip>
HENHOUSE_SERVICE_URL=http://<service-host>:18500
HENHOUSE_REVIEW_KEY=<read-only review key>
# optional: HENHOUSE_PREVIEW_PORT=18510, HENHOUSE_DB=<path>, HENHOUSE_PREVIEW_KEY_FILE,
#           HENHOUSE_PREVIEW_CONFIG, HENHOUSE_PREVIEW_LINKS_FILE
```

Without `HENHOUSE_SERVICE_URL` / `HENHOUSE_REVIEW_KEY` the preview shows published posts only.

## Install (systemd user unit)

```sh
uv sync
uv run weide preview-key
install -m 600 /dev/null ~/.config/henhouse/preview.env     # then fill it in
install -m 600 /dev/null ~/.config/henhouse/preview.toml    # then add the owner login
cp scripts/systemd/henhouse-preview.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now henhouse-preview.service
journalctl --user -u henhouse-preview -f
```

## Review API

The service's review API is not final; the assumed shape is documented in `app/weide/review.py`
(`ReviewClient` and `ReviewItem.parse` are the only places that need to change). The service is polled
every 60 s (`since` cursor, with a full refresh every 30 polls); the state is kept in memory only.
