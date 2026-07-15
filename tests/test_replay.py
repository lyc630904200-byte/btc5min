import json
from datetime import datetime, timedelta, timezone

from polybtc.config import AppConfig
from polybtc.models import BookLevel, Direction, MarketState, OrderBookSnapshot, PriceTick
from polybtc.replay import replay_events


def event(event_type: str, payload: object) -> str:
    return json.dumps({"type": event_type, "payload": payload}, default=str)


def test_replay_recomputes_entry_and_exit(tmp_path) -> None:
    now = datetime(2026, 7, 11, 1, 0, tzinfo=timezone.utc)
    market = MarketState(
        condition_id="m1",
        slug="bitcoin-up-or-down",
        question="Bitcoin Up or Down above 118000",
        threshold_price=118000,
        end_time=now + timedelta(seconds=120),
        up_token_id="up",
        down_token_id="down",
    )
    tick = PriceTick(price=118070, received_at=now)
    up_book = OrderBookSnapshot(
        token_id="up",
        timestamp=now,
        received_at=now,
        bids=[BookLevel(price=0.72, size=100)],
        asks=[BookLevel(price=0.60, size=100)],
    )
    down_book = OrderBookSnapshot(
        token_id="down",
        timestamp=now,
        received_at=now,
        bids=[BookLevel(price=0.30, size=100)],
        asks=[BookLevel(price=0.40, size=100)],
    )
    tick2 = PriceTick(price=118080, received_at=now + timedelta(seconds=1))
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            [
                event("market", market.model_dump(mode="json")),
                event("tick", tick.model_dump(mode="json")),
                event("book", {"direction": Direction.UP.value, **up_book.model_dump(mode="json")}),
                event("book", {"direction": Direction.DOWN.value, **down_book.model_dump(mode="json")}),
                event("tick", tick2.model_dump(mode="json")),
            ]
        ),
        encoding="utf-8",
    )

    config = AppConfig()
    engine = replay_events(path, config)
    engine2 = replay_events(path, config)

    assert engine.summary()["signals"] == 1
    assert engine.summary()["closed_positions"] == 1
    assert engine.summary()["realized_pnl"] == engine2.summary()["realized_pnl"]
