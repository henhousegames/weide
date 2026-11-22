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
#           HENHOUSE_PREVIEW_CONFIG, HENHOUSE_PREVIEW_LINKS_FILE,
#           HENHOUSE_FEED_DIR, HENHOUSE_CHARACTERS_DIR (curated feed, see below)
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

The shape of the service's review API (`GET /api/v1/review/generations`, see the service's
docs/API.md, "Review (private preview)") is handled in `app/weide/review.py` (`ReviewClient` and
`ReviewItem.parse`). The service is polled every 60 s for what finished after the `since` cursor (paging
while `has_more`); every 10 min the full list is re-read, since a later approval or revocation of an item
already seen does not come back through the cursor. The state is kept in memory only.

## Curated feed (`HENHOUSE_FEED_DIR`)

Without `HENHOUSE_FEED_DIR` the preview behaves as described above. With it, the preview shows **only**
published posts (henhouse.db) plus the posts of the feed dir; drafts and approvals are gone everywhere
(feed, profiles, counts): `#/p/vs-<id>` is 404, `studio-entwurf` is 404, draft media are not streamed.
The feed dir is written by another tool and only ever read here (`app/weide/feed.py`); its content never
reaches the static export or the public site. Publishing is unchanged (owner approval in the Lab).

| Variable | Meaning |
|---|---|
| `HENHOUSE_FEED_DIR` | dir with `posts.jsonl` and `interactions.jsonl` (no `WEIDE_` fallback) |
| `HENHOUSE_CHARACTERS_DIR` | character identities `<slug>/identity.md`; default `<feed dir>/../characters` |

**`posts.jsonl`** – one object per line: `id`, `generation_id`, `author` (character slug), `kind`
(`video`|`image`), `caption`, `posted_at` (ISO 8601 with offset), optional `story`, optional `hidden`.
A later line with the same `id` replaces the earlier one. A post is shown when `posted_at <= now`,
`hidden` is not true and the generation is readable through the review API (a listed item with media).

**`interactions.jsonl`** – `type` (`comment`|`like`), `post` (feed post id), `author` (slug), `text`
(comments), `at` (with offset). Counted when `at <= now` and the post is shown. Comments appear as normal
comments of the post; likes count into `reactions.heart` (one per character and post) and `like_count`.
The visitor's own reactions stay local in the browser, as before.

Malformed lines, naive timestamps, unknown kinds/types and comments without text are skipped (one log
line with file, line number and reason; never the content). Files are re-read when mtime or size change.
"now" is the real time.

### Characters (`identity.md`)

Only the YAML front matter between the first two `---` lines is read (`name`, `slug`, `handle`, `kind`
`animal|robot|thing`, `bio`, `avatar` = generation id of an image); simple `key: value` lines, quoted or
plain. The reader stops at the closing `---`: the markdown **body (private notes) is never read**, logged
or returned. `kind: thing` becomes account kind `other`; `bio` is cut to 150 characters.

**Merge rule:** the database account is the base. The front matter overrides `display_name` (`name`),
`handle`, `bio` and the avatar (served via `/preview/media/<generation id>/output`) for the preview only,
never written back; kind, colour, followers, the in-world profile and the robot flag stay from the
database (account briefs inside published cards, comments and profiles get the same overrides). A slug
that is not in the database becomes a preview-only account built from the front matter
(`preview_only: true`, kind from the front matter, `robot: true` for robots, so the disclaimer shows);
a feed author without identity.md and database row gets a plain account named after the slug.

### API (curated mode)

- `GET /api/feed` – published cards and feed cards, newest first (`posted_at`). Feed card = the published
  card shape plus: `id` `"fd-<post id>"`, `draft: false` (no `preview` key, so no ENTWURF pill),
  `story` (string or null), `lab_url` (Lab run page, for a "…" menu), `like_count`, `comment_count`,
  `generation: {public_id: null, model_label, credits, start_source_sim?}`, `feed: {post_id,
  generation_id, model, task, width, height, fps, duration_s, has_audio, created_at}`.
- `GET /api/posts/fd-<id>` – the card plus `comments` (`{id, text, created_at, source: "feed",
  reply_to: null, account, replies: []}`), `moments: []`, `parent: null`.
- `GET /api/accounts` – database accounts (with overrides) and preview-only characters; `posts` counts
  published + shown feed posts.
- `GET /api/accounts/<slug>` – profile with overrides; `posts` merges published and feed posts (newest
  first), `post_count`; `preview_only: true` for characters without a database row.
- `GET /api/meta` – `posts` includes the shown feed posts.
- `GET /api/preview/status` – `feed: {posts, waiting (due, generation not readable), scheduled, hidden,
  skipped_lines}` (`null` without a feed dir).
- `GET /api/preview/stories` (also without a feed dir: published posts only) – story rings:
  `{day, items: [{account, latest_at, post_ids, stories}]}`, one item per character with a shown post on
  `day` (HENHOUSE_TODAY, else today in Europe/Berlin), newest first; `stories` = distinct `story` values.
- `/api/top` stays published-only.

Draft and approval cards (without a feed dir) now also carry top-level `draft` and `lab_url`.
