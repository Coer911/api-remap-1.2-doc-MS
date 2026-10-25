"""Статусы и доп. поля любых сущностей — по человеческим именам.

МойСклад хранит статус и значение доп. поля ссылками ({"meta": …}). Здесь — перевод
«Отгружен» / «Канал продаж = HoReCa» в такие ссылки и обратно.

Типы доп. полей и формат value (_general.md, «Работа с дополнительными полями»):
  string, text, link → строка; long → целое; double → дробное; boolean → true/false;
  time → «ГГГГ-ММ-ДД ЧЧ:мм:сс»; file — не через этот модуль;
  справочник (counterparty, product, project, store, employee, contract, customentity)
  → {"meta": …} элемента. value = null сбрасывает значение (кроме файла).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from .client import MoySklad, entity_id, meta_of

SCALAR = {"string", "text", "link", "long", "double", "boolean", "time"}
DICTIONARY = {"counterparty", "product", "project", "store", "employee", "contract",
              "customentity"}
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TRUE = {"1", "true", "yes", "да", "on", "+"}
FALSE = {"0", "false", "no", "нет", "off", "-", ""}


class FieldError(ValueError):
    """Нет такого статуса/поля или значение не подходит по типу. Текст — для людей."""


def _names(rows: list[dict]) -> str:
    return ", ".join(f"«{r.get('name', '')}»" for r in rows) or "нет ни одного"


# ── статусы ──────────────────────────────────────────────────────────────────

async def states(ms: MoySklad, type_: str) -> list[dict]:
    return (await ms.metadata(type_)).get("states", [])


async def find_state(ms: MoySklad, type_: str, name: str) -> dict:
    rows = await states(ms, type_)
    for row in rows:
        if row.get("name", "").strip().lower() == name.strip().lower():
            return row
    raise FieldError(f"у {type_} нет статуса «{name}». Есть: {_names(rows)}")


async def set_state(ms: MoySklad, type_: str, id_: str, name: str) -> dict:
    state = await find_state(ms, type_, name)
    return await ms.update(type_, id_, {"state": meta_of(state)})


# ── доп. поля ────────────────────────────────────────────────────────────────

async def definitions(ms: MoySklad, type_: str) -> list[dict]:
    return (await ms.metadata(type_)).get("attributes", [])


async def find_definition(ms: MoySklad, type_: str, name: str) -> dict:
    rows = await definitions(ms, type_)
    for row in rows:
        if row.get("name", "").strip().lower() == name.strip().lower():
            return row
    raise FieldError(f"у {type_} нет доп. поля «{name}». Есть: {_names(rows)}")


def plain(value: Any) -> Any:
    """Значение доп. поля из ответа API → то, что удобно сравнивать и показывать."""
    if isinstance(value, dict):
        return value.get("name", value.get("meta", {}).get("href", ""))
    return value


def _time(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d 00:00:00")
    text = str(value).strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise FieldError(f"«{value}» — не дата (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД)")


async def _dictionary(ms: MoySklad, d: dict, value: Any) -> dict:
    """Элемент справочника: по {"meta"}, по id или по имени."""
    if isinstance(value, dict) and "meta" in value:
        return meta_of(value)
    kind = d["type"]
    text = str(value).strip()
    if kind == "customentity":
        ce_id = entity_id(d["customEntityMeta"]["href"])
        rows = await ms.customentity_elements(ce_id)
        for row in rows:
            if row.get("id") == text or row.get("name", "").strip().lower() == text.lower():
                return meta_of(row)
        raise FieldError(f"в справочнике поля «{d['name']}» нет «{text}». Есть: {_names(rows)}")
    if UUID.match(text):
        return {"meta": {"href": f"{ms.base}/entity/{kind}/{text}", "type": kind,
                         "mediaType": "application/json"}}
    rows = await ms.find(kind, f"name={text}")
    if len(rows) != 1:
        raise FieldError(f"поле «{d['name']}»: {kind} «{text}» "
                         + ("не найден" if not rows else "найден не один — укажите id"))
    return meta_of(rows[0])


async def coerce(ms: MoySklad, d: dict, value: Any) -> Any:
    """Человеческое значение → value для API по типу поля d."""
    kind, name = d.get("type", ""), d.get("name", "")
    if value is None:
        return None
    try:
        if kind in ("string", "text", "link"):
            return str(value)
        if kind == "long":
            return int(str(value).strip().replace(" ", ""))
        if kind == "double":
            return float(str(value).strip().replace(" ", "").replace(",", "."))
        if kind == "boolean":
            if isinstance(value, bool):
                return value
            low = str(value).strip().lower()
            if low in TRUE | FALSE:
                return low in TRUE
            raise FieldError(f"поле «{name}» — флажок, а «{value}» не да/нет")
        if kind == "time":
            return _time(value)
    except ValueError as e:
        if isinstance(e, FieldError):
            raise
        raise FieldError(f"поле «{name}» ({kind}): «{value}» не подходит") from None
    if kind in DICTIONARY:
        return await _dictionary(ms, d, value)
    raise FieldError(f"поле «{name}» типа {kind} через хаб пока не меняется")


async def set_fields(ms: MoySklad, type_: str, id_: str, values: dict[str, Any],
                     expand: str = "") -> dict:
    """Записать доп. поля по именам одним запросом. Возвращает обновлённую сущность."""
    if not values:
        raise FieldError("не передано ни одного поля")
    body = []
    for name, value in values.items():
        d = await find_definition(ms, type_, name)
        body.append({"meta": d["meta"], "value": await coerce(ms, d, value)})
    return await ms.update(type_, id_, {"attributes": body}, expand=expand)
