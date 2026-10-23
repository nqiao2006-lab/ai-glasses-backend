"""Expire only server-generated upload files; never recurse or follow links."""

import logging
import re
import time
from pathlib import Path

logger = logging.getLogger("ai_glasses_backend.cleanup")
UPLOAD_NAME = re.compile(r"[0-9a-f]{32}\.(?:jpg|png)")


def cleanup_expired(directory: Path, retention_seconds: float) -> int:
    cutoff = time.time() - retention_seconds
    removed = 0
    try:
        for path in directory.iterdir():
            if not UPLOAD_NAME.fullmatch(path.name) or path.is_symlink():
                continue
            try:
                if path.is_file() and path.stat().st_mtime <= cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                logger.warning("Unable to remove expired upload; will retry")
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Unable to scan uploads; will retry")
    if removed:
        logger.info("Expired uploads removed: count=%d", removed)
    return removed
