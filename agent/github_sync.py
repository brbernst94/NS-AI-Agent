"""Auto-sync knowledge base and data to GitHub after training."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "brbernst94/NS-AI-Agent")
# DATA_BRANCH is a separate branch from the code branch Railway watches.
# Pushing data here NEVER triggers a Railway redeploy.
DATA_BRANCH = os.getenv("GITHUB_DATA_BRANCH", "ns-ai-data")
WORKDIR = "/app"

# Separate git directory used exclusively for data syncing.
# GIT_DIR points here, GIT_WORK_TREE points to /app.
# This keeps data commits completely isolated from the code branch.
_DATA_GIT_DIR = "/tmp/ns-ai-data-git"

# Serialises concurrent sync calls so multiple simultaneous file uploads
# don't race each other on push.
_sync_lock = threading.Lock()


def _data_run(cmd: list[str]) -> tuple[int, str]:
    """Run a git command using the data-only git directory."""
    env = os.environ.copy()
    env["GIT_DIR"] = _DATA_GIT_DIR
    env["GIT_WORK_TREE"] = WORKDIR
    try:
        result = subprocess.run(
            cmd, env=env, cwd=WORKDIR, capture_output=True, text=True, timeout=120
        )
        return result.returncode, result.stdout + result.stderr
    except Exception as exc:
        return 1, str(exc)


def _setup_data_repo() -> None:
    """Initialise the data git directory and configure the remote."""
    remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
    os.makedirs(_DATA_GIT_DIR, exist_ok=True)
    _data_run(["git", "init"])
    _data_run(["git", "config", "user.email", "agent@ns-ai-agent.app"])
    _data_run(["git", "config", "user.name", "NS-AI-Agent"])
    code, _ = _data_run(["git", "remote", "set-url", "origin", remote_url])
    if code != 0:
        _data_run(["git", "remote", "add", "origin", remote_url])


def restore_from_github() -> bool:
    """Pull data/ from the data branch on startup to restore knowledge base."""
    if not GITHUB_TOKEN:
        logger.debug("GITHUB_TOKEN not set — skipping restore")
        return False

    logger.info("Restoring knowledge base from GitHub [%s]...", DATA_BRANCH)
    _setup_data_repo()

    code, out = _data_run(["git", "fetch", "origin", DATA_BRANCH])
    if code != 0:
        logger.info("No data branch yet (first run) — starting with empty knowledge base")
        return True  # Not an error on first deploy

    code, out = _data_run(["git", "checkout", f"origin/{DATA_BRANCH}", "--", "data/"])
    if code != 0:
        logger.warning("git checkout data/ failed: %s", out)
        return False

    logger.info("Knowledge base restored from GitHub [%s]", DATA_BRANCH)
    return True


def sync_to_github(reason: str = "Knowledge base updated") -> bool:
    """Commit and push data/ to the dedicated data branch.

    Data is pushed to DATA_BRANCH (default: ns-ai-data), NOT the code branch
    Railway watches. This permanently stops auto-syncs from triggering deploys.

    Thread-safe: lock serialises concurrent calls from multi-file uploads.
    """
    if not GITHUB_TOKEN:
        logger.debug("GITHUB_TOKEN not set — skipping sync")
        return False

    with _sync_lock:
        return _do_sync(reason)


def _do_sync(reason: str) -> bool:
    """Sync logic executed while _sync_lock is held."""
    logger.info("Syncing knowledge base to GitHub [%s]: %s", DATA_BRANCH, reason)
    _setup_data_repo()

    _data_run(["git", "fetch", "origin", DATA_BRANCH])

    remote_exists = _data_run(["git", "rev-parse", "--verify", f"refs/remotes/origin/{DATA_BRANCH}"])[0] == 0
    local_exists = _data_run(["git", "rev-parse", "--verify", f"refs/heads/{DATA_BRANCH}"])[0] == 0

    if remote_exists and local_exists:
        # Sync local branch to remote without touching working tree
        _data_run(["git", "reset", "--mixed", f"origin/{DATA_BRANCH}"])
    elif remote_exists:
        # Remote exists but no local branch — create it
        _data_run(["git", "checkout", "-B", DATA_BRANCH, f"origin/{DATA_BRANCH}"])
    else:
        # First ever sync — create an orphan branch (data-only, no code history)
        _data_run(["git", "checkout", "--orphan", DATA_BRANCH])

    _data_run(["git", "add", "data/"])

    code, _ = _data_run(["git", "diff", "--cached", "--quiet"])
    if code == 0:
        logger.debug("Nothing new to commit — knowledge base already in sync")
        return True

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    code, out = _data_run(["git", "commit", "-m", f"Data sync: {reason} ({timestamp})"])
    if code != 0:
        logger.warning("git commit failed: %s", out)
        return False

    code, out = _data_run(["git", "push", "origin", f"HEAD:{DATA_BRANCH}"])
    if code == 0:
        logger.info("Knowledge base synced to GitHub [%s] successfully", DATA_BRANCH)
        return True

    # Push rejected — retry once with fresh fetch
    logger.warning("git push failed, retrying after re-fetch: %s", out)
    _data_run(["git", "fetch", "origin", DATA_BRANCH])
    _data_run(["git", "reset", "--mixed", f"origin/{DATA_BRANCH}"])
    _data_run(["git", "add", "data/"])

    code, _ = _data_run(["git", "diff", "--cached", "--quiet"])
    if code == 0:
        logger.info("Remote already contains our data after retry fetch")
        return True

    code, out = _data_run(["git", "commit", "-m", f"Data sync: {reason} (retry) ({timestamp})"])
    if code != 0:
        logger.warning("git commit on retry failed: %s", out)
        return False

    code, out = _data_run(["git", "push", "origin", f"HEAD:{DATA_BRANCH}"])
    if code != 0:
        logger.warning("git push retry failed: %s", out)
        return False

    logger.info("Knowledge base synced to GitHub [%s] successfully (after retry)", DATA_BRANCH)
    return True
