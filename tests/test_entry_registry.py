from datetime import datetime, timezone

from polybtc.entry_registry import SqliteMarketEntryRegistry, historical_market_entry_counts


def test_sqlite_registry_enforces_limit_across_connections(tmp_path) -> None:
    path = tmp_path / "market-entry-ledger.sqlite3"
    first = SqliteMarketEntryRegistry(path)
    second = SqliteMarketEntryRegistry(path)
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    try:
        assert first.claim("m1", "p1", now, "run1", 1) is True
        assert second.claim("m1", "p2", now, "run2", 1) is False
        assert second.count("m1") == 1
        assert second.claim("m2", "p3", now, "run2", 1) is True
    finally:
        first.close()
        second.close()

    reopened = SqliteMarketEntryRegistry(path)
    try:
        assert reopened.count("m1") == 1
        assert reopened.claim("m1", "p4", now, "run3", 1) is False
    finally:
        reopened.close()


def test_sqlite_registry_supports_configured_count_and_historical_seed(tmp_path) -> None:
    registry = SqliteMarketEntryRegistry(tmp_path / "market-entry-ledger.sqlite3")
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    try:
        registry.seed({"seeded": 1})
        assert registry.claim("seeded", "p2", now, "run", 1) is False
        assert registry.claim("m1", "p1", now, "run", 2) is True
        assert registry.claim("m1", "p2", now, "run", 2) is True
        assert registry.claim("m1", "p3", now, "run", 2) is False
        assert registry.count("m1") == 2
    finally:
        registry.close()


def test_historical_market_entry_counts_only_unique_buys(tmp_path) -> None:
    run = tmp_path / "run1"
    run.mkdir()
    (run / "fills.csv").write_text(
        "fill_id,position_id,market_id,side\n"
        "f1,p1,m1,BUY\n"
        "f2,p1,m1,BUY\n"
        "f3,p1,m1,SELL\n"
        "f4,p2,m1,BUY\n"
        "f5,p3,m2,SELL\n",
        encoding="utf-8",
    )

    assert historical_market_entry_counts(tmp_path) == {"m1": 2}


def test_historical_market_entry_counts_rejects_invalid_header(tmp_path) -> None:
    run = tmp_path / "run1"
    run.mkdir()
    (run / "fills.csv").write_text("fill_id,position_id\nf1,p1\n", encoding="utf-8")

    try:
        historical_market_entry_counts(tmp_path)
    except RuntimeError as exc:
        assert "invalid header" in str(exc)
    else:
        raise AssertionError("invalid historical fill header must fail closed")
