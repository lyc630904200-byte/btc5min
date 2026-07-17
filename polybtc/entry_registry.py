from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Protocol


class MarketEntryRegistry(Protocol):
    def count(self, market_id: str) -> int: ...

    def claim(
        self,
        market_id: str,
        position_id: str,
        entered_at: datetime,
        run_id: str,
        max_entries: int,
    ) -> bool: ...


class InMemoryMarketEntryRegistry:
    def __init__(self, counts: dict[str, int] | None = None):
        self.counts = dict(counts or {})

    def count(self, market_id: str) -> int:
        return self.counts.get(market_id, 0)

    def claim(
        self,
        market_id: str,
        position_id: str,
        entered_at: datetime,
        run_id: str,
        max_entries: int,
    ) -> bool:
        current = self.count(market_id)
        if current >= max_entries:
            return False
        self.counts[market_id] = current + 1
        return True


class SqliteMarketEntryRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10)
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS market_entries (
                    market_id TEXT PRIMARY KEY,
                    entry_count INTEGER NOT NULL,
                    last_position_id TEXT NOT NULL,
                    last_entered_at TEXT NOT NULL,
                    last_run_id TEXT NOT NULL
                )
                """
            )
            self.connection.commit()
        except Exception:
            self.connection.close()
            raise

    def count(self, market_id: str) -> int:
        row = self.connection.execute(
            "SELECT entry_count FROM market_entries WHERE market_id = ?",
            (market_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def claim(
        self,
        market_id: str,
        position_id: str,
        entered_at: datetime,
        run_id: str,
        max_entries: int,
    ) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO market_entries (
                    market_id, entry_count, last_position_id, last_entered_at, last_run_id
                ) VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    entry_count = market_entries.entry_count + 1,
                    last_position_id = excluded.last_position_id,
                    last_entered_at = excluded.last_entered_at,
                    last_run_id = excluded.last_run_id
                WHERE market_entries.entry_count < ?
                """,
                (market_id, position_id, entered_at.isoformat(), run_id, max_entries),
            )
        return cursor.rowcount == 1

    def seed(self, counts: dict[str, int]) -> None:
        with self.connection:
            for market_id, count in counts.items():
                self.connection.execute(
                    """
                    INSERT INTO market_entries (
                        market_id, entry_count, last_position_id, last_entered_at, last_run_id
                    ) VALUES (?, ?, '', '', 'historical_fills')
                    ON CONFLICT(market_id) DO UPDATE SET
                        entry_count = MAX(market_entries.entry_count, excluded.entry_count)
                    """,
                    (market_id, count),
                )

    def close(self) -> None:
        self.connection.close()


def historical_market_entry_counts(data_dir: Path) -> dict[str, int]:
    seen_positions: set[tuple[str, str]] = set()
    for path in data_dir.glob("*/fills.csv"):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle, strict=True)
                required_fields = {"side", "market_id"}
                if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
                    raise RuntimeError(f"historical fills have an invalid header: {path}")
                for row_number, row in enumerate(reader, start=2):
                    if row.get("side") != "BUY" or not row.get("market_id"):
                        continue
                    position_id = row.get("position_id") or row.get("fill_id") or f"{path}:{row_number}"
                    seen_positions.add((row["market_id"], position_id))
        except (OSError, UnicodeError, csv.Error) as exc:
            raise RuntimeError(f"failed to read historical fills: {path}") from exc
    return dict(Counter(market_id for market_id, _ in seen_positions))
