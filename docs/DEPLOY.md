# Deploy

The public site is a static export (`weide export-static`) served by GitHub Pages. Publishing runs
locally; GitHub only serves the committed `site/` directory.

## Settings

Copy `.env.example` to `.env` in the private dir (default `private/.env`, gitignored) or export the
variables in the environment:

| Variable | Meaning |
|---|---|
| `HENHOUSE_SERVICE_URL` | base URL of the generation service |
| `HENHOUSE_SERVICE_KEY` | bearer key with scope `publish` (read-only). Never printed or logged |

## Pull approved generations

```sh
uv run weide pull-approved --dry-run   # what would change
uv run weide pull-approved             # publish
```

Generations are approved in the service's Lab UI; `pull-approved` turns each approved item into a
published post of an existing character account (it never creates accounts; run `seed-characters`
first). Reruns are idempotent: an unchanged item is skipped, a changed caption, title or summary
updates the post. Media land in `media/gen/<public_id>.mp4`, posters (ffmpeg, optional) in
`thumb/540/gen/<public_id>.jpg`. The generation summary is sanitized again before it is stored, and
the internal run id stays out of every public file. A failing item leaves no post and no file behind;
the command then exits non-zero.

The service's publish API is not final; its assumed shape is documented in `app/weide/publish.py`
(`ServiceClient` is the only place that needs to change).

## Build the site

```sh
uv run weide deploy            # pull-approved -> export-static into site/ -> leak check
uv run weide deploy --skip-pull
```

The command prints JSON. `"changed": false` means `site/` is byte-identical to before, so automation
can skip the commit. `errors` (pull failures, leak check hits) make it exit non-zero; then do not
commit `site/`. Nothing is committed or pushed by the command. Note that the exported "today"
(ranking pages) follows the local date unless `--today` is given, so a new day is a real change.

Then review and commit `site/` and push to `main`.

## GitHub Pages

`.github/workflows/pages.yml` deploys the committed `site/` directory on every push to `main` that
touches it (or by manual dispatch). It only takes effect once the repository owner sets
**Settings → Pages → Source** to **GitHub Actions**, planned for the repository rename. Until then the
existing Pages setup keeps serving the site as before.

`.github/workflows/ci.yml` runs the test suite on pushes and pull requests.
