"""Repositories: the only place that knows how domain objects map to tables."""

from acm_hub.db.repositories.collector_runs import CollectorRunRepository
from acm_hub.db.repositories.hides import MemberHideRepository
from acm_hub.db.repositories.opportunities import OpportunityRepository
from acm_hub.db.repositories.sources import SourceRepository
from acm_hub.db.repositories.subscriptions import SubscriptionRepository

__all__ = [
    "CollectorRunRepository",
    "MemberHideRepository",
    "OpportunityRepository",
    "SourceRepository",
    "SubscriptionRepository",
]
