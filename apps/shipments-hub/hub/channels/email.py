"""Отправка писем по SMTP (Яндекс 360, Mail.ru, свой сервер — любой с логином)."""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid


def build(sender: str, to: list[str], subject: str, body: str,
          attachments: list[tuple[str, bytes, str]] = (), cc: list[str] = ()) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender if "<" in sender else formataddr(("", sender))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1].strip(">") or None)
    msg.set_content(body)
    for filename, data, mime in attachments:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream",
                           filename=filename)
    return msg


def _send_sync(host: str, port: int, user: str, password: str, starttls: bool,
               msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if starttls:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=context)
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)


async def send(host: str, port: int, user: str, password: str, starttls: bool,
               msg: EmailMessage) -> None:
    await asyncio.to_thread(_send_sync, host, port, user, password, starttls, msg)
