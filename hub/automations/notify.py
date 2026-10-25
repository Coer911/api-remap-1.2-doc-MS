"""Уведомление людям: заказ, отгрузка или контрагент пришли в нужное состояние.

when:
  state          — сущность в статусе `state` (один раз на каждый вход в этот статус);
  created        — сущность только что создана (один раз);
  state_changed  — каждый переход статуса; `from_state` / `state` сужают переход.
"""
from __future__ import annotations

from ..entities import Entity
from ..events import Event
from . import Automation, ConfigError, Context, register, render

WHEN = {"state", "created", "state_changed"}


@register
class Notify(Automation):
    kind = "notify"
    title = "Уведомление"
    entities = ("customerorder", "demand", "counterparty")
    defaults = {
        "entity": "customerorder",
        "when": "state",
        "state": "",
        "from_state": "",
        "text": "{title} {number} · {state} · {agent} · {sum} ₽",
        "telegram_chats": [],
        "email_to": [],
        "subject": "{title} {number}: {state}",
        "webhook_url": "",
    }

    def validate(self) -> None:
        if self.when not in WHEN:
            raise ConfigError(f"{self.name}: when «{self.when}», можно {sorted(WHEN)}")
        if self.when == "state" and not self.state:
            raise ConfigError(f"{self.name}: when: state без state")
        if not (self.telegram_chats or self.email_to or self.webhook_url):
            raise ConfigError(f"{self.name}: некуда отправлять (telegram_chats, email_to, webhook_url)")

    def _is(self, value: str | None, want: str) -> bool:
        return (value or "").strip().lower() == want.strip().lower()

    def wants(self, obj: Entity, event: Event) -> bool:
        if self.when == "created":
            return event.is_new or event.action == "CREATE"
        if self.when == "state":
            return self._is(obj.state, self.state)
        # state_changed: только настоящий переход, и только известный
        if not event.state_changed(obj.state):
            return False
        if self.state and not self._is(obj.state, self.state):
            return False
        return not self.from_state or self._is(event.previous_state, self.from_state)

    def key(self, obj: Entity, event: Event | None) -> str:
        if self.when == "created":
            return obj.id
        if self.when == "state":
            return f"{obj.id}:{obj.state}"
        return f"{obj.id}:{obj.updated}"

    async def run(self, obj: Entity, ctx: Context) -> str:
        values = obj.fields()
        text = render(self.text, values)
        sent = [await ctx.channels.telegram_message(chat, text) for chat in self.telegram_chats]
        if self.email_to:
            sent.append(await ctx.channels.email(self.email_to, render(self.subject, values), text))
        if self.webhook_url:
            sent.append(await ctx.channels.webhook(self.webhook_url, {
                "event": self.name, "entity": obj.type, "text": text, "object": obj.to_dict()}))
        return "; ".join(sent)
