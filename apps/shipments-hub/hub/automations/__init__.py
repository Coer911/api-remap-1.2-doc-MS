"""Каркас автоматизаций.

Автоматизация — модуль в этой папке с классом-наследником Automation,
помеченным @register. Хаб сам находит модули, берёт параметры из
config.yaml (automations.<name>) и вызывает:

    wants(shipment, event)  — дешёвый фильтр: надо ли что-то делать;
    key(shipment)           — ключ идемпотентности (по умолчанию id отгрузки:
                              «один раз на отгрузку»);
    run(shipment, ctx)      — сама работа; вернуть короткий итог для журнала,
                              бросить Skip("почему") — осознанный пропуск,
                              любое другое исключение — повтор по расписанию.

Шаблон новой автоматизации — _template.py, порядок — .claude/skills/new-automation.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass
from typing import Any, ClassVar

from ..channels import Channels
from ..models import Event, Shipment
from ..moysklad import MoySklad
from ..settings import Settings
from ..store import Store

REGISTRY: dict[str, type[Automation]] = {}


class Skip(Exception):
    """Делать нечего и повторять не надо: нет e-mail, не тот статус и т. п."""


class ConfigError(Exception):
    pass


def register(cls: type[Automation]) -> type[Automation]:
    if not cls.name:
        raise ConfigError(f"{cls.__name__}: не задано name")
    REGISTRY[cls.name] = cls
    return cls


@dataclass
class Context:
    """Всё, чем автоматизация может пользоваться во время run()."""
    ms: MoySklad
    store: Store
    channels: Channels
    settings: Settings
    log: logging.Logger

    def run_key(self, automation: Automation, shipment: Shipment) -> str:
        """Ключ запуска. В dry-run свой: пробные прогоны не блокируют настоящую отправку."""
        key = automation.key(shipment)
        return f"dry:{key}" if self.settings.dry_run else key

    def step_done(self, automation: str, key: str, step: str) -> bool:
        return self.store.get(f"step:{automation}:{key}:{step}") is not None

    def mark_step(self, automation: str, key: str, step: str, info: str = "ok") -> None:
        """Шаг (например, письмо) сделан — при повторе после сбоя его не повторять."""
        self.store.set(f"step:{automation}:{key}:{step}", info)


class Automation:
    name: ClassVar[str] = ""
    title: ClassVar[str] = ""
    entity: ClassVar[str] = "demand"
    defaults: ClassVar[dict[str, Any]] = {}

    def __init__(self, params: dict | None = None):
        params = dict(params or {})
        params.pop("enabled", None)
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ConfigError(f"{self.name}: неизвестные параметры {sorted(unknown)}")
        self.params = {**self.defaults, **params}

    def __getattr__(self, item: str) -> Any:
        # параметры доступны как атрибуты: self.state, self.template …
        params = self.__dict__.get("params", {})
        if item in params:
            return params[item]
        raise AttributeError(item)

    def wants(self, shipment: Shipment, event: Event) -> bool:
        return False

    def key(self, shipment: Shipment) -> str:
        return shipment.id

    def already_done(self, shipment: Shipment) -> bool:
        """Отметка в самом МойСклад — переживает потерю базы хаба."""
        mark = self.params.get("mark_attribute")
        return bool(mark and shipment.attributes.get(mark))

    async def run(self, shipment: Shipment, ctx: Context) -> str:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "title": self.title, "params": self.params}


def discover() -> None:
    """Импортирует все модули папки, чтобы сработали @register."""
    for mod in pkgutil.iter_modules(__path__):
        if not mod.name.startswith("_"):
            importlib.import_module(f"{__name__}.{mod.name}")


def enabled(config: dict) -> list[Automation]:
    """Экземпляры включённых в config.yaml автоматизаций."""
    discover()
    out = []
    for name, params in (config or {}).items():
        params = params or {}
        if name not in REGISTRY:
            raise ConfigError(f"в config.yaml автоматизация «{name}», а такой нет."
                              f" Есть: {', '.join(sorted(REGISTRY))}")
        if params.get("enabled", True):
            out.append(REGISTRY[name](params))
    return out
