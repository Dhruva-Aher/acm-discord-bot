"""SIG classification, including the ambiguous cases.

Two behaviours matter beyond "does it find the obvious keyword": short tokens
like "ai" and "ml" must not match inside unrelated words, and a genuinely
cross-disciplinary role should surface more than one SIG.
"""

from __future__ import annotations

import pytest

from acm_hub.config.sigs import SIG_NAMES, ClassifierWeights
from acm_hub.pipeline.classify import classify_opportunity, classify_text
from acm_hub.pipeline.normalize import normalize_opportunity
from tests.conftest import FROZEN_NOW, make_raw


def sigs_for(title: str, description: str | None = None, tags: tuple[str, ...] = ()) -> list[str]:
    return [match.sig for match in classify_text(title=title, description=description, tags=tags)]


class TestObviousMatches:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Machine Learning Engineer Intern", "SIGAI"),
            ("Software Engineering Intern", "SIGSOFT"),
            ("High Performance Computing Intern", "SIGHPC"),
            ("Data Science Intern", "SIGKDD"),
            ("Cybersecurity Intern", "SIGSAC"),
            ("Computer Graphics Rendering Intern", "SIGGRAPH"),
            ("UX Research Intern", "SIGCHI"),
            ("Algorithms Research Intern", "SIGACT"),
            ("Accessibility Engineering Intern", "SIGACCESS"),
        ],
    )
    def test_each_sig_is_reachable_from_a_title(self, title: str, expected: str) -> None:
        assert expected in sigs_for(title)

    def test_every_configured_sig_has_working_rules(self) -> None:
        """Guards against a SIG being added to config with unmatchable terms."""
        from acm_hub.config.sigs import SIG_RULES

        for rule in SIG_RULES:
            assert rule.primary_terms, f"{rule.sig} has no primary terms"
            assert rule.sig in sigs_for(f"{rule.primary_terms[0]} Intern"), rule.sig


class TestWordBoundaries:
    @pytest.mark.parametrize(
        "title",
        [
            "Supply Chain Intern",  # contains "ai" inside "chain"
            "HTML Developer Intern",  # contains "ml" inside "html"
            "Retail Operations Intern",  # contains "ai" inside "retail"
        ],
    )
    def test_short_tokens_do_not_match_inside_words(self, title: str) -> None:
        assert "SIGAI" not in sigs_for(title)

    def test_standalone_short_token_still_matches(self) -> None:
        assert "SIGAI" in sigs_for("AI Research Intern")


class TestWeighting:
    def test_a_title_match_outranks_a_description_match(self) -> None:
        title_match = classify_text(title="Machine Learning Intern")
        description_match = classify_text(
            title="Engineering Intern", description="You may touch machine learning systems."
        )
        assert title_match[0].score > description_match[0].score

    def test_description_only_mention_can_fall_below_threshold(self) -> None:
        """A passing mention in prose should not tag the whole opportunity."""
        assert (
            classify_text(
                title="Operations Intern",
                description="Our teams occasionally use machine learning.",
                threshold=0.5,
            )
            == ()
        )

    def test_scores_are_bounded(self) -> None:
        matches = classify_text(
            title="Machine Learning Deep Learning Computer Vision NLP LLM Generative AI Intern",
            description="machine learning deep learning computer vision nlp",
            tags=("machine learning", "artificial intelligence"),
        )
        assert all(0.0 <= match.score <= 1.0 for match in matches)
        assert matches[0].score == 1.0


class TestAmbiguousCases:
    def test_ml_infrastructure_role_matches_both_ai_and_software(self) -> None:
        result = sigs_for(
            "Machine Learning Platform Engineering Intern",
            description="Build backend services and distributed systems for model training.",
        )
        assert "SIGAI" in result
        assert "SIGSOFT" in result

    def test_gpu_ml_role_can_reach_hpc(self) -> None:
        result = sigs_for(
            "CUDA Kernel Optimization Intern",
            description="Optimize GPU kernels for deep learning inference throughput.",
        )
        assert "SIGHPC" in result

    def test_security_data_role_matches_both(self) -> None:
        result = sigs_for(
            "Security Data Science Intern",
            description="Threat detection analytics over large log datasets.",
        )
        assert {"SIGSAC", "SIGKDD"} <= set(result)

    def test_unrelated_role_matches_nothing(self) -> None:
        assert sigs_for("Marketing Communications Intern") == []

    def test_hardware_role_does_not_masquerade_as_software(self) -> None:
        assert sigs_for("PCB Layout Intern") == []


class TestOrderingAndLimits:
    def test_results_are_ordered_by_descending_score(self) -> None:
        matches = classify_text(
            title="Machine Learning Intern",
            description="Some backend work in a distributed systems team.",
        )
        scores = [match.score for match in matches]
        assert scores == sorted(scores, reverse=True)

    def test_max_matches_is_respected(self) -> None:
        matches = classify_text(
            title="Machine Learning Security Graphics Algorithms Accessibility Intern",
            max_matches=2,
        )
        assert len(matches) <= 2

    def test_output_is_deterministic(self) -> None:
        first = classify_text(title="Machine Learning Software Engineering Intern")
        second = classify_text(title="Machine Learning Software Engineering Intern")
        assert first == second

    def test_matched_terms_are_reported_and_deduplicated(self) -> None:
        matches = classify_text(
            title="Machine Learning Intern", description="machine learning and computer vision"
        )
        ai = next(match for match in matches if match.sig == "SIGAI")
        assert "machine learning" in ai.matched_terms
        assert len(ai.matched_terms) == len(set(ai.matched_terms))

    def test_custom_weights_are_honoured(self) -> None:
        weights = ClassifierWeights(title_primary=0.1, saturation=10.0)
        assert classify_text(title="Machine Learning Intern", weights=weights) == ()


class TestClassifyOpportunity:
    def test_attaches_matches_to_the_opportunity(self) -> None:
        opportunity = normalize_opportunity(
            make_raw(title="Machine Learning Intern"), discovered_at=FROZEN_NOW
        )
        classified = classify_opportunity(opportunity)
        assert "SIGAI" in classified.sig_names

    def test_leaves_the_original_untouched(self) -> None:
        opportunity = normalize_opportunity(
            make_raw(title="Machine Learning Intern"), discovered_at=FROZEN_NOW
        )
        classify_opportunity(opportunity)
        assert opportunity.sig_matches == ()

    def test_a_single_generic_tag_is_too_weak_on_its_own(self) -> None:
        """Tags rank below titles and descriptions, so one tag must not decide a SIG."""
        opportunity = normalize_opportunity(
            make_raw(title="Summer Intern", tags=("machine learning",)),
            discovered_at=FROZEN_NOW,
        )
        assert classify_opportunity(opportunity).sig_names == ()

    def test_an_upstream_category_classifies_a_title_free_listing(self) -> None:
        """The Simplify feed has no description, so its category must carry weight.

        ``AI/ML/Data`` expands to several primary terms, which together clear the
        threshold even when the title says nothing useful.
        """
        from acm_hub.collectors.github.simplify import CATEGORY_TAGS

        opportunity = normalize_opportunity(
            make_raw(title="Summer Intern", tags=CATEGORY_TAGS["ai/ml/data"]),
            discovered_at=FROZEN_NOW,
        )
        assert "SIGAI" in classify_opportunity(opportunity).sig_names

    def test_all_sig_names_are_unique(self) -> None:
        assert len(SIG_NAMES) == len(set(SIG_NAMES))
