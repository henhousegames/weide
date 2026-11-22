# Deploy

The public site is a static export (`weide export-static`) served by GitHub Pages. Publishing runs
locally; GitHub only serves the committed `site/` directory.

## Settings

Copy `.env.example` to `.env` in the private dir (default `private/.env`, gitignored) or export the
variables in the environment:

| Variable | Meaning |
|---|---|
| `HENHOUSE_SERVICE_URL` | base URL of the generation service (`scheme://host:port`, without `/api/v1`) |
| `HENHOUSE_SERVICE_KEY` | bearer key with scope `publish` (read-only). Never printed or logged |

## Pull approved generations

```sh
uv run weide pull-approved --dry-run   # what would change
uv run weide pull-approved             # publish (incremental since the last successful pull)
uv run weide pull-approved --full      # ignore the cursor, reconcile every publication
```

The owner approves generations for publishing in the service (Lab UI or `publish-approval`, see the
service's docs/API.md "Publishing"); `pull-approved` reads `GET /api/v1/publications` with the
`publish`-scoped key and turns each publication into a published post of an existing character account
(it never creates accounts; run `seed-characters` first). `video/mp4` becomes a reel, `image/png`,
`image/jpeg` and `image/webp` an image post. Posts are matched by `generation:<public_id>` (the service
exposes no run id), so reruns are idempotent: an unchanged item is skipped, a new caption, character or
summary updates the post (it keeps its first post time). Media land in `media/gen/<public_id>.<ext>`
(the list's sha256 is verified), posters in `thumb/540/gen/<public_id>.jpg` (the service poster, scaled
with ffmpeg; else a frame of the media via ffmpeg; without ffmpeg only a JPEG service poster is kept).

Cursor: the service documents `revoked` as every withdrawal after `since`, so polling with `since` is
complete. After a fully successful pull, `next_since` is stored in the database meta
(`publications_since`); the next pull follows `next_since` page by page until a page is empty. If any
item fails, the cursor stays where it was and the item is fetched again next time. A fresh database
(or `--full`) fetches the full list.

Full pull (`--full`, or a database without a cursor): every publication is listed and its summary
fetched again, so credits and licence notices the service recomputed reach already-imported posts
(only changed records are written). It also re-applies revocations: ids in `revoked` are unpublished,
and so is every published generation post that is no longer in the listing at all (skipped when the
listing is empty or has an entry without a valid public_id). After a fully successful full pull the
time is stored in the database meta (`last_full_pull_at`, next to the cursor); a failed one does not
store it.

Revocations (`revoked`) unpublish the post with that public_id: it goes back to draft and disappears
from the API and the next export (the media file stays on disk, but the export removes it from the
site). A later re-approval (same public_id, newer `approved_at`) publishes it again. Unknown ids are
reported as notes. A malformed item only fails itself; the others are still processed.

The service's summary is sanitized again before it is stored. Inputs are kept only when the summary
marks them `published` (a reference to that publication's own post, shown once that post is published
here); every other input is shown as "privater Input". Input media are not downloaded. A failing item
leaves no post and no file behind; the command then exits non-zero.

Only `app/weide/publish.py` (`ServiceClient`, `Publication`) knows the API shape.

## Build the site

```sh
uv run weide deploy                # pull-approved -> export-static into site/ -> leak check
uv run weide deploy --full         # force a full pull (HENHOUSE_FULL_PULL=1 does the same)
uv run weide deploy --incremental  # never a full pull this time (HENHOUSE_FULL_PULL=0)
uv run weide deploy --skip-pull
```

Daily full pull: by default the pull is incremental, but a full one when `last_full_pull_at` is
missing or at least 24 h old (`app/weide/deploy.py`, `full_pull_due`). So `scripts/auto-deploy.sh`
(every 12 min via `henhouse-deploy.timer`) does one full pull a day without a timer of its own; a
failed full pull is retried on the next run. The JSON reports `pull.full`.
`scripts/auto-deploy.sh --dry-run` runs `pull-approved --dry-run --auto-full` (same decision, writes
nothing).

`scripts/auto-deploy.sh` never wedges on its own leftovers: `site/` is rebuilt from the database on
every run, so the script restores it to `HEAD` (`git checkout HEAD -- site`, `git clean -fd -- site`;
nothing outside `site/` is touched) at the start of each run and after a failed `weide deploy`. The
media `pull-approved` writes into the repository root (`/media/gen/`, `/thumb/*/gen/`) are gitignored
(anchored, so `site/media/gen/` stays committable) and not counted as local changes. Any other local
change (tracked or untracked) still stops the run.

The command prints JSON. `"changed": false` means `site/` is byte-identical to before, so automation
can skip the commit. `errors` (the publication listing could not be read, leak check hits) make it
exit non-zero; then do not commit `site/`. A single failed item (unknown character, bad media, leaky
credit, ...) is only a `warning`: its public_id is listed in `failed_items`, every other publication and
revocation is exported, and since the pull keeps its cursor (and does not record a full pull) the item
is retried on the next run. Nothing is committed or pushed by the command. The exported "today" (ranking pages)
is the date of the newest published post unless `--today` is given, so the same data exports to the
same files on any day. The export holds no build timestamps, so a full pull that changes nothing leaves
`site/` byte-identical and the automation commits nothing.

Then review and commit `site/` and push to `main`.

## GitHub Pages

`.github/workflows/pages.yml` deploys the committed `site/` directory on every push to `main` that
touches it (or by manual dispatch). It only takes effect once the repository owner sets
**Settings → Pages → Source** to **GitHub Actions**, planned for the repository rename. Until then the
existing Pages setup keeps serving the site as before.

`.github/workflows/ci.yml` runs the test suite on pushes and pull requests.
