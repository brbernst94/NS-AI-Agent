"""GitHub sync stubs — data is now persisted in PostgreSQL on Railway.

These functions are retained as no-ops so any lingering import sites don't break,
but they perform no work since the database handles persistence automatically.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def restore_from_github() -> bool:
    logger.debug("restore_from_github: no-op (data stored in PostgreSQL)")
    return True


def sync_to_github(reason: str = "") -> bool:
    logger.debug("sync_to_github: no-op (data stored in PostgreSQL)")
    return True
