"""Событие — «с сущностью что-то произошло»: из вебхука, опроса, ручного запуска."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Event:
    entity: str                           # customerorder | demand | counterparty
    entity_id: str
    action: str                           # CREATE | UPDATE | DELETE | POLL | MANUAL
    fields: list[str] | None = None       # updatedFields из вебхука с diffType=FIELDS
    source: str = "webhook"
    # заполняет движок по снимку предыдущего состояния:
    previous_state: str | None = None     # статус до изменения (None — хаб видит впервые)
    is_new: bool = False                  # сущность хаб видит впервые

    def state_changed(self, current: str) -> bool:
        """Статус сменился: по полям вебхука или по сравнению со снимком."""
        if self.fields is not None and "state" in self.fields:
            return True
        return self.previous_state is not None and self.previous_state != current


def merge(events: list[Event]) -> Event:
    """Несколько событий по одной сущности → одно: поля объединяются, CREATE побеждает."""
    first = events[0]
    actions = [e.action for e in events]
    merged: list[str] | None = []
    for e in events:
        if e.fields is None:
            merged = None
            break
        merged.extend(f for f in e.fields if f not in merged)
    if "CREATE" in actions:
        action = "CREATE"
    elif all(a == "DELETE" for a in actions):
        action = "DELETE"
    else:
        action = next(a for a in actions if a != "DELETE")
    return Event(entity=first.entity, entity_id=first.entity_id, action=action, fields=merged,
                 source=first.source)
