from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Literal


EventType = Literal["move", "click", "scroll", "key_press", "key_release"]
ButtonName = Literal["left", "right", "middle"]

VALID_EVENT_TYPES = frozenset({"move", "click", "scroll", "key_press", "key_release"})
VALID_BUTTON_NAMES = frozenset({"left", "right", "middle"})


@dataclass(frozen=True)
class Event:
    t: float
    type: EventType
    x: int = 0
    y: int = 0
    button: ButtonName | None = None
    pressed: bool | None = None
    dx: int = 0
    dy: int = 0
    key: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, 0) or k in ("t", "x", "y", "type")}


@dataclass
class Macro:
    version: int = 1
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    events: list[Event] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "events": [e.to_dict() for e in self.events],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Macro":
        if not isinstance(data, dict):
            raise ValueError("Macro JSON must be an object")
        version = data.get("version")
        if version != 1:
            raise ValueError(f"Unsupported macro version: {version!r}")
        raw_events = data.get("events", [])
        if not isinstance(raw_events, list):
            raise ValueError("'events' must be a list")
        events: list[Event] = []
        for i, e in enumerate(raw_events):
            if not isinstance(e, dict):
                raise ValueError(f"Event #{i} must be an object")
            ev_type = e.get("type")
            if ev_type not in VALID_EVENT_TYPES:
                raise ValueError(
                    f"Event #{i}: unknown type {ev_type!r}, "
                    f"must be one of {sorted(VALID_EVENT_TYPES)}"
                )
            button = e.get("button")
            if button is not None and button not in VALID_BUTTON_NAMES:
                raise ValueError(
                    f"Event #{i}: unknown button {button!r}, "
                    f"must be one of {sorted(VALID_BUTTON_NAMES)}"
                )
            try:
                events.append(
                    Event(
                        t=float(e.get("t", 0.0)),
                        type=ev_type,
                        x=int(e.get("x", 0)),
                        y=int(e.get("y", 0)),
                        button=button,
                        pressed=e.get("pressed"),
                        dx=int(e.get("dx", 0)),
                        dy=int(e.get("dy", 0)),
                        key=e.get("key"),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Event #{i}: invalid field — {exc}") from exc
        return cls(
            version=version,
            created_at=data.get("created_at", ""),
            events=events,
        )
