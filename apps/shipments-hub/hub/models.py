"""Отгрузка в том виде, в каком с ней удобно писать автоматизации.

Сырой JSON МойСклад остаётся в Shipment.raw — если не хватает поля, бери оттуда.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .moysklad import entity_id


@dataclass
class Party:
    id: str = ""
    name: str = ""
    email: str = ""
    phone: str = ""
    inn: str = ""

    @classmethod
    def from_api(cls, d: dict | None) -> Party:
        d = d or {}
        href = d.get("meta", {}).get("href", "")
        return cls(id=d.get("id") or (entity_id(href) if href else ""),
                   name=d.get("name", ""), email=(d.get("email") or "").strip(),
                   phone=d.get("phone", ""), inn=d.get("inn", ""))


@dataclass
class Event:
    """Что произошло с документом: из вебхука, опроса или ручного запуска."""
    entity_id: str
    action: str                           # CREATE | UPDATE | DELETE | POLL | MANUAL
    fields: list[str] | None = None       # updatedFields из вебхука с diffType=FIELDS
    source: str = "webhook"
    entity: str = "demand"

    @property
    def state_changed(self) -> bool | None:
        """True/False — известно из вебхука; None — неизвестно (опрос, вебхук без полей)."""
        if self.fields is None:
            return None
        return "state" in self.fields


@dataclass
class Shipment:
    id: str
    number: str
    moment: str                           # «2026-10-25 14:03:00.000», время московское
    sum: float                            # рубли (в API — копейки)
    state: str
    agent: Party
    organization: str
    applicable: bool = True
    description: str = ""
    href: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> Shipment:
        attrs = {}
        for a in d.get("attributes", []):
            v = a.get("value")
            attrs[a.get("name", "")] = v.get("name") if isinstance(v, dict) else v
        return cls(
            id=d["id"], number=d.get("name", ""), moment=d.get("moment", ""),
            sum=round((d.get("sum") or 0) / 100, 2),
            state=(d.get("state") or {}).get("name", ""),
            agent=Party.from_api(d.get("agent")),
            organization=(d.get("organization") or {}).get("name", ""),
            applicable=d.get("applicable", True),
            description=d.get("description", ""),
            href=d.get("meta", {}).get("href", ""),
            attributes=attrs, raw=d,
        )

    @property
    def date(self) -> str:
        """ДД.ММ.ГГГГ — для писем и имён файлов."""
        try:
            return datetime.strptime(self.moment[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            return self.moment[:10]

    @property
    def sum_text(self) -> str:
        """12 345,60"""
        whole, frac = f"{self.sum:,.2f}".split(".")
        return whole.replace(",", " ") + ("," + frac if frac != "00" else "")

    def fields(self, **extra) -> dict[str, Any]:
        """Подстановки для шаблонов писем: {number}, {date}, {sum}, {agent}, …"""
        return {"number": self.number, "date": self.date, "sum": self.sum_text,
                "agent": self.agent.name, "inn": self.agent.inn, "email": self.agent.email,
                "organization": self.organization, "state": self.state,
                "id": self.id, **extra}

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw")
        d["date"] = self.date
        return d
