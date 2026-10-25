"""Печатная форма документа — покупателю, без рук.

send_document — любой шаблон печати заказа или отгрузки (счёт, УПД, ТОРГ-12, заказ);
send_upd — то же с настройками для УПД по отгрузке.

Когда документ в статусе `state` (пустой — сразу, как появился), хаб печатает его
по шаблону `template` и отправляет в каналы `channels`:
  email    — письмо с файлом на e-mail контрагента (или адреса из email_to);
  telegram — файл в чаты telegram_chats;
  webhook  — JSON с документом, ссылкой и файлом во внешнюю автоматизацию.
После отправки: отметка в доп. поле `mark_field` и, если задан, статус `state_after`.
"""
from __future__ import annotations

import base64
from datetime import datetime
from zoneinfo import ZoneInfo

from ..entities import Document
from ..events import Event
from . import Automation, ConfigError, Context, Skip, register, render

MIME = {"pdf": "application/pdf", "xls": "application/vnd.ms-excel",
        "ods": "application/vnd.oasis.opendocument.spreadsheet", "html": "text/html"}
MSK = ZoneInfo("Europe/Moscow")
CHANNELS = {"email", "telegram", "webhook"}


@register
class SendDocument(Automation):
    kind = "send_document"
    title = "Документ покупателю"
    entities = ("demand", "customerorder")
    defaults = {
        "entity": "demand",
        "state": "",
        "template": "",
        "file_title": "",                     # начало имени файла; пусто — имя шаблона
        "format": "pdf",
        "channels": ["email"],
        "email_to": ["agent"],                # agent — e-mail из карточки контрагента
        "email_cc": [],
        "subject": "{file_title} № {number} от {date} — {organization}",
        "body": ("Здравствуйте!\n\nНаправляем {file_title} № {number} от {date}"
                 " на сумму {sum} ₽.\n\nС уважением,\n{organization}"),
        "telegram_chats": [],
        "telegram_caption": "{file_title} № {number} от {date} · {agent} · {sum} ₽",
        "webhook_url": "",
        "webhook_with_file": True,
        "with_link": False,
        "mark_field": "",
        "state_after": "",
        "organizations": [],
        "not_before": "",
        "only_applicable": True,
    }

    def validate(self) -> None:
        if not self.template:
            raise ConfigError(f"{self.name}: не задан template (python -m hub templates {self.entity})")
        if self.format not in MIME:
            raise ConfigError(f"{self.name}: format «{self.format}», можно {sorted(MIME)}")
        if set(self.channels) - CHANNELS:
            raise ConfigError(f"{self.name}: неизвестные каналы {sorted(set(self.channels) - CHANNELS)}")
        if "webhook" in self.channels and not self.webhook_url:
            raise ConfigError(f"{self.name}: канал webhook без webhook_url")
        if "telegram" in self.channels and not self.telegram_chats:
            raise ConfigError(f"{self.name}: канал telegram без telegram_chats")
        if isinstance(self.email_to, str):
            self.params["email_to"] = [self.email_to]

    def wants(self, obj: Document, event: Event) -> bool:
        if self.only_applicable and not obj.applicable:
            return False
        if self.organizations and obj.organization not in self.organizations:
            return False
        if self.not_before and obj.moment[:10] < self.not_before:
            return False
        return super().wants(obj, event)

    def recipients(self, obj: Document) -> list[str]:
        out = []
        for item in self.email_to:
            if item == "agent":
                out += [a.strip() for a in obj.agent.email.replace(";", ",").split(",")]
            else:
                out.append(item)
        return [a for a in dict.fromkeys(out) if a and "@" in a]

    async def run(self, obj: Document, ctx: Context) -> str:
        to = self.recipients(obj)
        channels = [c for c in self.channels if c != "email" or to]
        if not channels:
            raise Skip(f"у контрагента «{obj.agent.name}» нет e-mail")

        template = await ctx.ms.find_template(obj.type, self.template)
        data = await ctx.ms.export(obj.type, obj.id, template, self.format)
        file_title = self.file_title or template.get("name", self.template)
        filename = f"{file_title} {obj.number} от {obj.date}.{self.format}"
        link = await ctx.ms.publish(obj.type, obj.id, template) if self.with_link else ""
        values = obj.fields(link=link, file_title=file_title)
        key = ctx.run_key(self, self.key(obj, None))

        done = ["письмо: у контрагента нет e-mail"] if "email" in self.channels and not to else []
        for channel in channels:
            if await ctx.step_done(self, key, channel):
                done.append(f"{channel}: отправлено раньше")
                continue
            if channel == "email":
                body = render(self.body, values) + (f"\n\nСсылка на документ: {link}" if link else "")
                result = await ctx.channels.email(
                    to, render(self.subject, values), body,
                    attachments=[(filename, data, MIME[self.format])], cc=self.email_cc)
            elif channel == "telegram":
                caption = render(self.telegram_caption, values)
                result = "; ".join([await ctx.channels.telegram_document(chat, filename, data, caption)
                                    for chat in self.telegram_chats])
            else:
                payload = {"event": self.name, "entity": obj.type, "document": obj.to_dict(),
                           "link": link, "filename": filename}
                if self.webhook_with_file:
                    payload["file"] = {"mime": MIME[self.format],
                                       "base64": base64.b64encode(data).decode()}
                result = await ctx.channels.webhook(self.webhook_url, payload)
            await ctx.mark_step(self, key, channel, result)
            done.append(result)

        if self.mark_field:
            stamp = f"{datetime.now(MSK):%d.%m.%Y %H:%M} · {'; '.join(done)}"[:255]
            done.append(await ctx.set_fields(obj.type, obj.id, {self.mark_field: stamp}))
        if self.state_after:
            done.append(await ctx.set_state(obj.type, obj.id, self.state_after))
        return "; ".join(done)


@register
class SendUPD(SendDocument):
    kind = "send_upd"
    title = "УПД покупателю"
    entities = ("demand",)
    defaults = {**SendDocument.defaults,
                "state": "Отгружен",
                "template": "Универсальный передаточный документ",
                "file_title": "УПД",
                "mark_field": "УПД отправлен"}
