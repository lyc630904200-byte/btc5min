from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


class SqliteControlEventStore:
    """Shared JSON control/event operations for independent ledgers."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        controls_table: str,
        events_table: str,
        control_value_column: str = "value",
    ):
        self.connection = connection
        self.controls_table = controls_table
        self.events_table = events_table
        self.control_value_column = control_value_column

    def set_control(self, key: str, value: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        column = self.control_value_column
        self.connection.execute(
            f"""
            INSERT INTO {self.controls_table} (key, {column}, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                {column}=excluded.{column}, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False), now),
        )
        self.connection.commit()

    def get_control(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            f"SELECT {self.control_value_column} FROM {self.controls_table} WHERE key=?",
            (key,),
        ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[self.control_value_column])
        except (TypeError, json.JSONDecodeError):
            return default

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.connection.execute(
            f"""
            INSERT INTO {self.events_table} (event_type, created_at, payload_json)
            VALUES (?, ?, ?)
            """,
            (
                event_type,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
            ),
        )
        self.connection.commit()
