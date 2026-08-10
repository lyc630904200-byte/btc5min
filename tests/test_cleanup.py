from datetime import datetime, timedelta, timezone
import asyncio
import os

from polybtc.config import AppConfig
from polybtc.journal import RunJournal
from polybtc.runner import (
    btc_v8_data_cleanup_loop,
    cleanup_expired_runs,
    data_cleanup_loop,
)


def test_cleanup_removes_only_expired_run_directories(tmp_path) -> None:
    data_dir = tmp_path / "data"
    active = data_dir / "active"
    expired = data_dir / "expired"
    recent = data_dir / "recent"
    unrelated = data_dir / "notes"
    for run in (active, expired, recent):
        run.mkdir(parents=True)
        (run / "events.jsonl").write_text("", encoding="utf-8")
    unrelated.mkdir(parents=True)
    (unrelated / "readme.txt").write_text("keep", encoding="utf-8")
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    os.utime(expired, (now.timestamp() - 25 * 3600, now.timestamp() - 25 * 3600))
    os.utime(recent, (now.timestamp() - 23 * 3600, now.timestamp() - 23 * 3600))

    removed = cleanup_expired_runs(data_dir, active, timedelta(hours=24), now=now)

    assert removed == [expired]
    assert not expired.exists()
    assert active.exists()
    assert recent.exists()
    assert unrelated.exists()


def test_cleanup_loop_returns_without_removing_runs_when_disabled(tmp_path) -> None:
    data_dir = tmp_path / "data"
    active = data_dir / "active"
    expired = data_dir / "expired"
    active.mkdir(parents=True)
    expired.mkdir()
    (expired / "events.jsonl").write_text("", encoding="utf-8")
    expired_at = datetime.now(timezone.utc) - timedelta(hours=25)
    os.utime(expired, (expired_at.timestamp(), expired_at.timestamp()))
    journal = RunJournal(active)
    config = AppConfig(data_dir=data_dir, data_cleanup_enabled=False)

    asyncio.run(asyncio.wait_for(data_cleanup_loop(config, active, journal), timeout=0.1))

    assert expired.exists()


def test_data_cleanup_is_enabled_by_default() -> None:
    assert AppConfig().data_cleanup_enabled is True


def test_v8_data_cleanup_loop_uses_configured_retention(tmp_path) -> None:
    class RegistrySpy:
        def __init__(self) -> None:
            self.calls: list[tuple[float, int]] = []
            self.raw_calls: list[tuple[float, int]] = []
            self.checkpoint_calls = 0

        def cleanup_expired_snapshots(
            self,
            retention_hours: float,
            now: datetime,
            batch_size: int,
        ) -> int:
            assert now.tzinfo == timezone.utc
            self.calls.append((retention_hours, batch_size))
            return 1

        def cleanup_expired_raw_events(
            self,
            retention_hours: float,
            now: datetime,
            batch_size: int,
        ) -> int:
            assert now.tzinfo == timezone.utc
            self.raw_calls.append((retention_hours, batch_size))
            return 0

        def checkpoint_wal(self) -> None:
            self.checkpoint_calls += 1

    async def run_once() -> None:
        task = asyncio.create_task(btc_v8_data_cleanup_loop(config, registry, journal))
        while not registry.calls:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    active = tmp_path / "active"
    journal = RunJournal(active)
    config = AppConfig(
        data_dir=tmp_path,
        data_retention_hours=12,
        data_cleanup_interval_seconds=1,
    )
    registry = RegistrySpy()

    asyncio.run(run_once())

    assert registry.calls == [(12, 2_000)]
    assert registry.raw_calls == [(0, 5_000)]
    assert registry.checkpoint_calls == 1


def test_v8_data_cleanup_loop_returns_when_cleanup_is_disabled(tmp_path) -> None:
    class RegistrySpy:
        calls = 0

        def cleanup_expired_snapshots(self, *args, **kwargs) -> int:
            self.calls += 1
            return 0

    active = tmp_path / "active"
    journal = RunJournal(active)
    config = AppConfig(data_dir=tmp_path, data_cleanup_enabled=False)
    registry = RegistrySpy()

    asyncio.run(asyncio.wait_for(btc_v8_data_cleanup_loop(config, registry, journal), 0.1))

    assert registry.calls == 0
