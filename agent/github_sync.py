"""Auto-sync knowledge base and data to GitHub after training."""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "brbernst94/NS-AI-Agent")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "claude/funny-ride-Oq16Q")
WORKDIR = "/app"


def _run(cmd: list[str], cwd: str = WORKDIR) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
        return result.returncode, result.stdout + result.stderr
    except Exception as exc:
        return 1, str(exc)


def _setup_repo() -> bool:
    """Initialize git repo and configure remote. Does NOT touch working tree files."""
    remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"

    _run(["git", "config", "user.email", "agent@ns-ai-agent.app"])
    _run(["git", "config", "user.name", "NS-AI-Agent"])

    if not os.path.exists(os.path.join(WORKDIR, ".git")):
        logger.info("No .git found — initializing repo")
        _run(["git", "init"])
        _run(["git", "remote", "add", "origin", remote_url])
    else:
        _run(["git", "remote", "set-url", "origin", remote_url])

    return True


def restore_from_github() -> bool:
    """Pull data/ folder from GitHub on startup to restore previous knowledge."""
    if not GITHUB_TOKEN:
        logger.debug("GITHUB_TOKEN not set — skipping restore")
        return False

    logger.info("Restoring knowledge base from GitHub...")
    _setup_repo()

    code, out = _run(["git", "fetch", "origin", GITHUB_BRANCH])
    if code != 0:
        logger.warning("git fetch failed: %s", out)
        return False

    # Move branch pointer to match remote (--soft keeps working tree intact)
    _run(["git", "reset", "--soft", f"origin/{GITHUB_BRANCH}"])

    # Pull the actual data/ files from remote into working tree
    code, out = _run(["git", "checkout", f"origin/{GITHUB_BRANCH}", "--", "data/"])
    if code != 0:
        logger.warning("git checkout data/ failed: %s", out)
        return False

    logger.info("Knowledge base restored from GitHub")
    return True


def sync_to_github(reason: str = "Knowledge base updated") -> bool:
    """Commit and push the data/ folder to GitHub."""
    if not GITHUB_TOKEN:
        logger.debug("GITHUB_TOKEN not set — skipping sync")
        return False

    logger.info("Syncing knowledge base to GitHub: %s", reason)
    _setup_repo()

    # Fetch remote and move branch pointer WITHOUT touching the working tree.
    # --soft ensures ChromaDB files written during ingest are preserved while
    # the commit goes on top of existing remote history (no non-fast-forward).
    code, out = _run(["git", "fetch", "origin", GITHUB_BRANCH])
    if code == 0:
        _run(["git", "reset", "--soft", f"origin/{GITHUB_BRANCH}"])
    else:
        logger.warning("git fetch failed before sync (will try anyway): %s", out)

    _run(["git", "add", "data/"])

    # Nothing to commit?
    code, _ = _run(["git", "diff", "--cached", "--quiet"])
    if code == 0:
        logger.debug("Nothing new to commit")
        return True

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    code, out = _run(["git", "commit", "-m", f"Auto-sync: {reason} ({timestamp})"])
    if code != 0:
        logger.warning("git commit failed: %s", out)
        return False

    code, out = _run(["git", "push", "origin", f"HEAD:{GITHUB_BRANCH}"])
    if code != 0:
        logger.warning("git push failed: %s", out)
        return False

    logger.info("Knowledge base synced to GitHub successfully")
    return True
