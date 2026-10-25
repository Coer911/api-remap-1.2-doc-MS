"""Состояние хаба: очередь событий, журнал запусков, снимки статусов, курсоры.

Один код на двух базах:
  * SQLite — один контейнер с ролью all (по умолчанию, ничего ставить не надо);
  * PostgreSQL — когда процессов несколько (роли api / worker / scheduler, реплики
    воркеров). Конкурентность держится на аренде (lease) и атомарных UPDATE:
    событие или запуск берёт ровно один воркер, упавший воркер отпускает их
    по истечении аренды, а FOR UPDATE SKIP LOCKED не даёт воркерам ждать друг друга.

Время хранится строками ISO UTC фиксированной длины — они правильно сравниваются
как строки в обеих базах.
"""
from __future__ import annotations

import asyncio
import json
import zlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    delete,
    event,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from ..events import Event

# пауза перед повтором после 1-й, 2-й, … неудачи; дальше — dead и алерт
BACKOFF = [60, 300, 1800, 7200, 21600]
# событие, которое не удаётся разобрать столько раз подряд, уходит в «мёртвые»
EVENT_MAX_ATTEMPTS = 20

metadata = MetaData()

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", String(200), nullable=False),
    Column("entity", String(40), nullable=False),
    Column("entity_id", String(64), nullable=False),
    Column("action", String(16), nullable=False),
    Column("fields", Text),
    Column("source", String(16), nullable=False),
    Column("received_at", String(20), nullable=False),
    Column("lease_until", String(20)),
    Column("worker", String(64)),
    Column("attempts", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("processed_at", String(20)),
    UniqueConstraint("request_id", "entity", "entity_id", name="events_dedup"),
    Index("events_pending", "processed_at", "id"),
)

runs = Table(
    "runs", metadata,
    Column("automation", String(80), nullable=False),
    Column("key", String(255), nullable=False),
    Column("entity", String(40), nullable=False),
    Column("entity_id", String(64), nullable=False),
    Column("label", String(255)),
    Column("status", String(16), nullable=False),       # running | done | failed | dead | skipped
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_at", String(20)),
    Column("lease_until", String(20)),
    Column("result", Text),
    Column("error", Text),
    Column("updated_at", String(20), nullable=False),
    PrimaryKeyConstraint("automation", "key", name="runs_pk"),
    Index("runs_due", "status", "next_at"),
    Index("runs_entity", "entity_id"),
)

snapshots = Table(
    "snapshots", metadata,
    Column("entity", String(40), nullable=False),
    Column("entity_id", String(64), nullable=False),
    Column("state", String(255), nullable=False, default=""),
    Column("updated", String(32), nullable=False, default=""),
    PrimaryKeyConstraint("entity", "entity_id", name="snapshots_pk"),
)

kv = Table(
    "kv", metadata,
    Column("key", String(255), primary_key=True),
    Column("value", Text, nullable=False),
)


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def later(seconds: float) -> str:
    return iso(now() + timedelta(seconds=seconds))


@dataclass
class Claimed:
    id: int
    event: Event
    attempts: int


@dataclass
class Run:
    automation: str
    key: str
    entity: str
    entity_id: str
    label: str | None
    status: str
    attempts: int
    next_at: str | None
    lease_until: str | None
    result: str | None
    error: str | None
    updated_at: str


@dataclass
class Snapshot:
    previous_state: str | None    # None — сущность раньше не встречалась
    is_new: bool
    advanced: bool                # снимок сдвинут этим вызовом (не устаревшее событие)


def normalize_url(url: str) -> str:
    """postgresql://… → postgresql+asyncpg://…; путь без схемы → SQLite-файл."""
    if url.startswith(("postgres://", "postgresql://")):
        return "postgresql+asyncpg://" + url.split("://", 1)[1]
    if "://" not in url:
        return f"sqlite+aiosqlite:///{url}"
    return url


