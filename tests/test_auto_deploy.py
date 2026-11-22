"""scripts/auto-deploy.sh in a throwaway clone with a local bare origin, a fake `uv` (stands in for
`weide deploy`) and a fake `gh`. Nothing here talks to GitHub or a real service."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from .conftest import REPO

pytestmark = pytest.mark.skipif(not all(shutil.which(t) for t in ("git", "jq", "flock", "bash")),
                                reason="needs git, jq, flock and bash")

FAKE_UV = r"""#!/usr/bin/env bash
# fake `uv run --quiet weide deploy`: FAKE_UV_MODE=ok|fail|noop
set -e
mode=${FAKE_UV_MODE:-ok}
mkdir -p media/gen thumb/540/gen
echo media > media/gen/pub_A.mp4             # what pull-approved writes into the repo root
echo poster > thumb/540/gen/pub_A.jpg
if [ "$mode" = fail ]; then
  mkdir -p site/data && echo partial > site/data/partial.json && echo changed >> site/index.html
  echo '{"errors": ["leak: x"], "pull": {"new": 0}}'
  exit 1
fi
if [ "$mode" = ok ]; then
  mkdir -p site/data
  echo '<html>' > site/index.html
  echo "{\"items\": [{\"generation\": {\"public_id\": \"${FAKE_PID:-pub_A}\"}}]}" > site/data/feed.json
fi
echo '{"changed": true, "changes": {}, "pull": {}, "export": {"posts": 1, "generations": 1}}'
"""

FAKE_GH = r"""#!/usr/bin/env bash
echo "gh $*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "repo view") case "$*" in *".name"*) echo henhouse ;; *owner*) echo example-owner ;; esac ;;
  "api repos/"*)
    case "${FAKE_GH_PAGES:-workflow}" in
      down) echo "error connecting to api.github.com" >&2; exit 1 ;;
      404) echo '{"message":"Not Found"}'; echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
      *) echo "$FAKE_GH_PAGES" ;;
    esac ;;
  "api -X") exit 0 ;;
  "workflow run") exit 1 ;;            # stop the switch right after the Pages step
  *) exit 1 ;;
esac
"""


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True).stdout


@pytest.fixture
def deploy(tmp_path):
    origin, clone, bin_, private = (tmp_path / n for n in ("origin.git", "clone", "bin", "private"))
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test", "HENHOUSE_PRIVATE_DIR": str(private),
           "FAKE_GH_LOG": str(tmp_path / "gh.log"), "PATH": f"{bin_}:{os.environ['PATH']}"}
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    git(clone, "config", "user.name", "t")
    git(clone, "config", "user.email", "t@example.test")
    (clone / "scripts").mkdir()
    shutil.copy(REPO / "scripts/auto-deploy.sh", clone / "scripts/auto-deploy.sh")
    shutil.copy(REPO / ".gitignore", clone / ".gitignore")
    (clone / "README.md").write_text("keep me\n")
    git(clone, "add", ".")
    git(clone, "commit", "-qm", "init")
    git(clone, "remote", "add", "origin", str(origin))
    git(clone, "push", "-q", "origin", "main")
    bin_.mkdir()
    for name, body in (("uv", FAKE_UV), ("gh", FAKE_GH)):
        (bin_ / name).write_text(body)
        (bin_ / name).chmod(0o755)
    private.mkdir()
    (private / ".repo-switch-done").write_text("done\n")

    def run(**extra):
        r = subprocess.run(["bash", str(clone / "scripts/auto-deploy.sh")], cwd=clone, env={**env, **extra},
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    run.clone, run.origin, run.private, run.gh_log = clone, origin, private, tmp_path / "gh.log"
    return run


def test_generated_media_is_ignored_but_site_media_is_committable():
    paths = ["media/gen/a.mp4", "thumb/540/gen/a.jpg", "site/media/gen/a.mp4", "site/thumb/540/gen/a.jpg",
             "media/x.mp4", "thumb/540/x.jpg"]
    r = subprocess.run(["git", "check-ignore", "--no-index", *paths], cwd=REPO, capture_output=True, text=True)
    assert r.stdout.split() == ["media/gen/a.mp4", "thumb/540/gen/a.jpg"]


def test_publish_then_clean_second_run(deploy):
    code, out = deploy()
    assert code == 0, out
    assert "pushed site/" in out
    assert git(deploy.clone, "status", "--porcelain") == ""            # media/gen + thumb/*/gen ignored
    assert git(deploy.origin, "ls-tree", "-r", "--name-only", "main", "site").split() == \
        ["site/data/feed.json", "site/index.html"]
    head = git(deploy.clone, "rev-parse", "HEAD")
    code, out = deploy()                                               # same data: nothing to commit
    assert code == 0, out
    assert "site/ unchanged" in out and git(deploy.clone, "rev-parse", "HEAD") == head


def test_failed_deploy_restores_site_and_next_run_recovers(deploy):
    assert deploy()[0] == 0
    code, out = deploy(FAKE_UV_MODE="fail")
    assert code != 0 and "weide deploy failed" in out
    assert git(deploy.clone, "status", "--porcelain") == ""            # site/ back to HEAD
    assert (deploy.clone / "README.md").read_text() == "keep me\n"
    code, out = deploy(FAKE_PID="pub_B")
    assert code == 0, out
    assert "pushed site/" in out


def test_leftover_site_changes_from_a_killed_run_are_dropped(deploy):
    assert deploy()[0] == 0
    (deploy.clone / "site/index.html").write_text("half written")
    (deploy.clone / "site/stray.json").write_text("{}")
    code, out = deploy(FAKE_UV_MODE="noop")
    assert code == 0, out
    assert (deploy.clone / "site/index.html").read_text() == "<html>\n" and not (deploy.clone / "site/stray.json").exists()


def test_changes_outside_site_still_stop_the_run(deploy):
    assert deploy()[0] == 0
    (deploy.clone / "README.md").write_text("local edit\n")
    (deploy.clone / "notes.txt").write_text("untracked\n")
    code, out = deploy()
    assert code != 0 and "working tree is dirty" in out
    assert (deploy.clone / "README.md").read_text() == "local edit\n" and (deploy.clone / "notes.txt").exists()


def test_old_gitignore_with_generated_media_still_runs(deploy):
    """A clone whose .gitignore predates the media patterns must not block itself."""
    gi = deploy.clone / ".gitignore"
    gi.write_text("".join(line for line in gi.read_text().splitlines(keepends=True) if "/gen/" not in line))
    git(deploy.clone, "commit", "-qam", "old ignore")
    git(deploy.clone, "push", "-q", "origin", "main")
    assert deploy()[0] == 0
    code, out = deploy()
    assert code == 0, out


@pytest.mark.parametrize("pages,posted", [("down", False), ("404", True)])
def test_pages_lookup_failure_is_not_none(deploy, pages, posted):
    assert deploy()[0] == 0
    (deploy.private / ".repo-switch-done").unlink()
    code, out = deploy(FAKE_UV_MODE="noop", FAKE_GH_PAGES=pages)
    assert code != 0                       # down: stops at the lookup; 404: Pages created, dispatch (fake) fails
    calls = deploy.gh_log.read_text()
    assert ("api -X POST" in calls) is posted
    if not posted:
        assert "reading the Pages settings failed" in out
    assert not (deploy.private / ".repo-switch-done").exists()
