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

    # Move branch pointer to remote without touching files (--soft).
    _run(["git", "reset", "--soft", f"origin/{GITHUB_BRANCH}"])

    # Now pull the actual data/ files from remote into working tree.
    code, out = _run(["git", "checkout", f"origin/{GITHUB_BRANCH}", "--", "data/"])
    if code != 0:
        logger.warning("git checkout data/ failed: %s", out)
        return False

    logger.info("Knowledge base restored from GitHub")
    return True


def sync_to_github(reason: str = "Knowledge base updated") -> bool:
    """Commit and push the data/ folder to GitHub.

    Thread-safe: a lock serialises concurrent calls so multi-file uploads
    don't produce simultaneous pushes that reject each other. The later
    callers will typically exit as no-ops once the first sync commits
    all pending ChromaDB changes.
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

    # Bring local branch pointer up to the remote tip WITHOUT touching the
    # working tree (--soft).  This ensures our commit goes on top of existing
    # history so the push is always a fast-forward.
    code, out = _run(["git", "fetch", "origin", GITHUB_BRANCH])
    if code == 0:
        _run(["git", "reset", "--soft", f"origin/{GITHUB_BRANCH}"])
    else:
        logger.warning("git fetch failed before sync (will try push anyway): %s", out)

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

    # Push was rejected — another process pushed between our fetch and now.
    # Re-fetch, replay our staged data on top of the new tip, and retry once.
    logger.warning("git push failed (remote moved), retrying: %s", out)
    code2, out2 = _run(["git", "fetch", "origin", GITHUB_BRANCH])
    if code2 != 0:
        logger.warning("git fetch on retry failed: %s", out2)
        return False

    _run(["git", "reset", "--soft", f"origin/{GITHUB_BRANCH}"])
    _run(["git", "add", "data/"])

    # Re-check — the remote may have already included our data.
    code3, _ = _run(["git", "diff", "--cached", "--quiet"])
    if code3 == 0:
        logger.info("Remote already contains our data — no retry commit needed")
        return True

    code4, out4 = _run(["git", "commit", "-m", f"Auto-sync: {reason} (retry) ({timestamp})"])
    if code4 != 0:
        logger.warning("git commit on retry failed: %s", out4)
        return False

    code5, out5 = _run(["git", "push", "origin", f"HEAD:{GITHUB_BRANCH}"])
    if code5 != 0:
        logger.warning("git push retry also failed: %s", out5)
        return False

    logger.info("Knowledge base synced to GitHub successfully (after retry)")
    return True
