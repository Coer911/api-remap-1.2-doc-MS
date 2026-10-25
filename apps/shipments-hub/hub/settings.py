"""Настройки хаба.

Секреты и адреса — только из переменных окружения (на Dockhost — панель
проекта → переменные окружения). Правила автоматизаций — в config.yaml:
это не секрет, его удобно держать в git и ревьюить.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

APP_DIR = Path(__file__).resolve().parent.parent


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "да")


@dataclass
class Settings:
    ms_token: str = ""
    ms_base: str = "https://api.moysklad.ru/api/remap/1.2"
    db_path: str = str(APP_DIR / "data" / "hub.sqlite3")
    config_path: str = str(APP_DIR / "config.yaml")
    api_key: str = ""                 # X-Api-Key для REST API хаба; пусто — API закрыт
    webhook_secret: str = ""          # часть пути вебхука /hooks/moysklad/<secret>
    public_url: str = ""              # https://… — внешний адрес, для установки вебхуков
    port: int = 8080
    poll_seconds: int = 300           # страховочный опрос отгрузок; 0 — выключен
    lookback_hours: int = 0           # при первом старте: сколько часов назад подхватить
    dry_run: bool = True              # True — ничего наружу не отправляется, только журнал
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = False
    telegram_token: str = ""
    alert_chat: str = ""              # Telegram chat_id для ошибок автоматизаций
    automations: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict | None = None) -> Settings:
        env = os.environ if env is None else env
        s = cls(
            ms_token=env.get("MOYSKLAD_TOKEN", ""),
            ms_base=env.get("MOYSKLAD_BASE", cls.ms_base),
            db_path=env.get("HUB_DB", cls.db_path),
            config_path=env.get("HUB_CONFIG", cls.config_path),
            api_key=env.get("HUB_API_KEY", ""),
            webhook_secret=env.get("HUB_WEBHOOK_SECRET", ""),
            public_url=env.get("HUB_PUBLIC_URL", "").rstrip("/"),
            port=int(env.get("PORT", cls.port)),
            poll_seconds=int(env.get("HUB_POLL_SECONDS", cls.poll_seconds)),
            lookback_hours=int(env.get("HUB_LOOKBACK_HOURS", cls.lookback_hours)),
            dry_run=_flag(env.get("HUB_DRY_RUN"), True),
            smtp_host=env.get("SMTP_HOST", ""),
            smtp_port=int(env.get("SMTP_PORT", cls.smtp_port)),
            smtp_user=env.get("SMTP_USER", ""),
            smtp_password=env.get("SMTP_PASSWORD", ""),
            smtp_from=env.get("SMTP_FROM", "") or env.get("SMTP_USER", ""),
            smtp_starttls=_flag(env.get("SMTP_STARTTLS"), False),
            telegram_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            alert_chat=env.get("HUB_ALERT_CHAT", ""),
        )
        s.automations = load_config(s.config_path).get("automations", {})
        return s

    @property
    def webhook_url(self) -> str:
        if not (self.public_url and self.webhook_secret):
            return ""
        return f"{self.public_url}/hooks/moysklad/{self.webhook_secret}"


def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
