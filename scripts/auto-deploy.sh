#!/usr/bin/env bash
# Automated publish: pull approved generations, rebuild site/, commit and push it to main.
#
#   scripts/auto-deploy.sh            run once (the systemd timer in scripts/systemd/ calls this)
#   scripts/auto-deploy.sh --dry-run  print the steps; pulls only with --dry-run, never writes,
#                                     commits, pushes or renames
#
# Steps: lock (no overlapping runs) -> repo must be on a clean main that fast-forwards to origin/main
# -> `weide deploy` (pull-approved + export into site/ + leak check; the pull is incremental, and a full
# one once per 24 h: see app/weide/deploy.py, HENHOUSE_FULL_PULL=1|0 forces either)
# -> until the one-time switch is done, stop here (no commit, no push) while the export has no published generation post
# -> if site/ changed: commit + push
# -> one-time switch (rename repo weide -> henhouse, Pages source = GitHub Actions, run pages.yml,
# wait until the site serves the newest published generation), guarded by a marker in private/.
#
# Settings (service URL/key) come from private/.env via the app; this script never prints the
# environment. GitHub access uses the existing git credentials and `gh` login.
set -euo pipefail

OLD_NAME=weide
NEW_NAME=henhouse
BRANCH=main
POLL_TIMEOUT_S=${HENHOUSE_POLL_TIMEOUT_S:-1800}
POLL_INTERVAL_S=20

DRY_RUN=0
case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  "") ;;
  -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
  *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_DIR"
PRIVATE_DIR=${HENHOUSE_PRIVATE_DIR:-private}
MARKER="$PRIVATE_DIR/.repo-switch-done"
LOCK="$PRIVATE_DIR/.auto-deploy.lock"

log() { printf '%s %s\n' "$(date -Iseconds)" "$*"; }
die() { log "ERROR: $*"; exit 1; }
# mutating command: printed in dry-run mode, executed otherwise
run() {
  if [ "$DRY_RUN" = 1 ]; then log "dry-run: would run: $*"; else "$@"; fi
}

mkdir -p "$PRIVATE_DIR"
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another auto-deploy run holds the lock; skipping"
  exit 0
fi

# --- repository state ---------------------------------------------------------------------------

sync_main() {
  local branch ahead behind
  branch=$(git symbolic-ref --quiet --short HEAD || true)
  [ "$branch" = "$BRANCH" ] || die "checked out '${branch:-detached}', expected $BRANCH (not switching automatically)"
  [ -z "$(git status --porcelain)" ] || die "working tree is dirty; commit or clean it first"
  git fetch --quiet origin "$BRANCH" || die "git fetch failed"
  ahead=$(git rev-list --count "origin/$BRANCH..$BRANCH")
  behind=$(git rev-list --count "$BRANCH..origin/$BRANCH")
  [ "$ahead" = 0 ] || die "$BRANCH has $ahead commit(s) not on origin/$BRANCH (diverged or unpushed)"
  if [ "$behind" != 0 ]; then
    log "fast-forwarding $BRANCH by $behind commit(s)"
    run git merge --quiet --ff-only "origin/$BRANCH" || die "fast-forward failed"
  fi
}

# --- build ----------------------------------------------------------------------------------------

# Sets SITE_CHANGED=1 when site/ differs from the committed state.
build_site() {
  SITE_CHANGED=0
  if [ "$DRY_RUN" = 1 ]; then
    log "dry-run: uv run weide pull-approved --dry-run --auto-full"
    uv run --quiet weide pull-approved --dry-run --auto-full 2>/dev/null \
      | jq -c '{full, new, updated, unchanged, revoked, failed: (.failed | length)}' \
      || die "pull-approved --dry-run failed"
    log "dry-run: would run: uv run weide deploy (export into site/, leak check)"
    return
  fi
  local out
  out=$(mktemp)
  if ! uv run --quiet weide deploy >"$out" 2>/dev/null; then
    log "deploy failed: $(jq -c '{errors: (.errors // [] | length), first_errors: (.errors // [] | .[:5]), pull: (.pull // {} | {new, updated, revoked})}' "$out" 2>/dev/null || echo 'no JSON output')"
    rm -f "$out"
    die "weide deploy failed; nothing committed"
  fi
  log "deploy: $(jq -c '{changed, changes, pull: (.pull // {} | {full, new, updated, unchanged, revoked}), posts: .export.posts, generations: .export.generations}' "$out")"
  rm -f "$out"
  if [ -n "$(git status --porcelain -- site)" ]; then SITE_CHANGED=1; fi
}

commit_and_push() {
  local before
  before=$(git rev-parse HEAD)
  run git add site
  run git commit --quiet -m "chore(site): publish approved posts" \
    -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
  if ! run git push --quiet origin "$BRANCH"; then
    # drop the unpushed commit so the next run starts clean (site/ is rebuilt from the database)
    git reset --quiet --hard "$before"
    die "git push failed; local commit dropped, will retry next run"
  fi
  log "pushed site/ to origin/$BRANCH"
}

