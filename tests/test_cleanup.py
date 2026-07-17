from datetime import datetime, timedelta, timezone
import asyncio
import os

from polybtc.config import AppConfig
from polybtc.journal import RunJournal
from polybtc.runner import cleanup_expired_runs, data_cleanup_loop


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
    config = AppConfig(data_dir=data_dir)

    asyncio.run(asyncio.wait_for(data_cleanup_loop(config, active, journal), timeout=0.1))

    assert expired.exists()
