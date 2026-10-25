"""Шаблон новой автоматизации — скопируй в <имя>.py, переименуй класс и kind.

Файлы с подчёркиванием в начале хаб не загружает, так что шаблон сам не запустится.
Порядок работы — .claude/skills/new-automation/SKILL.md.
"""
from __future__ import annotations

from ..entities import Entity
from ..events import Event
from . import Automation, ConfigError, Context, Skip, register, render


@register
class Example(Automation):
    kind = "example"                         # uses: в config.yaml
    title = "Что делает — по-человечески"    # в алертах и /api/automations
    entities = ("customerorder", "demand")   # с какими типами умеет работать; первый — по умолчанию
    defaults = {                             # ВСЕ параметры с безопасными значениями
        "entity": "customerorder",
        "state": "Собран",
        "telegram_chats": [],
        "text": "{title} {number} · {agent}",
    }

    def validate(self) -> None:
        if not self.telegram_chats:
            raise ConfigError(f"{self.name}: нужен telegram_chats")

    def wants(self, obj: Entity, event: Event) -> bool:
        # только проверки по загруженной сущности, без запросов в API;
        # event.previous_state / event.state_changed(obj.state) — откуда пришёл статус
        return super().wants(obj, event)       # базовый фильтр по params["state"]

    def key(self, obj: Entity, event: Event | None) -> str:
        return f"{obj.id}:{obj.state}"         # один раз на каждый вход в статус

    async def run(self, obj: Entity, ctx: Context) -> str:
        if not obj.name:
            raise Skip("без номера")           # осознанный пропуск, без повторов
        text = render(self.text, obj.fields())
        sent = [await ctx.channels.telegram_message(chat, text) for chat in self.telegram_chats]
        # записи в МойСклад — только так (уважают HUB_DRY_RUN):
        # await ctx.set_state(obj.type, obj.id, "Уведомлён")
        # await ctx.set_fields(obj.type, obj.id, {"Уведомлён": True})
        return "; ".join(sent)
