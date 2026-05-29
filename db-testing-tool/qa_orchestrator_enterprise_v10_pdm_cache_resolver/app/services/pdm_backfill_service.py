"""Backfill request hook for missing PDM schemas/tables/attributes.

The real app can replace `request_backfill` with a call into the existing schema crawler,
connector registry, or background job queue. This default implementation writes an
append-only JSONL event so the tool can trigger or audit the backfill process.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


class PDMBackfillService:
    def __init__(self, local_kb_dir: str = "data/local_kb", operation_history_file: str = "operation_history.jsonl"):
        self.local_kb_dir = Path(local_kb_dir)
        self.local_kb_dir.mkdir(parents=True, exist_ok=True)
        self.operation_history_path = self.local_kb_dir / operation_history_file

    def request_backfill(self, reason: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        event = {
            "event_type": "PDM_BACKFILL_REQUESTED",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "payload": payload,
            "status": "QUEUED"
        }
        with self.operation_history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event
