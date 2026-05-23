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


def _run(cmd: list[str], cwd: str = "/app") -> tuple[int, str]:
    """Run a shell command and return (returncode, output)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode, result.stdout + result.stderr
    except Exception as exc:
        return 1, str(exc)


def restore_from_github() -> bool:
    """
    Pull the latest data/ folder from GitHub on startup.

    This restores the knowledge base and crawler state that was
    saved from a previous session, so nothing is lost on redeploy.
    Silently skips if GITHUB_TOKEN is not set.
    """
    if not GITHUB_TOKEN:
        logger.debug("GITHUB_TOKEN not set — skipping GitHub restore")
        return False

    logger.info("Restoring knowledge base from GitHub...")

    remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"

    _run(["git", "config", "user.email", "agent@ns-ai-agent.app"])
    _run(["git", "config", "user.name", "NS-AI-Agent"])
    _run(["git", "remote", "set-url", "origin", remote_url])

    # Fetch latest from remote
    code, out = _run(["git", "fetch", "origin", GITHUB_BRANCH])
    if code != 0:
        logger.warning("git fetch failed: %s", out)
        return False

    # Checkout just the data/ folder from remote
    code, out = _run(["git", "checkout", f"origin/{GITHUB_BRANCH}", "--", "data/"])
    if code != 0:
        logger.warning("git checkout data/ failed: %s", out)
        return False

    logger.info("Knowledge base restored from GitHub successfully")
    return True


def sync_to_github(reason: str = "Knowledge base updated") -> bool:
    """
    Commit and push the data/ folder to GitHub.

    Returns True if sync succeeded, False otherwise.
    Silently skips if GITHUB_TOKEN is not set.
    """
    if not GITHUB_TOKEN:
        logger.debug("GITHUB_TOKEN not set — skipping GitHub sync")
        return False

    logger.info("Syncing knowledge base to GitHub: %s", reason)

    remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"

    # Configure git identity
    _run(["git", "config", "user.email", "agent@ns-ai-agent.app"])
    _run(["git", "config", "user.name", "NS-AI-Agent"])

    # Set remote with auth token
    _run(["git", "remote", "set-url", "origin", remote_url])

    # Stage the data directory
    code, out = _run(["git", "add", "data/"])
    if code != 0:
        logger.warning("git add failed: %s", out)

    # Check if there's anything to commit
    code, out = _run(["git", "diff", "--cached", "--quiet"])
    if code == 0:
        logger.debug("Nothing new to commit to GitHub")
        return True

    # Commit
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    commit_msg = f"Auto-sync: {reason} ({timestamp})"
    code, out = _run(["git", "commit", "-m", commit_msg])
    if code != 0:
        logger.warning("git commit failed: %s", out)
        return False

    # Push
    code, out = _run(["git", "push", "origin", GITHUB_BRANCH])
    if code != 0:
        logger.warning("git push failed: %s", out)
        return False

    logger.info("Knowledge base synced to GitHub successfully")
    return True
