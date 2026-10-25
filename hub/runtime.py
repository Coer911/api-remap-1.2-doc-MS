"""Сборка хаба и его роли.

  all        — всё в одном процессе (один контейнер, SQLite или PostgreSQL);
  api        — приём вебхуков и REST API; без состояния, реплик сколько угодно;
  worker     — разбор очереди и повторы; реплик сколько угодно (нужен PostgreSQL);
  scheduler  — опрос МойСклад и плановые автоматизации; активен ровно один —
               лишние реплики ждут advisory lock в PostgreSQL.

Масштабирование = тот же образ, другая HUB_ROLE. Код автоматизаций не меняется.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

from .automations import Context, build, event_types
from .channels import Channels
from .engine import Engine, Outcome
from .ingest import poll
from .moysklad import MoySklad
from .settings import Settings
from .storage import Storage

log = logging.getLogger("hub")
WORKER_IDLE = 2.0          # воркер сам проверяет очередь раз в 2 с; вебхук в том же процессе будит сразу
SCHEDULE_EVERY = 30.0
LEADER_RETRY = 30.0
PRUNE_EVERY = 24 * 3600


class Hub:
    def __init__(self, settings: Settings, storage: Storage, *, ms: MoySklad | None = None,
                 channels: Channels | None = None):
        self.settings = settings
        self.storage = storage
        self.automations = build(settings.automations)
        self.ms = ms or MoySklad(settings.ms_token, settings.ms_base, rps=settings.ms_rps)
        self.channels = channels or Channels(settings)
        self.ctx = Context(self.ms, storage, self.channels, settings,
                           logging.getLogger("hub.automation"))
        worker = f"{socket.gethostname()}:{os.getpid()}"
        self.engine = Engine(self.ctx, self.automations, worker=worker)
        self.types = event_types(self.automations)
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @classmethod
    async def create(cls, settings: Settings, **kw) -> Hub:
        return cls(settings, await Storage.open(settings.database_url), **kw)

    def wake(self) -> None:
        self._wake.set()

    # ── единицы работы (их же зовут CLI и тесты) ────────────────────────────

    async def tick(self) -> list[Outcome]:
        return await self.engine.process() + await self.engine.retry_due()

    async def poll_once(self) -> int:
        added = 0
        for type_ in self.types:
            added += await poll(self.ms, self.storage, type_, self.settings.lookback_hours)
        if added:
            self.wake()
        return added

    # ── фоновые циклы ────────────────────────────────────────────────────────

    async def _loop(self, name: str, work, every: float, wake: bool = False) -> None:
        while True:
            try:
                await work()
            except Exception:
                log.exception("%s: круг не удался, повторю", name)
            if wake:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=every)
                except TimeoutError:
                    pass
                self._wake.clear()
            else:
                await asyncio.sleep(every)

    async def _scheduler(self) -> None:
        while not await self.storage.acquire_leader():
            log.info("планировщик уже работает в другом процессе — жду")
            await asyncio.sleep(LEADER_RETRY)
        log.info("планировщик: этот процесс ведущий")
        tasks = [self._loop("расписание", self.engine.scheduled, SCHEDULE_EVERY),
                 self._loop("чистка", self.storage.prune, PRUNE_EVERY)]
        if self.settings.poll_seconds > 0:
            tasks.append(self._loop("опрос", self.poll_once, self.settings.poll_seconds))
        await asyncio.gather(*tasks)

    async def start(self, role: str | None = None) -> None:
        role = role or self.settings.role
        if self.storage.is_sqlite and role != "all":
            raise RuntimeError(f"роль {role} отдельным процессом — только с PostgreSQL"
                               " (HUB_DATABASE_URL); на SQLite запускайте роль all")
        if self.settings.dry_run:
            log.warning("HUB_DRY_RUN включён: наружу и в МойСклад ничего не пишется, только журнал")
        log.info("роль %s; автоматизации: %s; слежу за: %s", role,
                 ", ".join(a.name for a in self.automations) or "нет", ", ".join(self.types) or "—")
        if role in ("all", "worker"):
            self._tasks.append(asyncio.create_task(
                self._loop("воркер", self.tick, WORKER_IDLE, wake=True), name="worker"))
        if role in ("all", "scheduler"):
            self._tasks.append(asyncio.create_task(self._scheduler(), name="scheduler"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self.ms.close()
        await self.channels.close()
        await self.storage.close()
