"""Каналы доставки: почта, Telegram, исходящий вебхук.

Автоматизации ходят наружу только через Channels — так режим HUB_DRY_RUN
выключает все отправки разом, а тесты подменяют один объект.
"""
from __future__ import annotations

import logging

import httpx

from ..settings import Settings
from . import email as email_

log = logging.getLogger("hub.channels")
TELEGRAM_API = "https://api.telegram.org"


class ChannelError(Exception):
    pass


class Channels:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=30.0)
        self.sent: list[dict] = []          # что ушло (и что «ушло бы» в dry-run) — для журнала

    async def close(self) -> None:
        await self.http.aclose()

    def _dry(self, kind: str, **info) -> str:
        self.sent.append({"kind": kind, "dry_run": True, **info})
        log.info("DRY RUN %s %s", kind, info)
        return f"dry-run {kind}"

    async def email(self, to: list[str], subject: str, body: str,
                    attachments: list[tuple[str, bytes, str]] = (), cc: list[str] = ()) -> str:
        to = [a for a in to if a]
        if not to:
            raise ChannelError("нет адреса получателя")
        if self.s.dry_run:
            return self._dry("email", to=to, cc=list(cc), subject=subject,
                             files=[a[0] for a in attachments])
        if not (self.s.smtp_host and self.s.smtp_from):
            raise ChannelError("почта не настроена: SMTP_HOST / SMTP_FROM")
        msg = email_.build(self.s.smtp_from, to, subject, body, list(attachments), list(cc))
        await email_.send(self.s.smtp_host, self.s.smtp_port, self.s.smtp_user,
                          self.s.smtp_password, self.s.smtp_starttls, msg)
        self.sent.append({"kind": "email", "to": to, "subject": subject})
        return f"письмо {', '.join(to)}"

    async def _telegram(self, method: str, data: dict, files: dict | None = None) -> dict:
        if not self.s.telegram_token:
            raise ChannelError("Telegram не настроен: TELEGRAM_BOT_TOKEN")
        r = await self.http.post(f"{TELEGRAM_API}/bot{self.s.telegram_token}/{method}",
                                 data=data, files=files)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not body.get("ok"):
            raise ChannelError(f"Telegram {method}: {r.status_code} {body.get('description', '')}")
        return body

    async def telegram_document(self, chat: str, filename: str, data: bytes,
                                caption: str = "") -> str:
        if self.s.dry_run:
            return self._dry("telegram", chat=chat, file=filename, caption=caption)
        await self._telegram("sendDocument", {"chat_id": chat, "caption": caption[:1024]},
                             files={"document": (filename, data)})
        self.sent.append({"kind": "telegram", "chat": chat, "file": filename})
        return f"telegram {chat}"

    async def telegram_message(self, chat: str, text: str) -> str:
        if self.s.dry_run:
            return self._dry("telegram", chat=chat, text=text)
        await self._telegram("sendMessage", {"chat_id": chat, "text": text[:4096],
                                             "disable_web_page_preview": "true"})
        return f"telegram {chat}"

    async def webhook(self, url: str, payload: dict) -> str:
        """POST JSON во внешнюю автоматизацию (n8n, Make, свой бот) — она отправит сама."""
        if self.s.dry_run:
            return self._dry("webhook", url=url, keys=sorted(payload))
        r = await self.http.post(url, json=payload)
        if r.status_code >= 300:
            raise ChannelError(f"вебхук {url}: {r.status_code}")
        self.sent.append({"kind": "webhook", "url": url})
        return f"вебхук {url}"

    async def alert(self, text: str) -> None:
        """Сообщить людям, что автоматизация сдалась. Ошибку алерта только логируем."""
        log.error("ALERT %s", text)
        if self.s.alert_chat and self.s.telegram_token:
            try:
                await self.telegram_message(self.s.alert_chat, text)
            except Exception:
                log.exception("не удалось отправить алерт")
