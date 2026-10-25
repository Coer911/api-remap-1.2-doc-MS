"""Шаблон новой автоматизации — скопируй в <имя>.py, переименуй класс и name.

Файлы с подчёркиванием в начале хаб не загружает, так что этот шаблон
никогда не запустится сам. Порядок работы — .claude/skills/new-automation.
"""
from __future__ import annotations

from ..models import Event, Shipment
from . import Automation, Context, Skip, register


@register
class Example(Automation):
    name = "example"                       # ключ в config.yaml и в журнале
    title = "Что делает — по-человечески"  # попадёт в алерты и /api/automations
    defaults = {
        "state": "Отгружен",               # все параметры перечисляются здесь
        "telegram_chats": [],
    }

    def wants(self, shipment: Shipment, event: Event) -> bool:
        # только дешёвые проверки по уже загруженной отгрузке, без запросов в API
        return shipment.state == self.state

    # def key(self, shipment):            # раз в отгрузку — по умолчанию;
    #     return f"{shipment.id}:{shipment.state}"   # раз на каждый статус — так

    async def run(self, shipment: Shipment, ctx: Context) -> str:
        if not self.telegram_chats:
            raise Skip("некому писать")
        text = f"Отгрузка {shipment.number} · {shipment.agent.name} · {shipment.sum_text} ₽"
        sent = [await ctx.channels.telegram_message(chat, text) for chat in self.telegram_chats]
        return "; ".join(sent)