# --- one-time switch --------------------------------------------------------------------------------

newest_public_id() {
  [ -f site/data/feed.json ] || return 0
  jq -r '[.items[]? | .generation.public_id? // empty] | first // empty' site/data/feed.json
}

pages_build_type() {   # prints workflow|legacy, or "none" when Pages is not set up
  local slug=$1 out
  if out=$(gh api "repos/$slug/pages" --jq '.build_type' 2>/dev/null); then echo "$out"; else echo none; fi
}

wait_for_site() {
  local url=$1 pid=$2 deadline code
  deadline=$(( $(date +%s) + POLL_TIMEOUT_S ))
  log "waiting for the site to serve $pid (timeout ${POLL_TIMEOUT_S}s)"
  while :; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$url" || true)
    if [ "$code" = 200 ] && curl -fsS "${url%/}/data/feed.json?t=$(date +%s)" 2>/dev/null | grep -q "\"$pid\""; then
      log "site is live (HTTP 200) and data/feed.json contains $pid"
      return 0
    fi
    [ "$(date +%s)" -lt "$deadline" ] || { log "last HTTP status: $code"; return 1; }
    sleep "$POLL_INTERVAL_S"
  done
}

one_time_switch() {
  [ -f "$MARKER" ] && return 0
  local name owner slug pid url remote build
  pid=$(newest_public_id)
  if [ -z "$pid" ]; then
    log "one-time switch postponed: the site has no published generation yet"
    return 0
  fi
  if ! git cat-file -e "origin/$BRANCH:site/index.html" 2>/dev/null; then
    [ "$DRY_RUN" = 1 ] || die "site/ is not on origin/$BRANCH; not switching"
    log "dry-run: site/ is not on origin/$BRANCH yet"
  fi
  name=$(gh repo view --json name --jq .name) || die "gh repo view failed"
  owner=$(gh repo view --json owner --jq .owner.login) || die "gh repo view failed"
  log "one-time switch: repo $owner/$name, newest public_id $pid"

  if [ "$name" = "$OLD_NAME" ]; then
    log "renaming repository $OLD_NAME -> $NEW_NAME"
    run gh repo rename "$NEW_NAME" --yes || die "gh repo rename failed"
  elif [ "$name" != "$NEW_NAME" ]; then
    die "unexpected repository name '$name'"
  fi
  slug="$owner/$NEW_NAME"
  [ "$DRY_RUN" = 1 ] && slug="$owner/$name"

  remote=$(git remote get-url origin)
  case "$remote" in
    */"$OLD_NAME".git|*/"$OLD_NAME"|*:"$OLD_NAME".git)
      run git remote set-url origin "$(printf '%s' "$remote" | sed -E "s#$OLD_NAME(\.git)?\$#$NEW_NAME\1#")"
      log "origin remote updated to the new name" ;;
  esac

  build=$(pages_build_type "$slug")
  log "Pages build_type: $build"
  case "$build" in
    workflow) ;;
    none) run gh api -X POST "repos/$slug/pages" -f build_type=workflow --silent || die "enabling Pages failed" ;;
    *)    run gh api -X PUT "repos/$slug/pages" -f build_type=workflow --silent || die "switching Pages to GitHub Actions failed" ;;
  esac

  run gh workflow run pages.yml --repo "$slug" --ref "$BRANCH" || die "dispatching pages.yml failed"
  log "pages.yml dispatched"
  if [ "$DRY_RUN" = 1 ]; then
    log "dry-run: would poll the Pages URL until it serves $pid, then write $MARKER"
    return 0
  fi
  sleep 10
  url=$(gh api "repos/$slug/pages" --jq .html_url) || die "reading the Pages URL failed"
  wait_for_site "$url" "$pid" || die "site did not come up with $pid in time (switch will be retried)"
  date -Iseconds >"$MARKER"
  log "one-time switch done (marker written)"
}

# --- main ---------------------------------------------------------------------------------------------

if [ "$DRY_RUN" = 1 ]; then log "auto-deploy start (dry-run)"; else log "auto-deploy start"; fi
sync_main
build_site
if [ ! -f "$MARKER" ] && [ -z "$(newest_public_id)" ]; then
  # before the first publication the timer is a no-op: no commit, no push, site/ back to HEAD
  if [ "$SITE_CHANGED" = 1 ]; then
    git checkout --quiet HEAD -- site 2>/dev/null || true
    git clean -fdq -- site
  fi
  log "no published generation yet; not committing site/ (switch pending)"
  log "auto-deploy done"
  exit 0
fi
if [ "$SITE_CHANGED" = 1 ]; then
  commit_and_push
else
  log "site/ unchanged; nothing to commit"
fi
one_time_switch
log "auto-deploy done"