class Storage:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self.dialect = engine.dialect.name
        self._leader: AsyncConnection | None = None
        # SQLite — один процесс и один писатель: транзакции по очереди, без «database is locked»
        self._lock = asyncio.Lock() if self.dialect == "sqlite" else None

    @asynccontextmanager
    async def _tx(self):
        if self._lock is None:
            async with self.engine.begin() as conn:
                yield conn
        else:
            async with self._lock, self.engine.begin() as conn:
                yield conn

    @asynccontextmanager
    async def _ro(self):
        if self._lock is None:
            async with self.engine.connect() as conn:
                yield conn
        else:
            async with self._lock, self.engine.connect() as conn:
                yield conn

    @classmethod
    async def open(cls, url: str) -> Storage:
        url = normalize_url(url)
        if url.startswith("sqlite") and ":memory:" not in url:
            from pathlib import Path
            Path(url.split("///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(url, pool_pre_ping=not url.startswith("sqlite"))
        if engine.dialect.name == "sqlite":
            @event.listens_for(engine.sync_engine, "connect")
            def _pragmas(dbapi, _):
                cur = dbapi.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        return cls(engine)

    @property
    def is_sqlite(self) -> bool:
        return self.dialect == "sqlite"

    async def close(self) -> None:
        if self._leader is not None:
            await self._leader.close()
            self._leader = None
        await self.engine.dispose()

    def _insert(self, table: Table):
        return (pg_insert if self.dialect == "postgresql" else sqlite_insert)(table)

    # ── очередь событий ──────────────────────────────────────────────────────

    async def enqueue(self, ev: Event, request_id: str) -> bool:
        """False — такое уведомление уже было (МойСклад повторяет с тем же requestId)."""
        stmt = self._insert(events).values(
            request_id=request_id, entity=ev.entity, entity_id=ev.entity_id, action=ev.action,
            fields=json.dumps(ev.fields) if ev.fields is not None else None,
            source=ev.source, received_at=iso(now()), attempts=0,
        ).on_conflict_do_nothing(index_elements=["request_id", "entity", "entity_id"])
        async with self._tx() as conn:
            return (await conn.execute(stmt)).rowcount == 1

    async def claim(self, limit: int, worker: str, lease: float) -> list[Claimed]:
        """Взять до limit необработанных событий в аренду на lease секунд."""
        t = iso(now())
        free = and_(events.c.processed_at.is_(None),
                    or_(events.c.lease_until.is_(None), events.c.lease_until < t))
        ids = (select(events.c.id).where(free).order_by(events.c.id).limit(limit)
               .with_for_update(skip_locked=True))
        stmt = (update(events).where(events.c.id.in_(ids)).where(free)
                .values(lease_until=later(lease), worker=worker)
                .returning(events.c.id, events.c.entity, events.c.entity_id, events.c.action,
                           events.c.fields, events.c.source, events.c.attempts))
        async with self._tx() as conn:
            rows = (await conn.execute(stmt)).all()
        out = [Claimed(r.id, Event(entity=r.entity, entity_id=r.entity_id, action=r.action,
                                   fields=json.loads(r.fields) if r.fields else None,
                                   source=r.source), r.attempts) for r in rows]
        return sorted(out, key=lambda c: c.id)

    async def complete(self, ids: list[int]) -> None:
        if ids:
            async with self._tx() as conn:
                await conn.execute(update(events).where(events.c.id.in_(ids))
                                   .values(processed_at=iso(now()), lease_until=None, error=None))

    async def release(self, ids: list[int], error: str) -> None:
        """Не получилось разобрать: вернуть в очередь, а после EVENT_MAX_ATTEMPTS — списать."""
        if not ids:
            return
        async with self._tx() as conn:
            await conn.execute(update(events).where(events.c.id.in_(ids)).values(
                attempts=events.c.attempts + 1, lease_until=None, error=error[:2000]))
            await conn.execute(update(events).where(events.c.id.in_(ids))
                               .where(events.c.attempts >= EVENT_MAX_ATTEMPTS)
                               .values(processed_at=iso(now())))

    async def pending_count(self) -> int:
        async with self._ro() as conn:
            return (await conn.execute(select(func.count()).select_from(events)
                                       .where(events.c.processed_at.is_(None)))).scalar_one()

    async def prune(self, days: int = 30) -> None:
        async with self._tx() as conn:
            await conn.execute(delete(events).where(events.c.processed_at.is_not(None))
                               .where(events.c.processed_at < iso(now() - timedelta(days=days))))

    # ── журнал запусков ──────────────────────────────────────────────────────

    def _pk(self, automation: str, key: str):
        return and_(runs.c.automation == automation, runs.c.key == key)

    async def run(self, automation: str, key: str) -> Run | None:
        async with self._ro() as conn:
            r = (await conn.execute(select(runs).where(self._pk(automation, key)))).first()
        return Run(**r._mapping) if r else None

    async def try_start(self, automation: str, key: str, *, entity: str, entity_id: str,
                        label: str, lease: float, force: bool = False) -> tuple[bool, Run | None]:
        """Атомарно занять запуск. (True, None) — новый; (True, run) — повтор; (False, run) — занят/готов.

        Обычный режим берёт только новый запуск, просроченный повтор (failed, next_at прошёл)
        или брошенный (running с истёкшей арендой). force берёт всё, кроме живого running.
        """
        t = iso(now())
        values = dict(status="running", lease_until=later(lease), updated_at=t, label=label)
        stmt = self._insert(runs).values(
            automation=automation, key=key, entity=entity, entity_id=entity_id, attempts=0,
            **values).on_conflict_do_nothing(index_elements=["automation", "key"])
        async with self._tx() as conn:
            if (await conn.execute(stmt)).rowcount == 1:
                return True, None
        existing = await self.run(automation, key)
        alive = and_(runs.c.status == "running", runs.c.lease_until > t)
        if force:
            cond = ~alive
        else:
            cond = or_(and_(runs.c.status == "failed", runs.c.next_at <= t),
                       and_(runs.c.status == "running", runs.c.lease_until <= t))
        async with self._tx() as conn:
            res = await conn.execute(update(runs).where(self._pk(automation, key)).where(cond)
                                     .values(**values))
        return res.rowcount == 1, existing

    async def finish(self, automation: str, key: str, status: str, result: str = "") -> None:
        async with self._tx() as conn:
            await conn.execute(update(runs).where(self._pk(automation, key)).values(
                status=status, result=result, error=None, next_at=None, lease_until=None,
                attempts=runs.c.attempts + 1, updated_at=iso(now())))

    async def fail(self, automation: str, key: str, error: str) -> Run:
        """Неудача: назначает повтор по BACKOFF или переводит в dead."""
        run = await self.run(automation, key)
        attempts = (run.attempts if run else 0) + 1
        if attempts > len(BACKOFF):
            status, next_at = "dead", None
        else:
            status, next_at = "failed", later(BACKOFF[attempts - 1])
        async with self._tx() as conn:
            await conn.execute(update(runs).where(self._pk(automation, key)).values(
                status=status, attempts=attempts, next_at=next_at, lease_until=None,
                error=error[:2000], updated_at=iso(now())))
        return await self.run(automation, key)

    async def due(self, limit: int = 20) -> list[Run]:
        t = iso(now())
        cond = or_(and_(runs.c.status == "failed", runs.c.next_at <= t),
                   and_(runs.c.status == "running", runs.c.lease_until <= t))
        async with self._ro() as conn:
            rows = (await conn.execute(select(runs).where(cond).order_by(runs.c.next_at)
                                       .limit(limit))).all()
        return [Run(**r._mapping) for r in rows]

    async def journal(self, limit: int = 50, *, automation: str = "", entity_id: str = "",
                      status: str = "") -> list[Run]:
        q = select(runs)
        for col, val in ((runs.c.automation, automation), (runs.c.entity_id, entity_id),
                         (runs.c.status, status)):
            if val:
                q = q.where(col == val)
        async with self._ro() as conn:
            rows = (await conn.execute(q.order_by(runs.c.updated_at.desc()).limit(limit))).all()
        return [Run(**r._mapping) for r in rows]

    async def stats(self) -> dict:
        async with self._ro() as conn:
            by_status = dict((await conn.execute(
                select(runs.c.status, func.count()).group_by(runs.c.status))).all())
        return {"pending_events": await self.pending_count(), "runs": by_status}

    # ── снимки статусов: из них движок знает, откуда сущность пришла ─────────

    async def advance(self, entity: str, entity_id: str, state: str, updated: str) -> Snapshot:
        """Запомнить статус сущности. Сдвигает снимок только вперёд по updated."""
        pk = and_(snapshots.c.entity == entity, snapshots.c.entity_id == entity_id)
        async with self._tx() as conn:
            row = (await conn.execute(select(snapshots).where(pk))).first()
            if row is None:
                ins = self._insert(snapshots).values(
                    entity=entity, entity_id=entity_id, state=state, updated=updated
                ).on_conflict_do_nothing(index_elements=["entity", "entity_id"])
                if (await conn.execute(ins)).rowcount == 1:
                    return Snapshot(None, True, True)
                row = (await conn.execute(select(snapshots).where(pk))).first()
            if updated > row.updated:
                res = await conn.execute(update(snapshots).where(pk)
                                         .where(snapshots.c.updated == row.updated)
                                         .values(state=state, updated=updated))
                return Snapshot(row.state, False, res.rowcount == 1)
            return Snapshot(row.state, False, False)

    # ── ключ-значение: курсоры опроса, шаги автоматизаций ────────────────────

    async def get(self, key: str, default: str | None = None) -> str | None:
        async with self._ro() as conn:
            value = (await conn.execute(select(kv.c.value).where(kv.c.key == key))).scalar()
        return default if value is None else value

    async def set(self, key: str, value: str) -> None:
        stmt = self._insert(kv).values(key=key, value=value)
        stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": value})
        async with self._tx() as conn:
            await conn.execute(stmt)

    # ── лидер: опрос и расписание должны идти ровно в одном процессе ────────

    async def acquire_leader(self, name: str = "scheduler") -> bool:
        """PostgreSQL: advisory lock на всё время жизни процесса. SQLite: процесс и так один."""
        if self.dialect != "postgresql":
            return True
        if self._leader is not None:
            return True
        conn = await self.engine.connect()
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                  {"k": zlib.crc32(name.encode())})).scalar()
        if got:
            self._leader = conn
            return True
        await conn.close()
        return False
