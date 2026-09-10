"""Deterministic SIG classification.

No model and no API call: a weighted keyword match over the title, description
and tags. The same input always produces the same SIGs and the same scores,
which is what makes the behaviour testable and lets an officer reason about why
something was tagged.

Weighting reflects where a term appears. "Machine Learning Intern" in a title is
a strong signal; the phrase "machine learning" once inside a long description is
a weak one; an upstream category tag sits in between.
"""

from __future__ import annotations

import re
from functools import lru_cache

from acm_hub.config.sigs import SIG_RULES, ClassifierWeights, SigRule
from acm_hub.domain.opportunity import Opportunity, SigMatch

DEFAULT_THRESHOLD = 0.25
DEFAULT_MAX_MATCHES = 3


@lru_cache(maxsize=2048)
def _term_pattern(term: str) -> re.Pattern[str]:
    """Compile a word-boundary matcher for ``term``.

    Boundaries matter for short terms: without them "ai" matches "chain" and
    "ml" matches "html". ``\\b`` is only applied where the adjacent character is
    alphanumeric, so terms such as "ci/cd" and "c++"-style tokens still match.
    """
    escaped = re.escape(term)
    prefix = r"\b" if term[:1].isalnum() else ""
    suffix = r"\b" if term[-1:].isalnum() else ""
    return re.compile(f"{prefix}{escaped}{suffix}", re.IGNORECASE)


def _scan(text: str, terms: tuple[str, ...]) -> list[str]:
    """Return the terms present in ``text``, each at most once."""
    if not text:
        return []
    return [term for term in terms if _term_pattern(term).search(text)]


def _score_rule(
    rule: SigRule,
    *,
    title: str,
    description: str,
    tag_text: str,
    weights: ClassifierWeights,
) -> tuple[float, tuple[str, ...]]:
    """Accumulate the weighted score and the distinct matched terms for one SIG."""
    score = 0.0
    matched: dict[str, None] = {}

    for field_text, primary_weight, secondary_weight in (
        (title, weights.title_primary, weights.title_secondary),
        (description, weights.description_primary, weights.description_secondary),
        (tag_text, weights.tag_primary, weights.tag_secondary),
    ):
        for term in _scan(field_text, rule.primary_terms):
            score += primary_weight
            matched[term] = None
        for term in _scan(field_text, rule.secondary_terms):
            score += secondary_weight
            matched[term] = None

    return score, tuple(matched)


def classify_text(
    *,
    title: str,
    description: str | None = None,
    tags: tuple[str, ...] = (),
    weights: ClassifierWeights | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    max_matches: int = DEFAULT_MAX_MATCHES,
    rules: tuple[SigRule, ...] = SIG_RULES,
) -> tuple[SigMatch, ...]:
    """Classify free text into SIGs, highest confidence first.

    Scores are normalized against :attr:`ClassifierWeights.saturation` and
    clamped to ``1.0``. SIGs scoring below ``threshold`` are dropped, and at most
    ``max_matches`` are returned so an embed cannot list every SIG at once.
    Ties break alphabetically to keep output stable across runs.
    """
    effective_weights = weights or ClassifierWeights()
    tag_text = " ".join(tags)

    matches: list[SigMatch] = []
    for rule in rules:
        raw_score, matched_terms = _score_rule(
            rule,
            title=title,
            description=description or "",
            tag_text=tag_text,
            weights=effective_weights,
        )
        if not matched_terms:
            continue
        normalized = min(raw_score / effective_weights.saturation, 1.0)
        if normalized < threshold:
            continue
        matches.append(
            SigMatch(sig=rule.sig, score=round(normalized, 4), matched_terms=matched_terms)
        )

    matches.sort(key=lambda match: (-match.score, match.sig))
    return tuple(matches[:max_matches])


def classify_opportunity(
    opportunity: Opportunity,
    *,
    weights: ClassifierWeights | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    max_matches: int = DEFAULT_MAX_MATCHES,
) -> Opportunity:
    """Return a copy of ``opportunity`` with ``sig_matches`` populated."""
    matches = classify_text(
        title=opportunity.title,
        description=opportunity.description,
        tags=opportunity.tags,
        weights=weights,
        threshold=threshold,
        max_matches=max_matches,
    )
    return opportunity.model_copy(update={"sig_matches": matches})
