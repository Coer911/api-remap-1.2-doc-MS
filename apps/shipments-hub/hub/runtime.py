"""Сборка хаба целиком и его фоновые циклы: воркер очереди и опрос."""
from __future__ import annotations

import asyncio
import logging

from .automations import Context, enabled
from .channels import Channels
from .engine import Engine, Outcome
from .ingest import poll
from .moysklad import MoySklad
from .settings import Settings
from .store import Store

log = logging.getLogger("hub")
WORKER_IDLE = 5.0          # воркер просыпается сам раз в 5 с, вебхук будит сразу
PRUNE_EVERY = 24 * 3600


class Hub:
    def __init__(self, settings: Settings, ms: MoySklad | None = None,
                 store: Store | None = None, channels: Channels | None = None):
        self.settings = settings
        self.automations = enabled(settings.automations)
        self.ms = ms or MoySklad(settings.ms_token, settings.ms_base)
        self.store = store or Store(settings.db_path)
        self.channels = channels or Channels(settings)
        self.ctx = Context(self.ms, self.store, self.channels, settings,
                           logging.getLogger("hub.automation"))
        self.engine = Engine(self.ctx, self.automations)
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> list[Outcome]:
        return await self.engine.process_pending() + await self.engine.retry_due()

    async def poll_once(self) -> int:
        added = await poll(self.ms, self.store, self.settings.lookback_hours)
        if added:
            self.wake()
        return added

    async def _worker(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("воркер: круг не удался, повторю")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=WORKER_IDLE)
            except TimeoutError:
                pass
            self._wake.clear()

    async def _poller(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("опрос МойСклад не удался, повторю через %s с",
                              self.settings.poll_seconds)
            await asyncio.sleep(self.settings.poll_seconds)

    async def _pruner(self) -> None:
        while True:
            await asyncio.sleep(PRUNE_EVERY)
            self.store.prune()

    async def start(self) -> None:
        recovered = self.store.recover()
        if recovered:
            log.warning("после перезапуска в повтор ушло запусков: %s", recovered)
        if self.settings.dry_run:
            log.warning("HUB_DRY_RUN включён: наружу ничего не отправляется, только журнал")
        log.info("автоматизации: %s", ", ".join(a.name for a in self.automations) or "нет")
        self._tasks = [asyncio.create_task(self._worker(), name="worker"),
                       asyncio.create_task(self._pruner(), name="pruner")]
        if self.settings.poll_seconds > 0:
            self._tasks.append(asyncio.create_task(self._poller(), name="poller"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.ms.close()
        await self.channels.close()
        self.store.close()
