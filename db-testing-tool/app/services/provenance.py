"""Simple provenance writer for generator runs.

Writes a small JSON metadata file under DATA_DIR/local_kb/generator_runs/
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime
import logging

from app.config import DATA_DIR

logger = logging.getLogger(__name__)

RUNS_DIR = Path(DATA_DIR) / "local_kb" / "generator_runs"


def write_generator_run(metadata: dict) -> Path:
    """Write generator run metadata to a timestamped JSON file and return its path.

    The caller should ensure metadata is JSON-serializable. The function will
    create parent directories as needed.
    """
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = metadata.get("timestamp") or datetime.utcnow().isoformat()
    # sanitize filename-friendly timestamp
    safe_ts = ts.replace(":", "-").replace(".", "-")
    table = (metadata.get("target_table") or "unknown").upper()
    fname = f"{table}_{safe_ts}.json"
    fpath = RUNS_DIR / fname
    try:
        # Dump with indentation for readability
        fpath.write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
        logger.info("Wrote generator run metadata: %s", fpath)
    except Exception:
        logger.exception("Failed to write generator run metadata to %s", fpath)
        raise
    return fpath
