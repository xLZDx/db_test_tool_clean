"""In-memory operation tracking for long-running schema tasks.

Terminal states (completed/failed/stopped) are also persisted to disk so they
survive server restarts. Call restore_persisted_operations() on startup.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional


_OPERATIONS: Dict[str, Dict[str, Any]] = {}


# ── Disk persistence helpers ─────────────────────────────────────────────────

def _history_path() -> Path:
    """Return path to the JSONL history file, creating the directory if needed."""
    try:
        from app.config import BASE_DIR  # lazy import to avoid circular deps
        p = BASE_DIR / "data" / "local_kb"
    except Exception:
        p = Path(__file__).resolve().parents[2] / "data" / "local_kb"
    p.mkdir(parents=True, exist_ok=True)
    return p / "operation_history.jsonl"


def _serialize_state(state: Dict[str, Any]) -> str:
    out: Dict[str, Any] = {}
    for k, v in state.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return json.dumps(out, ensure_ascii=True)


def _persist_terminal_state(state: Dict[str, Any]) -> None:
    """Append a terminal-state snapshot to the history file."""
    try:
        line = _serialize_state(state) + "\n"
        with _history_path().open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def restore_persisted_operations(max_age_hours: int = 48) -> int:
    """Load recent terminal-state operations from disk into _OPERATIONS.

    Returns the number of operations restored.
    Called once from main.py startup.
    """
    path = _history_path()
    if not path.exists():
        return 0
    cutoff = _now() - timedelta(hours=max_age_hours)
    loaded = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines[-500:]:  # cap memory: last 500 entries
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except Exception:
                continue
            # Deserialise datetime strings
            for key in ("started_at", "updated_at", "finished_at"):
                val = raw.get(key)
                if isinstance(val, str) and val:
                    try:
                        raw[key] = datetime.fromisoformat(val)
                    except Exception:
                        raw[key] = None
            # Skip entries that are too old
            updated = raw.get("updated_at")
            if isinstance(updated, datetime) and updated < cutoff:
                continue
            op_id = raw.get("id")
            if op_id and op_id not in _OPERATIONS:
                _OPERATIONS[op_id] = raw
                loaded += 1
    except Exception:
        pass
    return loaded


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_seconds(state: Dict[str, Any]) -> float:
    started = state.get("started_at")
    if not isinstance(started, datetime):
        return 0.0
    return max(0.0, (_now() - started).total_seconds())


def _decorate_state(state: Dict[str, Any]) -> Dict[str, Any]:
    current = int(state.get("current", 0) or 0)
    total = int(state.get("total", 0) or 0)
    elapsed = _elapsed_seconds(state)
    percent = 0.0
    remaining = None
    eta = None
    if total > 0:
        percent = max(0.0, min(100.0, (current / total) * 100.0))
        remaining = max(0, total - current)
        if current > 0 and remaining > 0 and elapsed > 0:
            rate = current / elapsed
            if rate > 0:
                eta = remaining / rate

    out = dict(state)
    out["percent_complete"] = round(percent, 2)
    out["remaining_units"] = remaining
    out["elapsed_seconds"] = round(elapsed, 1)
    out["eta_seconds"] = round(eta, 1) if eta is not None else None
    return out


def _prune_old_operations() -> None:
    cutoff = _now() - timedelta(minutes=30)
    to_delete = []
    for op_id, state in _OPERATIONS.items():
        updated = state.get("updated_at")
        if isinstance(updated, datetime) and updated < cutoff:
            to_delete.append(op_id)
    for op_id in to_delete:
        _OPERATIONS.pop(op_id, None)


def register_operation(operation_id: str | None, label: str = "operation") -> None:
    if not operation_id:
        return
    _prune_old_operations()
    _OPERATIONS[operation_id] = {
        "id": operation_id,
        "label": label,
        "status": "running",
        "current": 0,
        "total": 0,
        "notifications": [],
        "errors": [],
        "stop_requested": False,
        "started_at": _now(),
        "updated_at": _now(),
        "finished_at": None,
    }


def set_status(operation_id: str | None, status: str, message: str | None = None) -> None:
    if not operation_id or operation_id not in _OPERATIONS:
        return
    state = _OPERATIONS[operation_id]
    state["status"] = status or state.get("status", "running")
    if message:
        state["notifications"].append(message)
        state["notifications"] = state["notifications"][-30:]
    state["updated_at"] = _now()


def mark_queued(operation_id: str | None, message: str | None = None) -> None:
    set_status(operation_id, "queued", message)


def mark_running(operation_id: str | None, message: str | None = None) -> None:
    set_status(operation_id, "running", message)


def set_total(operation_id: str | None, total: int) -> None:
    if not operation_id or operation_id not in _OPERATIONS:
        return
    _OPERATIONS[operation_id]["total"] = max(0, int(total or 0))
    _OPERATIONS[operation_id]["updated_at"] = _now()


def set_progress(operation_id: str | None, current: int, total: int | None = None, message: str | None = None) -> None:
    if not operation_id or operation_id not in _OPERATIONS:
        return
    state = _OPERATIONS[operation_id]
    state["current"] = max(0, int(current or 0))
    if total is not None:
        state["total"] = max(0, int(total or 0))
    if message:
        state["notifications"].append(message)
        state["notifications"] = state["notifications"][-30:]
    state["updated_at"] = _now()


def advance_progress(operation_id: str | None, delta: int = 1, message: str | None = None) -> None:
    if not operation_id or operation_id not in _OPERATIONS:
        return
    state = _OPERATIONS[operation_id]
    state["current"] = max(0, int(state.get("current", 0)) + max(0, int(delta or 0)))
    if message:
        state["notifications"].append(message)
        state["notifications"] = state["notifications"][-30:]
    state["updated_at"] = _now()


def add_notification(operation_id: str | None, message: str) -> None:
    if not operation_id or operation_id not in _OPERATIONS or not message:
        return
    state = _OPERATIONS[operation_id]
    state["notifications"].append(message)
    state["notifications"] = state["notifications"][-30:]
    state["updated_at"] = _now()


def add_error(operation_id: str | None, message: str) -> None:
    if not operation_id or operation_id not in _OPERATIONS or not message:
        return
    state = _OPERATIONS[operation_id]
    state["errors"].append(message)
    state["errors"] = state["errors"][-20:]
    state["updated_at"] = _now()


def mark_completed(operation_id: str | None, message: str | None = None) -> None:
    if not operation_id or operation_id not in _OPERATIONS:
        return
    state = _OPERATIONS[operation_id]
    state["status"] = "completed"
    if message:
        state["notifications"].append(message)
        state["notifications"] = state["notifications"][-30:]
    if state.get("total", 0) > 0:
        state["current"] = max(int(state.get("current", 0)), int(state.get("total", 0)))
    state["finished_at"] = _now()
    state["updated_at"] = _now()
    _persist_terminal_state(state)


def mark_stopped(operation_id: str | None, message: str | None = None) -> None:
    if not operation_id or operation_id not in _OPERATIONS:
        return
    state = _OPERATIONS[operation_id]
    state["status"] = "stopped"
    if message:
        state["notifications"].append(message)
        state["notifications"] = state["notifications"][-30:]
    state["finished_at"] = _now()
    state["updated_at"] = _now()
    _persist_terminal_state(state)


def mark_failed(operation_id: str | None, message: str) -> None:
    if not operation_id or operation_id not in _OPERATIONS:
        return
    state = _OPERATIONS[operation_id]
    state["status"] = "failed"
    if message:
        state["errors"].append(message)
        state["errors"] = state["errors"][-20:]
    state["finished_at"] = _now()
    state["updated_at"] = _now()
    _persist_terminal_state(state)


def request_stop(operation_id: str) -> None:
    if operation_id not in _OPERATIONS:
        _OPERATIONS[operation_id] = {
            "id": operation_id,
            "label": "operation",
            "status": "stopped",
            "current": 0,
            "total": 0,
            "notifications": ["Stop requested"],
            "errors": [],
            "stop_requested": True,
            "started_at": _now(),
            "updated_at": _now(),
            "finished_at": _now(),
        }
        return
    state = _OPERATIONS[operation_id]
    state["stop_requested"] = True
    state["notifications"].append("Stop requested")
    state["notifications"] = state["notifications"][-30:]
    state["updated_at"] = _now()


def should_stop(operation_id: str | None) -> bool:
    if not operation_id:
        return False
    return bool(_OPERATIONS.get(operation_id, {}).get("stop_requested", False))


def ensure_not_stopped(operation_id: str | None) -> None:
    if should_stop(operation_id):
        mark_stopped(operation_id, "Operation was stopped by user")
        raise RuntimeError("Operation was stopped by user")


def get_operation(operation_id: str) -> Dict[str, Any] | None:
    state = _OPERATIONS.get(operation_id)
    if not state:
        return None
    return _decorate_state(state)


def sweep_stale_operations(
    *,
    running_stale_minutes: int = 20,
    queued_stale_minutes: int = 30,
    finished_retain_minutes: int = 120,
) -> Dict[str, int]:
    """Stop stale operation sessions and prune old finished sessions.

    Returns counters for watchdog diagnostics.
    """
    now = _now()
    running_cutoff = now - timedelta(minutes=max(1, int(running_stale_minutes or 20)))
    queued_cutoff = now - timedelta(minutes=max(1, int(queued_stale_minutes or 30)))
    finished_cutoff = now - timedelta(minutes=max(1, int(finished_retain_minutes or 120)))

    stopped = 0
    pruned = 0

    for op_id, state in list(_OPERATIONS.items()):
        status = str(state.get("status") or "").lower()
        updated = state.get("updated_at")
        if not isinstance(updated, datetime):
            updated = state.get("started_at")

        if status in {"running", "queued"} and isinstance(updated, datetime):
            cutoff = running_cutoff if status == "running" else queued_cutoff
            if updated < cutoff:
                state["stop_requested"] = True
                state["status"] = "stopped"
                state["finished_at"] = now
                state["updated_at"] = now
                state.setdefault("notifications", []).append(
                    "Watchdog terminated stale session"
                )
                state["notifications"] = state["notifications"][-30:]
                stopped += 1

        terminal = str(state.get("status") or "").lower() in {"completed", "failed", "stopped"}
        finished = state.get("finished_at") or state.get("updated_at")
        if terminal and isinstance(finished, datetime) and finished < finished_cutoff:
            _OPERATIONS.pop(op_id, None)
            pruned += 1

    return {
        "stale_stopped": stopped,
        "pruned": pruned,
        "active": len(_OPERATIONS),
    }


def clear_operation(operation_id: str | None) -> None:
    if operation_id:
        _OPERATIONS.pop(operation_id, None)
