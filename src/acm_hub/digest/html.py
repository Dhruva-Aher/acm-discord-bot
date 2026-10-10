"""HTML digest of published opportunities.

Renders the same member-visible facts as Discord embeds. Nothing is invented:
unknown sponsorship stays Unknown, and empty degree lists stay empty.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

from acm_hub.bot.embeds.opportunity import (
    SPONSORSHIP_LABELS,
    format_locations,
    truncate,
)
from acm_hub.bot.insights import build_insights
from acm_hub.db import models
from acm_hub.db.repositories.opportunities import sig_matches_from_json
from acm_hub.domain.enums import OpportunityType

#: Soft cap so a digest stays a usable download. The total published count is
#: still reported when truncated.
DEFAULT_DIGEST_LIMIT = 200

TYPE_LABELS: dict[OpportunityType, str] = {
    OpportunityType.INTERNSHIP: "Internship",
    OpportunityType.RESEARCH: "Research",
    OpportunityType.FELLOWSHIP: "Fellowship",
    OpportunityType.HACKATHON: "Hackathon",
    OpportunityType.SCHOLARSHIP: "Scholarship",
    OpportunityType.CONFERENCE: "Conference",
    OpportunityType.PROGRAM: "Program",
    OpportunityType.OTHER: "Opportunity",
}


@dataclass(frozen=True, slots=True)
class DigestBuildResult:
    """Rendered HTML plus how many published rows it covers."""

    html: str
    included: int
    total_matching: int
    generated_at: datetime

    @property
    def truncated(self) -> bool:
        return self.included < self.total_matching


def _discovered_label(discovered_at: datetime | None, *, now: datetime) -> str:
    if discovered_at is None:
        return "Unknown"
    when = discovered_at if discovered_at.tzinfo else discovered_at.replace(tzinfo=UTC)
    delta_days = (now.date() - when.astimezone(UTC).date()).days
    if delta_days <= 0:
        return "Today"
    if delta_days == 1:
        return "Yesterday"
    return f"{delta_days} days ago"


def _sig_text(row: models.Opportunity) -> str:
    matches = sig_matches_from_json(row.sig_matches)
    if not matches:
        return "None matched"
    return " · ".join(match.sig for match in matches)


def _eligibility_lines(row: models.Opportunity) -> list[str]:
    lines = [f"Sponsorship: {SPONSORSHIP_LABELS[row.sponsorship]}"]
    if row.citizenship_requirement:
        lines.append(f"Citizenship: {row.citizenship_requirement}")
    if row.degree_requirements:
        lines.append(f"Degrees: {', '.join(row.degree_requirements)}")
    return lines


def _render_row(
    row: models.Opportunity,
    *,
    source_display_names: Mapping[str, str],
    now: datetime,
) -> str:
    title = escape(truncate(row.title, 200))
    url = escape(row.application_url, quote=True)
    organization = escape(truncate(row.organization, 120))
    location = escape(format_locations(list(row.locations), row.remote_status))
    opportunity_type = TYPE_LABELS.get(row.opportunity_type, TYPE_LABELS[OpportunityType.OTHER])
    sigs = escape(_sig_text(row))
    source = escape(source_display_names.get(row.source_name, row.source_name))
    discovered = escape(_discovered_label(row.discovered_at, now=now))
    eligibility = "".join(f"<li>{escape(line)}</li>" for line in _eligibility_lines(row))
    description = ""
    if row.description:
        description = f'<p class="description">{escape(truncate(row.description, 400))}</p>'

    insights = build_insights(row, now=now)
    fresh = (
        f'<p class="fresh">{escape(insights.freshness)}</p>' if insights.freshness else ""
    )
    focus = ""
    if insights.focus_tags:
        pills = " ".join(
            f'<span class="pill">{escape(tag)}</span>' for tag in insights.focus_tags
        )
        focus = f'<p class="focus"><span class="focus-label">Focus</span> {pills}</p>'
    research = ""
    if insights.research_links:
        items = "".join(
            f'<li><a href="{escape(link.url, quote=True)}" rel="noopener noreferrer">'
            f"{escape(link.label)}</a></li>"
            for link in insights.research_links[:3]
        )
        research = (
            f'<aside class="insights"><p class="insights-title">Stand out</p>'
            f"<ul>{items}</ul>"
            f'<p class="insights-note">{escape(insights.disclaimer)}</p></aside>'
        )

    return f"""
<article class="opportunity">
  <h2><a href="{url}" rel="noopener noreferrer">{title}</a></h2>
  <p class="meta"><span class="type">{escape(opportunity_type)}</span>
    · <strong>{organization}</strong> · {location}</p>
  {fresh}
  {focus}
  {description}
  <ul class="facts">
    <li>Relevant SIGs: {sigs}</li>
    {eligibility}
    <li>Source: {source}</li>
    <li>Discovered: {discovered}</li>
  </ul>
  {research}
