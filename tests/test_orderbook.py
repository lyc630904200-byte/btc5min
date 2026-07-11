from datetime import datetime, timezone

from polybtc.models import BookLevel, OrderBookSnapshot
from polybtc.orderbook import simulate_buy, simulate_sell


def test_simulate_buy_walks_ask_book() -> None:
    book = OrderBookSnapshot(
        token_id="yes",
        timestamp=datetime.now(timezone.utc),
        asks=[BookLevel(price=0.50, size=10), BookLevel(price=0.60, size=10)],
    )

    result = simulate_buy(book, 8)

    assert result.complete is True
    assert result.quantity == 15
    assert round(result.avg_price, 6) == round(8 / 15, 6)
    assert round(result.slippage, 6) == round((8 / 15) - 0.50, 6)


def test_simulate_sell_reports_partial_depth() -> None:
    book = OrderBookSnapshot(
        token_id="yes",
        timestamp=datetime.now(timezone.utc),
        bids=[BookLevel(price=0.70, size=3), BookLevel(price=0.65, size=2)],
    )

    result = simulate_sell(book, 8)

    assert result.complete is False
    assert result.quantity == 5
    assert result.quote == 3 * 0.70 + 2 * 0.65
    assert result.best_price == 0.70
