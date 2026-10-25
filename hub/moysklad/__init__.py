"""Клиент МойСклад JSON API 1.2 и работа со статусами и доп. полями по именам."""
from .client import BASE, MoySklad, MoySkladError, NotFound, entity_id, meta_of
from .fields import FieldError

__all__ = ["BASE", "FieldError", "MoySklad", "MoySkladError", "NotFound", "entity_id", "meta_of"]
