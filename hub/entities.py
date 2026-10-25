"""Сущности МойСклад, с которыми работает хаб, в удобном для автоматизаций виде.

Добавить новый тип (счёт покупателю, приёмку …) — класс-наследник Entity и строка
в TYPES. Сырой JSON всегда лежит в .raw — если не хватает поля, бери оттуда.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import fields as dc_fields
from datetime import datetime
from typing import Any, ClassVar

from .moysklad import entity_id
from .moysklad.fields import plain


def _ref_id(d: dict | None) -> str:
    href = (d or {}).get("meta", {}).get("href", "")
    return entity_id(href) if href else ""


def money(kopecks: Any) -> float:
    return round((kopecks or 0) / 100, 2)


def money_text(rub: float) -> str:
    """12345.6 -> «12 345,60»"""
    whole, frac = f"{rub:,.2f}".split(".")
    return whole.replace(",", " ") + ("," + frac if frac != "00" else "")


@dataclass(kw_only=True)
class Party:
    id: str = ""
    name: str = ""
    email: str = ""
    phone: str = ""
    inn: str = ""

    @classmethod
    def from_api(cls, d: dict | None) -> Party:
        d = d or {}
        return cls(id=d.get("id") or _ref_id(d), name=d.get("name", ""),
                   email=(d.get("email") or "").strip(), phone=d.get("phone", ""),
                   inn=d.get("inn", ""))


@dataclass(kw_only=True)
class Entity:
    type: ClassVar[str] = ""
    title: ClassVar[str] = ""
    expand: ClassVar[str] = ""

    id: str
    name: str = ""
    state: str = ""
    updated: str = ""
    href: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def base_fields(cls, d: dict) -> dict:
        return {"id": d["id"], "name": d.get("name", ""),
                "state": (d.get("state") or {}).get("name", ""),
                "updated": d.get("updated", ""), "href": d.get("meta", {}).get("href", ""),
                "attributes": {a.get("name", ""): plain(a.get("value"))
                               for a in d.get("attributes", [])},
                "raw": d}

    @classmethod
    def from_api(cls, d: dict) -> Entity:
        return cls(**cls.base_fields(d))

    def attr(self, name: str, default: Any = None) -> Any:
        return self.attributes.get(name, default)

    @property
    def label(self) -> str:
        return f"{self.title} {self.name}"

    def fields(self, **extra) -> dict[str, Any]:
        """Подстановки для шаблонов сообщений: {name}, {state}, {поле:…} через attributes."""
        return {"id": self.id, "name": self.name, "number": self.name, "state": self.state,
                "type": self.type, "title": self.title, **extra}

    def to_dict(self) -> dict:
        out = {f.name: getattr(self, f.name) for f in dc_fields(self) if f.name != "raw"}
        out["type"] = self.type
        return out


@dataclass(kw_only=True)
class Document(Entity):
    """Общее у заказа и отгрузки: номер, дата, сумма, контрагент, юрлицо."""
    moment: str = ""
    sum: float = 0.0
    agent: Party = field(default_factory=Party)
    organization: str = ""
    applicable: bool = True
    description: str = ""
    shipment_address: str = ""

    @classmethod
    def base_fields(cls, d: dict) -> dict:
        return {**super().base_fields(d), "moment": d.get("moment", ""),
                "sum": money(d.get("sum")), "agent": Party.from_api(d.get("agent")),
                "organization": (d.get("organization") or {}).get("name", ""),
                "applicable": d.get("applicable", True),
                "description": d.get("description", ""),
                "shipment_address": d.get("shipmentAddress", "")}

    @property
    def number(self) -> str:
        return self.name

    @property
    def date(self) -> str:
        """ДД.ММ.ГГГГ"""
        try:
            return datetime.strptime(self.moment[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            return self.moment[:10]

    @property
    def sum_text(self) -> str:
        return money_text(self.sum)

    def fields(self, **extra) -> dict[str, Any]:
        return super().fields(date=self.date, sum=self.sum_text, agent=self.agent.name,
                              inn=self.agent.inn, email=self.agent.email,
                              organization=self.organization, address=self.shipment_address,
                              **extra)


@dataclass(kw_only=True)
class Order(Document):
    type: ClassVar[str] = "customerorder"
    title: ClassVar[str] = "Заказ"
    expand: ClassVar[str] = "agent,state,organization,salesChannel,project"

    delivery_planned: str = ""
    payed_sum: float = 0.0
    shipped_sum: float = 0.0
    reserved_sum: float = 0.0
    shipment_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, d: dict) -> Order:
        return cls(**cls.base_fields(d),
                   delivery_planned=d.get("deliveryPlannedMoment", ""),
                   payed_sum=money(d.get("payedSum")), shipped_sum=money(d.get("shippedSum")),
                   reserved_sum=money(d.get("reservedSum")),
                   shipment_ids=[_ref_id(x) for x in d.get("demands", [])])


@dataclass(kw_only=True)
class Shipment(Document):
    type: ClassVar[str] = "demand"
    title: ClassVar[str] = "Отгрузка"
    expand: ClassVar[str] = "agent,state,organization,customerOrder,salesChannel,project"

    order_id: str = ""
    order_number: str = ""
    payed_sum: float = 0.0

    @classmethod
    def from_api(cls, d: dict) -> Shipment:
        order = d.get("customerOrder") or {}
        return cls(**cls.base_fields(d), order_id=_ref_id(order),
                   order_number=order.get("name", ""), payed_sum=money(d.get("payedSum")))

    def fields(self, **extra) -> dict[str, Any]:
        return super().fields(order=self.order_number, **extra)


@dataclass(kw_only=True)
class Counterparty(Entity):
    type: ClassVar[str] = "counterparty"
    title: ClassVar[str] = "Контрагент"
    expand: ClassVar[str] = "state"

    email: str = ""
    phone: str = ""
    inn: str = ""
    kpp: str = ""
    legal_title: str = ""
    company_type: str = ""
    actual_address: str = ""
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, d: dict) -> Counterparty:
        return cls(**cls.base_fields(d), email=(d.get("email") or "").strip(),
                   phone=d.get("phone", ""), inn=d.get("inn", ""), kpp=d.get("kpp", ""),
                   legal_title=d.get("legalTitle", ""), company_type=d.get("companyType", ""),
                   actual_address=d.get("actualAddress", ""), tags=d.get("tags", []))

    @property
    def label(self) -> str:
        return f"{self.title} «{self.name}»"

    def fields(self, **extra) -> dict[str, Any]:
        return super().fields(email=self.email, phone=self.phone, inn=self.inn,
                              agent=self.name, **extra)


TYPES: dict[str, type[Entity]] = {cls.type: cls for cls in (Order, Shipment, Counterparty)}
# как тип называется в адресах REST API хаба
ALIASES = {"orders": "customerorder", "shipments": "demand", "counterparties": "counterparty"}
PRINTABLE = {"customerorder", "demand"}


def resolve_type(name: str) -> type[Entity]:
    """«orders» / «customerorder» → класс. Неизвестный тип — KeyError с подсказкой."""
    key = ALIASES.get(name, name)
    if key not in TYPES:
        raise KeyError(f"неизвестный тип «{name}». Есть: {', '.join(ALIASES)} "
                       f"({', '.join(TYPES)})")
    return TYPES[key]


def from_api(type_: str, d: dict) -> Entity:
    return TYPES[type_].from_api(d)
