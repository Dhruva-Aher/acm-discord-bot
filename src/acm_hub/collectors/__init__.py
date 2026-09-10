"""Collectors: adapters that read external sources and return raw records.

A collector never touches Discord, never writes to the database and never
decides where something is published. It answers exactly one question: "what
does this source currently say?"
"""

from acm_hub.collectors.base import CollectionOutcome, Collector, CollectorError, safe_collect

__all__ = ["CollectionOutcome", "Collector", "CollectorError", "safe_collect"]
