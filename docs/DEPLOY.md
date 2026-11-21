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
updates the post. Media land in `media/gen/<public_id>.mp4`, posters in
`thumb/540/gen/<public_id>.jpg` (the service poster, scaled with ffmpeg; without a service poster a
video frame via ffmpeg; without ffmpeg only a JPEG service poster is kept as is).

Revocations (a top-level `revoked` list or items with `status: "revoked"`) unpublish the post with that
public_id: it goes back to draft and disappears from the API and the next export (the media file stays
on disk, but the export removes it from the site). Unknown ids are reported as notes. A malformed item
only fails itself; the others are still processed. The generation summary is sanitized again before it is stored, and
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
commit `site/`. Nothing is committed or pushed by the command. The exported "today" (ranking pages)
is the date of the newest published post unless `--today` is given, so the same data exports to the
same files on any day.

Then review and commit `site/` and push to `main`.

## GitHub Pages

`.github/workflows/pages.yml` deploys the committed `site/` directory on every push to `main` that
touches it (or by manual dispatch). It only takes effect once the repository owner sets
**Settings → Pages → Source** to **GitHub Actions**, planned for the repository rename. Until then the
existing Pages setup keeps serving the site as before.

`.github/workflows/ci.yml` runs the test suite on pushes and pull requests.
