from __future__ import annotations

import json
from pathlib import Path

from .config import AppConfig
from .engine import PaperEngine
from .models import Direction, MarketState, OrderBookSnapshot, PriceTick


def replay_events(input_path: Path, config: AppConfig) -> PaperEngine:
    engine = PaperEngine(config)
    with input_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            event = json.loads(line)
            event_type = event.get("type")
            payload = event.get("payload") or {}
            if event_type == "market":
                engine.set_market(MarketState.model_validate(payload))
            elif event_type == "tick":
                engine.set_tick(PriceTick.model_validate(payload))
            elif event_type == "book":
                direction = Direction(payload.pop("direction"))
                engine.set_book(direction, OrderBookSnapshot.model_validate(payload))
    return engine
