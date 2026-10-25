"""Статус заказа идёт следом за отгрузкой.

Отгрузка пришла в статус `state` → связанному заказу покупателя ставится
`order_state`. Если задан `only_from`, заказ меняется только из этих статусов —
чтобы не перетирать то, что менеджер поставил руками («Отменён», «Возврат»).
"""
from __future__ import annotations

from ..entities import Shipment
from ..events import Event
from ..moysklad import fields as ms_fields
from . import Automation, ConfigError, Context, Skip, register


@register
class SyncOrderState(Automation):
    kind = "sync_order_state"
    title = "Статус заказа по отгрузке"
    entities = ("demand",)
    defaults = {"entity": "demand", "state": "Отгружен", "order_state": "Отгружен",
                "only_from": []}

    def validate(self) -> None:
        if not (self.state and self.order_state):
            raise ConfigError(f"{self.name}: нужны state и order_state")

    def wants(self, obj: Shipment, event: Event) -> bool:
        return bool(obj.order_id) and super().wants(obj, event)

    def key(self, obj: Shipment, event: Event | None) -> str:
        return f"{obj.id}:{obj.state}"

    async def run(self, obj: Shipment, ctx: Context) -> str:
        order = await ctx.ms.entity("customerorder", obj.order_id, "state")
        current = (order.get("state") or {}).get("name", "")
        if current.lower() == self.order_state.lower():
            raise Skip(f"заказ {order.get('name')} уже «{current}»")
        allowed = [s.lower() for s in self.only_from]
        if allowed and current.lower() not in allowed:
            raise Skip(f"заказ {order.get('name')} в «{current}» — не трогаю")
        await ms_fields.find_state(ctx.ms, "customerorder", self.order_state)
        result = await ctx.set_state("customerorder", obj.order_id, self.order_state)
        return f"заказ {order.get('name')}: «{current}» → {result}"
