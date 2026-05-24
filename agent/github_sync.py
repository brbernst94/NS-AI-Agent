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
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "claude/funny-ride-Oq16Q")
WORKDIR = "/app"

# Serialises concurrent sync calls so multiple simultaneous file uploads
# don't race each other on push. The second caller in the queue will almost
# always find "nothing new to commit" because the first already pushed
# everything that was in ChromaDB at that point.
_sync_lock = threading.Lock()


def _run(cmd: list[str], cwd: str = WORKDIR) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
        return result.returncode, result.stdout + result.stderr
    except Exception as exc:
        return 1, str(exc)


def _setup_repo() -> None:
    """Initialise git repo and set remote URL. Never touches working-tree files."""
    remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
    _run(["git", "config", "user.email", "agent@ns-ai-agent.app"])
    _run(["git", "config", "user.name", "NS-AI-Agent"])

    if not os.path.exists(os.path.join(WORKDIR, ".git")):
        logger.info("No .git found — initialising repo")
        _run(["git", "init"])
        _run(["git", "remote", "add", "origin", remote_url])
    else:
        _run(["git", "remote", "set-url", "origin", remote_url])


def _local_branch_exists() -> bool:
    code, _ = _run(["git", "rev-parse", "--verify", f"refs/heads/{GITHUB_BRANCH}"])
    return code == 0


def restore_from_github() -> bool:
    """Pull data/ from GitHub on startup to restore the previous knowledge base."""
    if not GITHUB_TOKEN:
        logger.debug("GITHUB_TOKEN not set — skipping restore")
        return False

    logger.info("Restoring knowledge base from GitHub...")
    _setup_repo()

    code, out = _run(["git", "fetch", "origin", GITHUB_BRANCH])
    if code != 0:
        logger.warning("git fetch failed during restore: %s", out)
        return False

    # Create/reset local branch from remote. At startup data/ is empty
    # (Docker image has no ChromaDB files), so the checkout is safe.
    _run(["git", "checkout", "-B", GITHUB_BRANCH, f"origin/{GITHUB_BRANCH}"])

    logger.info("Knowledge base restored from GitHub")
    return True


def sync_to_github(reason: str = "Knowledge base updated") -> bool:
    """Commit and push the data/ folder to GitHub.

    Thread-safe: a lock serialises concurrent calls so multi-file uploads
    don't produce simultaneous pushes that reject each other. Later callers
    typically exit as no-ops once the first sync commits all pending changes.
    """
    if not GITHUB_TOKEN:
        logger.debug("GITHUB_TOKEN not set — skipping sync")
        return False

    with _sync_lock:
        return _do_sync(reason)


def _do_sync(reason: str) -> bool:
    """Sync logic executed while _sync_lock is held."""
    logger.info("Syncing knowledge base to GitHub: %s", reason)
    _setup_repo()

    code, out = _run(["git", "fetch", "origin", GITHUB_BRANCH])
    if code != 0:
        logger.warning("git fetch failed before sync (will try anyway): %s", out)

    if _local_branch_exists():
        # Branch exists — update HEAD + index to remote tip WITHOUT touching
        # the working tree. ChromaDB files written since the last sync survive;
        # git add data/ below stages the diff against the previous sync.
        _run(["git", "reset", "--mixed", f"origin/{GITHUB_BRANCH}"])
    else:
        # Fresh git init — create local branch from remote. ChromaDB files in
        # data/ are UNTRACKED at this point (not in origin/BRANCH), so checkout
        # leaves them completely untouched.
        _run(["git", "checkout", "-B", GITHUB_BRANCH, f"origin/{GITHUB_BRANCH}"])

    _run(["git", "add", "data/"])

    code, _ = _run(["git", "diff", "--cached", "--quiet"])
    if code == 0:
        logger.debug("Nothing new to commit — knowledge base already in sync")
        return True

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    code, out = _run(["git", "commit", "-m", f"Auto-sync: {reason} ({timestamp})"])
    if code != 0:
        logger.warning("git commit failed: %s", out)
        return False

    code, out = _run(["git", "push", "origin", f"HEAD:{GITHUB_BRANCH}"])
    if code == 0:
        logger.info("Knowledge base synced to GitHub successfully")
        return True

    # Push was rejected — remote moved between our fetch and now. Re-fetch,
    # replay our staged data on top of the new tip, and try once more.
    logger.warning("git push failed (remote moved), retrying: %s", out)
    if _run(["git", "fetch", "origin", GITHUB_BRANCH])[0] != 0:
        logger.warning("git fetch on retry failed")
        return False

    _run(["git", "reset", "--mixed", f"origin/{GITHUB_BRANCH}"])
    _run(["git", "add", "data/"])

    code, _ = _run(["git", "diff", "--cached", "--quiet"])
    if code == 0:
        logger.info("Remote already contains our data after retry fetch")
        return True

    code, out = _run(["git", "commit", "-m", f"Auto-sync: {reason} (retry) ({timestamp})"])
    if code != 0:
        logger.warning("git commit on retry failed: %s", out)
        return False

    code, out = _run(["git", "push", "origin", f"HEAD:{GITHUB_BRANCH}"])
    if code != 0:
        logger.warning("git push retry also failed: %s", out)
        return False

    logger.info("Knowledge base synced to GitHub successfully (after retry)")
    return True
