"""Хранилище держит конкурентность: несколько воркеров не берут одно и то же."""
import asyncio

from conftest import fresh_storage

from hub.events import Event
from hub.storage import EVENT_MAX_ATTEMPTS


async def test_claim_is_exclusive_and_lease_expires():
    s = await fresh_storage()
    try:
        for i in range(10):
            await s.enqueue(Event("demand", f"d{i}", "UPDATE"), f"r{i}")
        a, b = await asyncio.gather(s.claim(6, "w1", 60), s.claim(6, "w2", 60))
        ids_a, ids_b = {c.id for c in a}, {c.id for c in b}
        assert not ids_a & ids_b and len(ids_a | ids_b) == 10
        assert await s.claim(5, "w3", 60) == []            # всё в аренде
        await s.complete(list(ids_a))
        await s.release(list(ids_b), "МойСклад недоступен")
        again = await s.claim(20, "w4", 60)
        assert {c.id for c in again} == ids_b and all(c.attempts == 1 for c in again)
    finally:
        await s.close()


async def test_dead_worker_lease_expires():
    s = await fresh_storage()
    try:
        await s.enqueue(Event("demand", "d1", "UPDATE"), "r")
        first = await s.claim(1, "w1", -1)                  # воркер взял и умер: аренда истекла
        second = await s.claim(1, "w2", 60)
        assert [c.id for c in first] == [c.id for c in second]
    finally:
        await s.close()


async def test_poison_event_is_retired():
    s = await fresh_storage()
    try:
        await s.enqueue(Event("demand", "bad", "UPDATE"), "r")
        for _ in range(EVENT_MAX_ATTEMPTS):
            batch = await s.claim(1, "w", 60)
            await s.release([c.id for c in batch], "кривое событие")
        assert await s.pending_count() == 0
    finally:
        await s.close()


async def test_run_starts_once_under_race():
    s = await fresh_storage()
    try:
        results = await asyncio.gather(*[
            s.try_start("send_upd", "d1", entity="demand", entity_id="d1", label="x", lease=60)
            for _ in range(5)])
        assert sum(started for started, _ in results) == 1
        await s.finish("send_upd", "d1", "done", "ok")
        assert (await s.try_start("send_upd", "d1", entity="demand", entity_id="d1",
                                  label="x", lease=60))[0] is False
        assert (await s.try_start("send_upd", "d1", entity="demand", entity_id="d1",
                                  label="x", lease=60, force=True))[0] is True
    finally:
        await s.close()


async def test_snapshot_moves_forward_only():
    s = await fresh_storage()
    try:
        first = await s.advance("customerorder", "o1", "Новый", "2026-10-25 10:00:00.000")
        assert first.is_new and first.previous_state is None
        nxt = await s.advance("customerorder", "o1", "Собран", "2026-10-25 11:00:00.000")
        assert nxt.previous_state == "Новый" and nxt.advanced
        stale = await s.advance("customerorder", "o1", "Новый", "2026-10-25 09:00:00.000")
        assert not stale.advanced and stale.previous_state == "Собран"
    finally:
        await s.close()
