"""УПД за день — покупателям на почту по расписанию.

Каждый день в `at` (по Москве) хаб берёт отгрузки за сегодня и отбирает:
  * проведённые (отгруженные) — only_applicable;
  * созданные по заказу покупателя — only_with_order;
  * у контрагентов-ИП и юрлиц — company_types; name_prefixes дополнительно
    сужает по началу названия («ООО», «ИП»);
  * при желании — только в статусах `states` / кроме `exclude_states`.
Каждому контрагенту уходит одно письмо: УПД по всем его отгрузкам за день вложениями.

Каждая отгрузка отправляется один раз: повторный запуск, перезапуск хаба и повтор
после сбоя дошлют только то, что не ушло. Контрагенты без e-mail попадают в отчёт
(report_chats), а не в повторы.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from ..entities import Shipment, from_api
from . import Automation, ConfigError, Context, register, render
from .send_document import MIME

TYPES = {"legal": "юрлицо", "entrepreneur": "ИП", "individual": "физлицо"}


@register
class DailyUPD(Automation):
    kind = "daily_upd"
    title = "УПД за день покупателям"
    scheduled = True
    defaults = {
        "at": "17:00",
        "template": "УПД",
        "format": "pdf",
        "only_applicable": True,
        "only_with_order": True,
        "company_types": ["legal", "entrepreneur"],
        "name_prefixes": ["ООО", "ИП"],
        "states": [],
        "exclude_states": [],
        "email_cc": [],
        "subject": "УПД за {date} — {organization}",
        "body": ("Здравствуйте!\n\nНаправляем УПД за {date}:\n{documents}\n\n"
                 "С уважением,\n{organization}"),
        "mark_field": "",
        "report_chats": [],
    }

    def validate(self) -> None:
        try:
            datetime.strptime(self.at, "%H:%M")
        except ValueError:
            raise ConfigError(f"{self.name}: at «{self.at}», нужно ЧЧ:ММ") from None
        if self.format not in MIME:
            raise ConfigError(f"{self.name}: format «{self.format}», можно {sorted(MIME)}")
        if set(self.company_types) - set(TYPES):
            raise ConfigError(f"{self.name}: company_types — из {sorted(TYPES)}")

    def slot(self, now: datetime) -> str | None:
        return now.strftime("%Y-%m-%d") if now.strftime("%H:%M") >= self.at else None

    def _in(self, value: str, options: list[str]) -> bool:
        return value.strip().lower() in {o.strip().lower() for o in options}

    def why_not(self, sh: Shipment) -> str | None:
        """Почему отгрузка не подходит; None — подходит."""
        if self.only_applicable and not sh.applicable:
            return "не проведена"
        if self.only_with_order and not sh.order_id:
            return "без заказа"
        if self.company_types and sh.agent.company_type not in self.company_types:
            return TYPES.get(sh.agent.company_type, sh.agent.company_type or "тип не указан")
        if self.name_prefixes and not any(sh.agent.name.strip().upper().startswith(p.upper())
                                          for p in self.name_prefixes):
            return "не ООО/ИП по названию"
        if self.states and not self._in(sh.state, self.states):
            return f"статус «{sh.state}»"
        if self.exclude_states and self._in(sh.state, self.exclude_states):
            return f"статус «{sh.state}»"
        return None

    @staticmethod
    def recipients(sh: Shipment) -> list[str]:
        parts = sh.agent.email.replace(";", ",").split(",")
        return [a.strip() for a in dict.fromkeys(parts) if "@" in a]

    async def shipments(self, ctx: Context, day: str) -> list[Shipment]:
        flt = f"moment>={day} 00:00:00;moment<={day} 23:59:59"
        return [from_api("demand", row) async for row in
                ctx.ms.list("demand", filter=flt, expand=Shipment.expand, order="moment,asc")]

    async def run_scheduled(self, ctx: Context, slot: str) -> str:
        day = slot
        date_text = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
        skipped: dict[str, int] = defaultdict(int)
        by_agent: dict[str, list[Shipment]] = defaultdict(list)
        for sh in await self.shipments(ctx, day):
            reason = self.why_not(sh)
            if reason:
                skipped[reason] += 1
            else:
                by_agent[sh.agent.id].append(sh)

        template = await ctx.ms.find_template("demand", self.template) if by_agent else None
        sent_letters = sent_docs = already = 0
        no_email, errors = [], []
        for items in by_agent.values():
            agent = items[0].agent
            to = self.recipients(items[0])
            todo = [sh for sh in items
                    if not await ctx.step_done(self, ctx.run_key(self, f"demand:{sh.id}"), "email")]
            already += len(items) - len(todo)
            if not todo:
                continue
            if not to:
                no_email.append(f"{agent.name} ({', '.join(sh.number for sh in todo)})")
                continue
            try:
                files = []
                for sh in todo:
                    data = await ctx.ms.export("demand", sh.id, template, self.format)
                    files.append((f"УПД {sh.number} от {sh.date}.{self.format}", data, MIME[self.format]))
                documents = "\n".join(f"  — УПД № {sh.number} на сумму {sh.sum_text} ₽" for sh in todo)
                values = {**todo[0].fields(documents=documents), "date": date_text}
                result = await ctx.channels.email(to, render(self.subject, values),
                                                  render(self.body, values), attachments=files,
                                                  cc=self.email_cc)
            except Exception as e:                    # один контрагент не валит остальных
                errors.append(f"{agent.name}: {e}")
                continue
            for sh in todo:
                await ctx.mark_step(self, ctx.run_key(self, f"demand:{sh.id}"), "email", result)
                if self.mark_field:
                    await ctx.set_fields("demand", sh.id, {self.mark_field: f"{date_text} · {result}"})
            sent_letters += 1
            sent_docs += len(todo)

        lines = [f"УПД за {date_text}: писем {sent_letters}, документов {sent_docs}"]
        if already:
            lines.append(f"отправлено раньше: {already}")
        if no_email:
            lines.append("без e-mail: " + "; ".join(no_email))
        if skipped:
            lines.append("не подошли: " + ", ".join(f"{k} — {v}" for k, v in sorted(skipped.items())))
        if errors:
            lines.append("ошибки: " + "; ".join(errors))
        summary = "\n".join(lines)
        for chat in self.report_chats:
            await ctx.channels.telegram_message(chat, summary)
        if errors:
            # повтор по расписанию: уже ушедшие отгрузки не задублируются
            raise RuntimeError(summary)
        return summary
