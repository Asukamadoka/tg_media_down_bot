"""The request table, for the bot's other stores (tgmd reaches WMS only through ``ops``)."""

from ..core.requests import SCHEMA, finish, outcome, post, take

__all__ = ["SCHEMA", "finish", "outcome", "post", "take"]
