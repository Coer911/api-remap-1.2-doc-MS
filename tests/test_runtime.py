"""Роли и фоновые циклы: то, что крутится в контейнере."""
import asyncio

import pytest
from conftest import webhook


async def test_background_worker_picks_up_webhook(fake, make_hub):
    hub = await make_hub({"send_upd": {}})
    d = fake.add_demand("00401")
    await hub.start("all") if hub.storage.is_sqlite else await hub.start("worker")
    try:
        await webhook(hub, fake, "demand", d["id"], "r")
        hub.wake()
        for _ in range(50):
            if hub.channels.emails:
                break
            await asyncio.sleep(0.05)
        assert len(hub.channels.emails) == 1
    finally:
        for t in hub._tasks:
            t.cancel()
        await asyncio.gather(*hub._tasks, return_exceptions=True)


async def test_split_roles_need_postgres(make_hub):
    hub = await make_hub({"send_upd": {}})
    if not hub.storage.is_sqlite:
        pytest.skip("на PostgreSQL роли разрешены")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        await hub.start("worker")


async def test_only_needed_types_are_watched(make_hub):
    hub = await make_hub({"send_upd": {}, "daily_digest": {"telegram_chats": ["-1"]},
                          "fill": {"uses": "counterparty_fields", "fields": {"X": "1"}}})
    assert hub.types == ["counterparty", "demand"]


async def test_two_workers_send_once(fake, make_hub):
    """Две реплики воркера на одной PostgreSQL: события по отгрузке разбирают обе, письмо — одно."""
    from conftest import DB_URL

    from hub.storage import Storage
    first = await make_hub({"send_upd": {}})
    if first.storage.is_sqlite:
        pytest.skip("общая база для нескольких процессов — только PostgreSQL")
    second = await make_hub({"send_upd": {}}, storage=await Storage.open(DB_URL))
    d = fake.add_demand("00402")
    for i in range(6):
        await webhook(first, fake, "demand", d["id"], f"r{i}")
    first.settings.worker_batch = second.settings.worker_batch = 1
    for _ in range(4):
        await asyncio.gather(first.tick(), second.tick())
    assert len(first.channels.emails) + len(second.channels.emails) == 1
    assert await first.storage.pending_count() == 0


async def test_single_scheduler_leader():
    from conftest import DB_URL

    from hub.storage import Storage
    a, b = await Storage.open(DB_URL), await Storage.open(DB_URL)
    try:
        if a.is_sqlite:
            pytest.skip("на SQLite процесс один — лидер всегда он")
        assert await a.acquire_leader("test-leader") is True
        assert await b.acquire_leader("test-leader") is False
        await a.close()
        assert await b.acquire_leader("test-leader") is True
    finally:
        await b.close()
        await a.close()
