"""Каркас автоматизаций.

Автоматизация — класс-наследник Automation с @register в модуле этой папки.
В config.yaml у каждой записи есть имя и (необязательно) `uses` — какой класс
взять; так один класс (например notify) работает под разными именами
с разными параметрами:

    automations:
      order_ready:            # имя — в журнале, API и ключах идемпотентности
        uses: notify          # класс; по умолчанию = имени
        entity: customerorder
        state: "Собран"

Событийная автоматизация отвечает на вопросы:
    wants(obj, event)  — надо ли что-то делать (дёшево, без запросов в API);
    key(obj, event)    — ключ идемпотентности: одинаковый ключ выполняется один раз;
    run(obj, ctx)      — работа; вернуть итог для журнала; Skip — осознанный пропуск,
                         любое другое исключение — повтор по расписанию.
Плановая (scheduled = True) вместо этого: slot(now) и run_scheduled(ctx, slot).

Наружу (почта, Telegram) — только через ctx.channels, записи в МойСклад — только
через ctx.set_state / ctx.set_fields: так HUB_DRY_RUN выключает побочные эффекты разом.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from ..channels import Channels
from ..entities import TYPES, Entity
from ..events import Event
from ..moysklad import MoySklad
from ..moysklad import fields as ms_fields
from ..settings import Settings
from ..storage import Storage

REGISTRY: dict[str, type[Automation]] = {}


class Skip(Exception):
    """Делать нечего и повторять не надо: нет e-mail, нет связанного заказа и т. п."""


class ConfigError(Exception):
    pass


def register(cls: type[Automation]) -> type[Automation]:
    if not cls.kind:
        raise ConfigError(f"{cls.__name__}: не задан kind")
    REGISTRY[cls.kind] = cls
    return cls


class Fields(dict):
    """Неизвестная подстановка в шаблоне остаётся как есть, а не роняет отправку."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, values: dict) -> str:
    return template.format_map(Fields(values))


@dataclass
class Context:
    """Всё, чем автоматизация пользуется во время работы."""
    ms: MoySklad
    storage: Storage
    channels: Channels
    settings: Settings
    log: logging.Logger
    writes: list[dict] = field(default_factory=list)   # записи в МойСклад (и пробные)

    def run_key(self, automation: Automation, key: str) -> str:
        """В dry-run свой ключ: пробные прогоны не блокируют настоящую работу."""
        return f"dry:{key}" if self.settings.dry_run else key

    async def step_done(self, automation: Automation, key: str, step: str) -> bool:
        return await self.storage.get(f"step:{automation.name}:{key}:{step}") is not None

    async def mark_step(self, automation: Automation, key: str, step: str, info: str = "ok") -> None:
        """Шаг (письмо в один канал) сделан — после сбоя следующего шага его не повторять."""
        await self.storage.set(f"step:{automation.name}:{key}:{step}", info)

    async def set_state(self, type_: str, id_: str, state: str) -> str:
        self.writes.append({"type": type_, "id": id_, "state": state,
                            "dry_run": self.settings.dry_run})
        if self.settings.dry_run:
            await ms_fields.find_state(self.ms, type_, state)        # проверить, что статус есть
            return f"dry-run статус «{state}»"
        await ms_fields.set_state(self.ms, type_, id_, state)
        return f"статус «{state}»"

    async def set_fields(self, type_: str, id_: str, values: dict[str, Any]) -> str:
        self.writes.append({"type": type_, "id": id_, "fields": dict(values),
                            "dry_run": self.settings.dry_run})
        names = ", ".join(values)
        if self.settings.dry_run:
            for name in values:
                await ms_fields.find_definition(self.ms, type_, name)
            return f"dry-run поля {names}"
        await ms_fields.set_fields(self.ms, type_, id_, values)
        return f"поля {names}"


class Automation:
    kind: ClassVar[str] = ""
    title: ClassVar[str] = ""
    entities: ClassVar[tuple[str, ...]] = ()      # с какими типами умеет работать
    scheduled: ClassVar[bool] = False
    defaults: ClassVar[dict[str, Any]] = {}

    def __init__(self, name: str, params: dict | None = None):
        params = dict(params or {})
        params.pop("enabled", None)
        params.pop("uses", None)
        clash = {k for k in self.defaults if k != "entity" and hasattr(type(self), k)}
        if clash:
            raise ConfigError(f"{self.kind}: параметры {sorted(clash)} совпадают с атрибутами класса")
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ConfigError(f"{name}: неизвестные параметры {sorted(unknown)};"
                              f" можно {sorted(self.defaults)}")
        self.name = name
        self.params = {**self.defaults, **params}
        if not self.scheduled and self.entity not in self.entities:
            raise ConfigError(f"{name}: entity «{self.entity}», а {self.kind} умеет"
                              f" {', '.join(self.entities)}")
        self.validate()

    def __getattr__(self, item: str) -> Any:
        params = self.__dict__.get("params", {})
        if item in params:
            return params[item]
        raise AttributeError(item)

    @property
    def entity(self) -> str:
        return self.params.get("entity") or (self.entities[0] if self.entities else "")

    def validate(self) -> None:
        """Проверка параметров при старте — ошибка конфига видна сразу, а не в 3 часа ночи."""

    # ── событийные ──
    def wants(self, obj: Entity, event: Event) -> bool:
        state = self.params.get("state")
        return not state or obj.state.strip().lower() == state.strip().lower()

    def key(self, obj: Entity, event: Event) -> str:
        return obj.id

    def already_done(self, obj: Entity) -> bool:
        """Отметка в самом МойСклад — переживает потерю базы хаба."""
        mark = self.params.get("mark_field")
        return bool(mark and obj.attr(mark))

    async def run(self, obj: Entity, ctx: Context) -> str:
        raise NotImplementedError

    # ── плановые ──
    def slot(self, now: datetime) -> str | None:
        """Идентификатор «окна» запуска (например дата), если пора. None — рано."""
        return None

    async def run_scheduled(self, ctx: Context, slot: str) -> str:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "uses": self.kind, "title": self.title,
                "entity": None if self.scheduled else self.entity, "params": self.params}


def discover() -> None:
    """Импортирует все модули папки, чтобы сработали @register."""
    for mod in pkgutil.iter_modules(__path__):
        if not mod.name.startswith("_"):
            importlib.import_module(f"{__name__}.{mod.name}")


def build(config: dict) -> list[Automation]:
    """Экземпляры включённых в config.yaml автоматизаций."""
    discover()
    out = []
    for name, params in (config or {}).items():
        params = dict(params or {})
        kind = params.get("uses", name)
        if kind not in REGISTRY:
            raise ConfigError(f"{name}: нет автоматизации «{kind}». Есть: {', '.join(sorted(REGISTRY))}")
        if params.get("enabled", True):
            out.append(REGISTRY[kind](name, params))
    return out


def event_types(automations: list[Automation]) -> list[str]:
    """Типы сущностей, за которыми хабу надо следить (опрос и вебхуки)."""
    return sorted({a.entity for a in automations if not a.scheduled} & set(TYPES))
