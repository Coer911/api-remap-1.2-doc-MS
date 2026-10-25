"""Доп. поля и статус контрагента — заполняются сами.

Каждое поле из `fields` ставится, если оно пустое (only_empty) или всегда.
Значение — строка-шаблон с подстановками контрагента ({name}, {inn}, {email}…)
или готовое значение (число, да/нет, элемент справочника по имени).
`state` — поставить статус контрагента, если он не задан (или всегда, если
only_empty: false). when: created — только новым, any — при любом изменении.

Хаб сам меняет контрагента — МойСклад пришлёт об этом событие. Зацикливания нет:
поле уже заполнено, и автоматизации нечего делать.
"""
from __future__ import annotations

from typing import Any

from ..entities import Counterparty
from ..events import Event
from . import Automation, ConfigError, Context, Skip, register, render


@register
class CounterpartyFields(Automation):
    kind = "counterparty_fields"
    title = "Поля контрагента"
    entities = ("counterparty",)
    defaults = {"entity": "counterparty", "when": "created", "fields": {}, "set_state": "",
                "only_empty": True, "only_state": ""}

    def validate(self) -> None:
        if self.when not in ("created", "any"):
            raise ConfigError(f"{self.name}: when «{self.when}», можно created или any")
        if not (self.fields or self.set_state):
            raise ConfigError(f"{self.name}: не задано ни fields, ни set_state")

    def missing(self, obj: Counterparty) -> dict[str, Any]:
        out = {}
        for name, value in self.fields.items():
            if self.only_empty and obj.attr(name) not in (None, ""):
                continue
            out[name] = render(value, obj.fields()) if isinstance(value, str) else value
        return out

    def needs_state(self, obj: Counterparty) -> bool:
        if not self.set_state:
            return False
        return not obj.state or (not self.only_empty and obj.state != self.set_state)

    def wants(self, obj: Counterparty, event: Event) -> bool:
        if self.when == "created" and not (event.is_new or event.action == "CREATE"):
            return False
        if self.only_state and obj.state.lower() != self.only_state.lower():
            return False
        return bool(self.missing(obj)) or self.needs_state(obj)

    def key(self, obj: Counterparty, event: Event | None) -> str:
        return obj.id if self.when == "created" else f"{obj.id}:{obj.updated}"

    async def run(self, obj: Counterparty, ctx: Context) -> str:
        done = []
        values = self.missing(obj)
        if values:
            done.append(await ctx.set_fields("counterparty", obj.id, values))
        if self.needs_state(obj):
            done.append(await ctx.set_state("counterparty", obj.id, self.set_state))
        if not done:
            raise Skip("всё уже заполнено")
        return "; ".join(done)
