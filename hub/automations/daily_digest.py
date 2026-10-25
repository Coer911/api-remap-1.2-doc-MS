"""Сводка по расписанию: заказы и отгрузки за день по статусам — в Telegram/почту.

Запускается раз в день в `at` (по Москве). Если хаб в это время лежал, сводка
уйдёт, как только он поднимется в тот же день.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from ..entities import money, money_text
from . import Automation, ConfigError, Context, register

NAMES = {"customerorder": "Заказы", "demand": "Отгрузки"}


@register
class DailyDigest(Automation):
    kind = "daily_digest"
    title = "Сводка за день"
    scheduled = True
    defaults = {"at": "18:00", "types": ["customerorder", "demand"], "days_back": 0,
                "telegram_chats": [], "email_to": [], "subject": "Сводка МойСклад за {date}"}

    def validate(self) -> None:
        try:
            datetime.strptime(self.at, "%H:%M")
        except ValueError:
            raise ConfigError(f"{self.name}: at «{self.at}», нужно ЧЧ:ММ") from None
        if set(self.types) - set(NAMES):
            raise ConfigError(f"{self.name}: types — только {sorted(NAMES)}")
        if not (self.telegram_chats or self.email_to):
            raise ConfigError(f"{self.name}: некуда отправлять")

    def slot(self, now: datetime) -> str | None:
        return now.strftime("%Y-%m-%d") if now.strftime("%H:%M") >= self.at else None

    async def run_scheduled(self, ctx: Context, slot: str) -> str:
        day = datetime.strptime(slot, "%Y-%m-%d") - timedelta(days=self.days_back)
        start, end = day.strftime("%Y-%m-%d 00:00:00"), day.strftime("%Y-%m-%d 23:59:59")
        lines = [f"📋 {day:%d.%m.%Y}"]
        for type_ in self.types:
            counts: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
            async for row in ctx.ms.list(type_, filter=f"moment>={start};moment<={end}",
                                         expand="state"):
                bucket = counts[(row.get("state") or {}).get("name", "без статуса")]
                bucket[0] += 1
                bucket[1] += money(row.get("sum"))
            total = sum(c[0] for c in counts.values())
            lines.append(f"\n{NAMES[type_]}: {total}")
            for state, (n, s) in sorted(counts.items(), key=lambda kv: -kv[1][0]):
                lines.append(f"  {state}: {n} · {money_text(s)} ₽")
        text = "\n".join(lines)
        sent = [await ctx.channels.telegram_message(chat, text) for chat in self.telegram_chats]
        if self.email_to:
            sent.append(await ctx.channels.email(
                self.email_to, self.subject.format(date=f"{day:%d.%m.%Y}"), text))
        return "; ".join(sent)
