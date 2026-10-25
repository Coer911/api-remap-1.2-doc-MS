"""Состояние хаба в SQLite: очередь событий, журнал запусков, курсоры.

Очередь нужна, потому что МойСклад ждёт ответ на вебхук не дольше 1500 мс
и потом выбрасывает уведомление (dictionaries/_webhook.md) — поэтому вебхук
только кладёт событие сюда, а обрабатывает его воркер.

Журнал запусков (runs) — защита от повторной отправки: пара
(автоматизация, ключ) выполняется успешно ровно один раз.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import Event

# пауза перед повтором после 1-й, 2-й, … неудачи; дальше — dead и алерт
BACKOFF = [60, 300, 1800, 7200, 21600]

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id  TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    fields      TEXT,
    source      TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    UNIQUE (request_id, entity_id)
);
CREATE INDEX IF NOT EXISTS events_pending ON events (processed_at, id);

CREATE TABLE IF NOT EXISTS runs (
    automation  TEXT NOT NULL,
    key         TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    number      TEXT,
    status      TEXT NOT NULL,          -- running | done | failed | dead | skipped
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_at     TEXT,
    result      TEXT,
    error       TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (automation, key)
);
CREATE INDEX IF NOT EXISTS runs_due ON runs (status, next_at);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Run:
    automation: str
    key: str
    entity_id: str
    number: str
    status: str
    attempts: int
    next_at: str | None
    result: str | None
    error: str | None
    updated_at: str


@dataclass
class QueuedEvent:
    id: int
    event: Event


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # ── очередь событий ──────────────────────────────────────────────────────

    def enqueue(self, event: Event, request_id: str) -> bool:
        """False — такое уведомление уже было (МойСклад повторяет с тем же requestId)."""
        cur = self._exec(
            "INSERT OR IGNORE INTO events (request_id, entity, entity_id, action, fields, source,"
            " received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (request_id, event.entity, event.entity_id, event.action,
             json.dumps(event.fields) if event.fields is not None else None,
             event.source, iso(now_utc())))
        return cur.rowcount == 1

    def pending(self, limit: int = 100) -> list[QueuedEvent]:
        rows = self._exec("SELECT * FROM events WHERE processed_at IS NULL ORDER BY id LIMIT ?",
                          (limit,)).fetchall()
        return [QueuedEvent(r["id"], Event(
            entity_id=r["entity_id"], action=r["action"], entity=r["entity"], source=r["source"],
            fields=json.loads(r["fields"]) if r["fields"] is not None else None)) for r in rows]

    def mark_processed(self, ids: list[int]) -> None:
        if ids:
            marks = ",".join("?" * len(ids))
            self._exec(f"UPDATE events SET processed_at = ? WHERE id IN ({marks})",
                       (iso(now_utc()), *ids))

    def pending_count(self) -> int:
        return self._exec("SELECT COUNT(*) FROM events WHERE processed_at IS NULL").fetchone()[0]

    def prune(self, days: int = 30) -> None:
        """Обработанные события старше days больше не нужны."""
        self._exec("DELETE FROM events WHERE processed_at IS NOT NULL AND processed_at < ?",
                   (iso(now_utc() - timedelta(days=days)),))

    # ── журнал запусков ──────────────────────────────────────────────────────

    def run(self, automation: str, key: str) -> Run | None:
        r = self._exec("SELECT * FROM runs WHERE automation = ? AND key = ?",
                       (automation, key)).fetchone()
        return Run(**dict(r)) if r else None

    def start(self, automation: str, key: str, entity_id: str, number: str) -> None:
        self._exec(
            "INSERT INTO runs (automation, key, entity_id, number, status, attempts, updated_at)"
            " VALUES (?, ?, ?, ?, 'running', 0, ?)"
            " ON CONFLICT (automation, key) DO UPDATE SET status = 'running',"
            " number = excluded.number, updated_at = excluded.updated_at",
            (automation, key, entity_id, number, iso(now_utc())))

    def finish(self, automation: str, key: str, status: str, result: str = "") -> None:
        self._exec("UPDATE runs SET status = ?, result = ?, error = NULL, next_at = NULL,"
                   " attempts = attempts + 1, updated_at = ? WHERE automation = ? AND key = ?",
                   (status, result, iso(now_utc()), automation, key))

    def fail(self, automation: str, key: str, error: str) -> Run:
        """Неудача: назначает повтор по BACKOFF или переводит в dead."""
        run = self.run(automation, key)
        attempts = (run.attempts if run else 0) + 1
        if attempts > len(BACKOFF):
            status, next_at = "dead", None
        else:
            status, next_at = "failed", iso(now_utc() + timedelta(seconds=BACKOFF[attempts - 1]))
        self._exec("UPDATE runs SET status = ?, attempts = ?, next_at = ?, error = ?,"
                   " updated_at = ? WHERE automation = ? AND key = ?",
                   (status, attempts, next_at, error[:2000], iso(now_utc()), automation, key))
        return self.run(automation, key)

    def recover(self) -> int:
        """После падения процесса незавершённые запуски уходят в повтор сразу."""
        cur = self._exec("UPDATE runs SET status = 'failed', next_at = ?, error = COALESCE(error,"
                         " 'процесс прервался во время запуска') WHERE status = 'running'",
                         (iso(now_utc()),))
        return cur.rowcount

    def due(self, limit: int = 20) -> list[Run]:
        rows = self._exec("SELECT * FROM runs WHERE status = 'failed' AND next_at <= ?"
                          " ORDER BY next_at LIMIT ?", (iso(now_utc()), limit)).fetchall()
        return [Run(**dict(r)) for r in rows]

    def journal(self, limit: int = 50, automation: str = "", entity_id: str = "",
                status: str = "") -> list[Run]:
        sql, args = "SELECT * FROM runs WHERE 1=1", []
        for col, val in (("automation", automation), ("entity_id", entity_id), ("status", status)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        rows = self._exec(sql, (*args, limit)).fetchall()
        return [Run(**dict(r)) for r in rows]

    # ── ключ-значение: курсоры опроса, шаги автоматизаций ────────────────────

    def get(self, key: str, default: str | None = None) -> str | None:
        r = self._exec("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default

    def set(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv (key, value) VALUES (?, ?)"
                   " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
