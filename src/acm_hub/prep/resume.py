"""Resume PDF text extraction and listing-theme gap checks.

No LLM. The comparison is a plain-text keyword scan against themes ACM Hub
already stores for the opportunity (SIG matches, source category tags, degree
strings). Missing themes mean the resume text did not contain those tokens —
not that an interviewer will reject the candidate.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from pypdf import PdfReader

from acm_hub.bot.insights import focus_tags_for
from acm_hub.db import models

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 40_000
MIN_TOKEN_LENGTH = 3

_NON_ALNUM = re.compile(r"[^a-z0-9+#.\s]+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ResumeGapReport:
    """Result of comparing resume text to a listing's stored themes."""

    themes_checked: tuple[str, ...]
    themes_found: tuple[str, ...]
    themes_missing: tuple[str, ...]
    resume_chars: int
    page_count: int
    notes: tuple[str, ...]

    @property
    def empty_resume(self) -> bool:
        return self.resume_chars == 0


def extract_pdf_text(data: bytes) -> tuple[str, int]:
    """Return ``(text, page_count)`` from a PDF byte string.

    Raises:
        ValueError: when the file is empty, too large, or not a readable PDF.
    """
    if not data:
        raise ValueError("PDF attachment was empty")
    if len(data) > MAX_PDF_BYTES:
        raise ValueError(f"PDF must be under {MAX_PDF_BYTES // (1024 * 1024)} MB")
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ValueError(f"could not read PDF: {exc}") from exc
    pages = list(reader.pages)
    chunks: list[str] = []
    for page in pages:
        try:
            extracted = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            del exc
            extracted = ""
        chunks.append(extracted)
    text = _WHITESPACE.sub(" ", " ".join(chunks)).strip()
    return text[:MAX_TEXT_CHARS], len(pages)


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", _NON_ALNUM.sub(" ", text.lower())).strip()


def _theme_present(theme: str, haystack: str) -> bool:
    """Whether every significant token of ``theme`` appears in the resume text."""
    tokens = [
        token
        for token in _normalize(theme).split()
        if len(token) >= MIN_TOKEN_LENGTH or token in {"c++", "c#", "go", "r", "ai", "ml"}
    ]
    if not tokens:
        return False
    return all(token in haystack for token in tokens)


def listing_themes(row: models.Opportunity) -> tuple[str, ...]:
    """Themes we are allowed to check — only facts already on the opportunity."""
    themes: list[str] = []
    for tag in focus_tags_for(row):
        if tag not in themes:
            themes.append(tag)
    for degree in row.degree_requirements:
        cleaned = degree.strip()
        if cleaned and cleaned not in themes:
            themes.append(cleaned)
    title_bits = [
        part.strip()
        for part in re.split(r"[-|/]", row.title)
        if part.strip() and len(part.strip()) >= 4
    ]
    for bit in title_bits[:3]:
        if bit not in themes and bit.lower() not in {"intern", "internship", "new grad"}:
            themes.append(bit)
    return tuple(themes[:12])


def analyse_resume_against_listing(
    resume_text: str, row: models.Opportunity, *, page_count: int = 0
) -> ResumeGapReport:
    """Find listing themes that do not appear in the resume text."""
    themes = listing_themes(row)
    haystack = _normalize(resume_text)
    notes: list[str] = []
    if not haystack:
        notes.append(
            "No extractable text was found. Scanned image PDFs cannot be read; "
            "export a text PDF and try again."
        )
        return ResumeGapReport(
            themes_checked=themes,
            themes_found=(),
            themes_missing=themes,
            resume_chars=0,
            page_count=page_count,
            notes=tuple(notes),
        )

    found = tuple(theme for theme in themes if _theme_present(theme, haystack))
    missing = tuple(theme for theme in themes if theme not in found)
    notes.append(
        "Missing themes were absent from the resume text. This is not an interview "
        "prediction and is not a full job-description skill list."
    )
    if not themes:
        notes.append("This listing has no stored themes to check against.")
    return ResumeGapReport(
        themes_checked=themes,
        themes_found=found,
        themes_missing=missing,
        resume_chars=len(resume_text),
        page_count=page_count,
        notes=tuple(notes),
    )