</article>
"""


def build_opportunities_html(
    rows: Sequence[models.Opportunity],
    *,
    total_matching: int,
    source_display_names: Mapping[str, str] | None = None,
    generated_at: datetime | None = None,
    title: str = "ACM Hub opportunities digest",
    filter_summary: str | None = None,
) -> DigestBuildResult:
    """Build a self-contained HTML document from published opportunity rows."""
    now = generated_at or datetime.now(tz=UTC)
    names = source_display_names or {}
    articles = [
        _render_row(row, source_display_names=names, now=now) for row in rows
    ]
    body = (
        "\n".join(articles)
        if articles
        else '<p class="empty">No published opportunities match.</p>'
    )
    truncated_note = ""
    if rows and len(rows) < total_matching:
        truncated_note = (
            f"<p class=\"note\">Showing the {len(rows)} most recently discovered of "
            f"{total_matching} matching published opportunities.</p>"
        )
    filter_note = (
        f"<p class=\"note\">Filters: {escape(filter_summary)}</p>" if filter_summary else ""
    )
    generated = escape(now.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(title)}</title>
  <style>
    :root {{
      --maroon: #8C1D40;
      --gold: #FFC627;
      --ink: #1a1a1a;
      --muted: #5c5c5c;
      --paper: #f7f4ef;
      --card: #ffffff;
      --line: #e4ddd2;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(140, 29, 64, 0.08), transparent 40%),
        linear-gradient(180deg, #fffdf8 0%, var(--paper) 100%);
      line-height: 1.45;
    }}
    header {{
      padding: 2.5rem 1.5rem 1.5rem;
      border-bottom: 4px solid var(--gold);
      background: linear-gradient(135deg, var(--maroon), #5e102c);
      color: #fff;
    }}
    header h1 {{
      margin: 0 0 0.4rem;
      font-size: clamp(1.8rem, 4vw, 2.6rem);
      letter-spacing: 0.01em;
    }}
    header p {{ margin: 0.25rem 0; color: rgba(255,255,255,0.9); }}
    main {{
      max-width: 52rem;
      margin: 0 auto;
      padding: 1.5rem;
    }}
    .note, .empty {{ color: var(--muted); }}
    .opportunity {{
      background: var(--card);
      border: 1px solid var(--line);
      border-left: 5px solid var(--gold);
      padding: 1.1rem 1.2rem;
      margin: 0 0 1rem;
    }}
    .opportunity h2 {{
      margin: 0 0 0.35rem;
      font-size: 1.2rem;
    }}
    .opportunity a {{ color: var(--maroon); text-decoration: none; }}
    .opportunity a:hover {{ text-decoration: underline; }}
    .meta {{ margin: 0 0 0.6rem; color: var(--muted); }}
    .type {{
      display: inline-block;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      font-size: 0.75rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--maroon);
      font-weight: 700;
    }}
    .description {{ margin: 0 0 0.7rem; }}
    .fresh {{
      margin: 0 0 0.6rem;
      color: var(--maroon);
      font-weight: 700;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      font-size: 0.9rem;
    }}
    .focus {{ margin: 0 0 0.7rem; }}
    .focus-label {{
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      font-size: 0.75rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--muted);
      margin-right: 0.35rem;
    }}
    .pill {{
      display: inline-block;
      margin: 0.15rem 0.25rem 0.15rem 0;
      padding: 0.15rem 0.45rem;
      border: 1px solid var(--line);
      background: #fffdf8;
      font-size: 0.85rem;
    }}
    .facts {{
      margin: 0;
      padding-left: 1.1rem;
      color: var(--ink);
    }}
    .insights {{
      margin: 0.9rem 0 0;
      padding: 0.85rem 1rem;
      border-top: 1px dashed var(--line);
      background: linear-gradient(90deg, rgba(140, 29, 64, 0.04), transparent);
    }}
    .insights-title {{
      margin: 0 0 0.4rem;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      font-size: 0.8rem;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      color: var(--maroon);
      font-weight: 700;
    }}
    .insights ul {{ margin: 0.2rem 0 0.5rem; padding-left: 1.1rem; }}
    .insights-note {{ margin: 0; font-size: 0.85rem; color: var(--muted); }}
    footer {{
      max-width: 52rem;
      margin: 0 auto;
      padding: 0 1.5rem 2.5rem;
      color: var(--muted);
      font-size: 0.9rem;
    }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(title)}</h1>
    <p>Published opportunities for ACM at Arizona State University</p>
    <p>Generated {generated} · {len(rows)} listed
      {"· truncated" if rows and len(rows) < total_matching else ""}</p>
  </header>
  <main>
    {filter_note}
    {truncated_note}
    {body}
  </main>
  <footer>
    <p>Details come from upstream sources. ACM Hub does not invent sponsorship,
    deadlines, salary, or eligibility. Where a source is silent, the digest says Unknown.</p>
  </footer>
</body>
</html>
"""
    return DigestBuildResult(
        html=html,
        included=len(rows),
        total_matching=total_matching,
        generated_at=now,
    )
