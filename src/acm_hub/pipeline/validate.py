"""Validation: reject records that would produce a misleading Discord post.

Failures are split into two kinds because they need different operational
handling. A ``permanent`` failure (a listing with no usable apply link) will
never become valid, so retrying it every cycle is wasted work. A ``transient``
failure could resolve on a later run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from acm_hub.domain.opportunity import Opportunity

MAX_TITLE_LENGTH = 300
MAX_ORGANIZATION_LENGTH = 200
ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of validating one opportunity."""

    is_valid: bool
    errors: tuple[str, ...] = ()
    is_permanent: bool = True

    @property
    def reason(self) -> str:
        """Human-readable joined error text, for logs and collector run records."""
        return "; ".join(self.errors)


@dataclass(slots=True)
class ValidationReport:
    """Aggregate outcome for a batch, used for collector run statistics."""

    valid: list[Opportunity] = field(default_factory=list)
    rejected: list[tuple[Opportunity, ValidationResult]] = field(default_factory=list)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


def _url_problem(label: str, url: str) -> str | None:
    if not url.strip():
        return f"{label} is empty"
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_URL_SCHEMES:
        return f"{label} must be http(s), got {parts.scheme or 'no scheme'!r}"
    if not parts.netloc:
        return f"{label} has no host"
    return None


def validate_opportunity(opportunity: Opportunity) -> ValidationResult:
    """Check that an opportunity is safe to store and eventually publish."""
    errors: list[str] = []

    if not opportunity.title.strip():
        errors.append("title is empty")
    elif len(opportunity.title) > MAX_TITLE_LENGTH:
        errors.append(f"title exceeds {MAX_TITLE_LENGTH} characters")

    if not opportunity.organization.strip():
        errors.append("organization is empty")
    elif len(opportunity.organization) > MAX_ORGANIZATION_LENGTH:
        errors.append(f"organization exceeds {MAX_ORGANIZATION_LENGTH} characters")

    for label, url in (
        ("application_url", opportunity.application_url),
        ("source_url", opportunity.source_url),
    ):
        problem = _url_problem(label, url)
        if problem:
            errors.append(problem)

    if not opportunity.canonical_key:
        errors.append("canonical_key is empty")
    if not opportunity.content_hash:
        errors.append("content_hash is empty")

    if (
        opportunity.expires_at is not None
        and opportunity.posted_at is not None
        and opportunity.expires_at < opportunity.posted_at
    ):
        errors.append("expires_at precedes posted_at")

    return ValidationResult(is_valid=not errors, errors=tuple(errors), is_permanent=True)


def validate_batch(opportunities: list[Opportunity]) -> ValidationReport:
    """Validate a batch, partitioning it into accepted and rejected records."""
    report = ValidationReport()
    for opportunity in opportunities:
        result = validate_opportunity(opportunity)
        if result.is_valid:
            report.valid.append(opportunity)
        else:
            report.rejected.append((opportunity, result))
    return report
