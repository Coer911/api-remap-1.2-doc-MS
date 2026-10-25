"""УПД по отгрузке — покупателю, без рук.

Когда отгрузка попадает в статус `state` (пустой state — сразу, как только
отгрузка появилась), хаб печатает её по шаблону `template` и отправляет
в каналы из `channels`:

  email    — письмо с УПД во вложении на e-mail из карточки контрагента
             (или на адреса из email_to);
  telegram — файл в чаты telegram_chats (менеджерам, складу, себе);
  webhook  — JSON с данными отгрузки, ссылкой и файлом во внешнюю
             автоматизацию (n8n, Make, свой бот) — дальше отправляет она.

Факт отправки пишется в доп. поле отгрузки `mark_attribute` — его видно
в МойСклад, и по нему хаб не отправит УПД второй раз даже с чистой базой.
"""
from __future__ import annotations

import base64
from datetime import datetime
from zoneinfo import ZoneInfo

from ..models import Event, Shipment
from . import Automation, ConfigError, Context, Skip, register

MIME = {"pdf": "application/pdf", "xls": "application/vnd.ms-excel",
        "ods": "application/vnd.oasis.opendocument.spreadsheet", "html": "text/html"}
MSK = ZoneInfo("Europe/Moscow")


class Fields(dict):
    """Неизвестная подстановка в шаблоне письма остаётся как есть, а не роняет отправку."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, fields: dict) -> str:
    return template.format_map(Fields(fields))


@register
class SendUPD(Automation):
    name = "send_upd"
    title = "УПД покупателю"
    defaults = {
        "state": "Отгружен",
        "template": "Универсальный передаточный документ",
        "format": "pdf",
        "channels": ["email"],
        "email_to": ["agent"],
        "email_cc": [],
        "subject": "УПД № {number} от {date} — {organization}",
        "body": ("Здравствуйте!\n\n"
                 "Направляем УПД № {number} от {date} на сумму {sum} ₽.\n\n"
                 "С уважением,\n{organization}"),
        "telegram_chats": [],
        "telegram_caption": "УПД № {number} от {date} · {agent} · {sum} ₽",
        "webhook_url": "",
        "webhook_with_file": True,
        "with_link": False,
        "mark_attribute": "УПД отправлен",
        "organizations": [],
        "not_before": "",
        "only_applicable": True,
    }

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        if self.format not in MIME:
            raise ConfigError(f"{self.name}: format «{self.format}», можно {sorted(MIME)}")
        bad = set(self.channels) - {"email", "telegram", "webhook"}
        if bad:
            raise ConfigError(f"{self.name}: неизвестные каналы {sorted(bad)}")
        if "webhook" in self.channels and not self.webhook_url:
            raise ConfigError(f"{self.name}: канал webhook без webhook_url")
        if "telegram" in self.channels and not self.telegram_chats:
            raise ConfigError(f"{self.name}: канал telegram без telegram_chats")
        if isinstance(self.email_to, str):
            self.params["email_to"] = [self.email_to]

    def wants(self, shipment: Shipment, event: Event) -> bool:
        if event.action == "DELETE":
            return False
        if self.only_applicable and not shipment.applicable:
            return False
        if self.organizations and shipment.organization not in self.organizations:
            return False
        if self.not_before and shipment.moment[:10] < self.not_before:
            return False
        if self.state:
            return shipment.state.strip().lower() == self.state.strip().lower()
        return True

    def recipients(self, shipment: Shipment) -> list[str]:
        out = []
        for item in self.email_to:
            if item == "agent":
                # в карточке бывает несколько адресов через запятую или точку с запятой
                out += [a.strip() for a in shipment.agent.email.replace(";", ",").split(",")]
            else:
                out.append(item)
        return [a for a in dict.fromkeys(out) if a and "@" in a]

    async def run(self, shipment: Shipment, ctx: Context) -> str:
        to = self.recipients(shipment)
        channels = [c for c in self.channels if c != "email" or to]
        if not channels:
            raise Skip(f"у контрагента «{shipment.agent.name}» нет e-mail")

        template = await ctx.ms.find_template(self.template)
        data = await ctx.ms.export("demand", shipment.id, template, self.format)
        filename = f"УПД {shipment.number} от {shipment.date}.{self.format}"
        link = await ctx.ms.publish("demand", shipment.id, template) if self.with_link else ""
        fields = shipment.fields(link=link)
        key = ctx.run_key(self, shipment)

        done = []
        if "email" in self.channels and not to:
            done.append("письмо: у контрагента нет e-mail")
        for channel in channels:
            if ctx.step_done(self.name, key, channel):
                done.append(f"{channel}: отправлено раньше")
                continue
            if channel == "email":
                body = render(self.body, fields) + (f"\n\nСсылка на документ: {link}" if link else "")
                result = await ctx.channels.email(
                    to, render(self.subject, fields), body,
                    attachments=[(filename, data, MIME[self.format])], cc=self.email_cc)
            elif channel == "telegram":
                caption = render(self.telegram_caption, fields)
                result = "; ".join([await ctx.channels.telegram_document(chat, filename, data, caption)
                                    for chat in self.telegram_chats])
            else:
                payload = {"event": "upd", "automation": self.name,
                           "shipment": shipment.to_dict(), "link": link, "filename": filename}
                if self.webhook_with_file:
                    payload["file"] = {"mime": MIME[self.format],
                                       "base64": base64.b64encode(data).decode()}
                result = await ctx.channels.webhook(self.webhook_url, payload)
            ctx.mark_step(self.name, key, channel, result)
            done.append(result)

        summary = "; ".join(done)
        if self.mark_attribute and not ctx.settings.dry_run:
            await self.mark(shipment, ctx, summary)
        return summary

    async def mark(self, shipment: Shipment, ctx: Context, summary: str) -> None:
        attr = await ctx.ms.attribute(self.mark_attribute)
        if not attr:
            ctx.log.warning("нет доп. поля «%s» у отгрузок — python -m hub setup",
                            self.mark_attribute)
            return
        now = datetime.now(MSK)
        kind = attr.get("type", "string")
        if kind == "boolean":
            value = True
        elif kind == "time":
            value = now.strftime("%Y-%m-%d %H:%M:%S")
        else:
            value = f"{now:%d.%m.%Y %H:%M} · {summary}"[:255]
        await ctx.ms.set_attribute("demand", shipment.id, attr, value)
