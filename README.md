# ACM Hub

A Discord bot for **ACM at Arizona State University** that turns the chapter's
server into a technical member hub by automatically discovering, classifying and
publishing opportunities.

ACM Hub is **not** a moderation bot and **not** a general-purpose AI assistant.
The server's Sapphire bot already handles moderation, verification, role
assignment and general server management, and ACM Hub deliberately duplicates
none of that. It requests no moderation permissions and no privileged intents.

## What it does

Right now (Milestones 1–7):

- Collects open internship listings from the
  [SimplifyJobs / Pitt CSC](https://github.com/SimplifyJobs/Summer2027-Internships)
  community list.
- Collects ASU faculty who are **explicitly labelled Ready to mentor** on the
  official [Fulton Forge mentors page](https://forge.engineering.asu.edu/mentors/).
  Expo-only mentors, lab pages, grants and publication records are not treated
  as recruiting evidence.
- Collects upcoming conferences from ACM's official
  [conferences page](https://www.acm.org/conferences) when that page is
  reachable. `robots.txt` forbids `/calendar`; Cloudflare blocks are reported
  as a failed source, never bypassed, and never replaced with a made-up list.
- Normalizes them into one canonical model, deduplicates them across collection
  runs and across sources, and detects when a posting has actually changed.
- Classifies each opportunity into ACM Special Interest Groups with a
  deterministic weighted keyword classifier — no LLM, no API call, no cost, and
  the same input always yields the same result.
- Treats a source's first successful run as **baseline backlog**, so enabling
  a source does not dump thousands of review cards on officers. Closures are
  inferred only from a complete, plausible snapshot, never from a failed or
  truncated feed, and published messages are edited in place to `[CLOSED]`.
- Posts each new (non-baseline) opportunity to a **private officer review
  channel** with Approve / Reject / Open Source buttons, unless an officer has
  explicitly set that source to auto-publish. Nothing reaches members until
  then.
- Lets members browse with `/acm opportunities`, `/acm research` and
  `/acm conferences`, download a self-contained HTML digest with `/acm digest`,
  save interests with `/acm subscribe`, and page through results that only they
  can control.

### A rule worth stating up front

**ACM Hub never invents facts about an opportunity.** No salary, no deadline, no
visa or sponsorship claim, no location, no requirement ever appears in a Discord
post unless the source explicitly stated it. Where a source is silent, the embed
says `Unknown`. Missing eligibility information is never treated as eligibility.

## Architecture

Each stage is independently testable, and the boundaries are enforced: a
collector cannot post to Discord, and the pipeline cannot decide a channel.

```
                 external sources
                        │
        ┌───────────────┴───────────────┐
        │           COLLECTORS          │   fetch + source-specific parsing only
        │  github/simplify              │   never touches Discord or the database
        │  asu/ready_to_mentor          │
        │  acm/conferences              │
        └───────────────┬───────────────┘
                        │  RawOpportunity[]
        ┌───────────────▼───────────────┐
        │  NORMALIZE   pipeline/normalize.py   canonical_key + content_hash
        │  VALIDATE    pipeline/validate.py    reject unpublishable records
        │  DEDUPLICATE pipeline/deduplicate.py collapse + detect real changes
        │  CLASSIFY    pipeline/classify.py    weighted SIG keyword matching
        └───────────────┬───────────────┘
                        │  Opportunity
        ┌───────────────▼───────────────┐
        │   PERSIST      db/repositories │   unique canonical_key
        └───────────────┬───────────────┘
                        │  status: discovered
        ┌───────────────▼───────────────┐
        │   APPROVAL   publishing/approval.py   officer buttons, idempotent
        └───────────────┬───────────────┘
                        │  status: approved
        ┌───────────────▼───────────────┐
        │   PUBLISH    publishing/discord.py    unique (opportunity, channel)
        └───────────────┬───────────────┘
                        ▼
                #opportunities
```

An opportunity moves through exactly these states:

```
discovered ──▶ pending_review ──▶ approved ──▶ published ──▶ closed
     ▲               │                              │
     │               └──────────▶ rejected          │
     │                     │                        │
     └──── reconsider ─────┘                        │
           (audit row kept)     source no longer lists it
```

A source's first successful run stores rows as `discovered` with
`is_baseline=true`, which holds them out of the review queue until an officer
releases them. `closed` keeps the row and its publication records; the members'
message is edited, never deleted.

### Layout

| Path | Responsibility |
| --- | --- |
| `src/acm_hub/domain/` | Canonical `Opportunity`, `RawOpportunity`, enums |
| `src/acm_hub/collectors/` | One adapter per external source, behind `Collector` |
| `src/acm_hub/pipeline/` | Normalize, validate, deduplicate, classify, orchestrate |
| `src/acm_hub/db/` | SQLAlchemy models, async session, repositories |
| `src/acm_hub/publishing/` | The only code that sends messages to Discord |
| `src/acm_hub/digest/` | HTML digest of published opportunities (same facts as embeds) |
| `src/acm_hub/bot/` | Client, slash commands, embeds, permission checks |
| `src/acm_hub/scheduler/` | APScheduler jobs with overlap protection |
| `src/acm_hub/config/` | Settings and SIG classification rules |

## Quick start with Docker

The fastest path to a running bot. You need Docker, and a Discord application
(see [Discord setup](#discord-developer-portal-setup)).

```bash
cp .env.example .env
# Fill in DISCORD_TOKEN, DISCORD_GUILD_ID, OPPORTUNITIES_CHANNEL_ID
# and OFFICER_REVIEW_CHANNEL_ID. Leave DATABASE_URL as-is: compose overrides it.

docker compose up --build
```

That starts PostgreSQL, waits for it to be healthy, runs `alembic upgrade head`
as a one-shot job, then starts the bot. Logs are JSON in this mode
(`LOG_FORMAT=text`).

```bash
docker compose logs -f bot     # follow the bot
docker compose down            # stop, keeping the database volume
docker compose down -v         # stop and delete the database
```

## Local development setup

Requires Python 3.12+ and a PostgreSQL 14+ instance.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env           # then fill it in

createdb acm_hub               # or use the compose database on port 55432
alembic upgrade head

python -m acm_hub.main
```

If configuration is missing or malformed the process exits with code `2` and
prints exactly which variables are wrong — it never starts half-configured.

### Using only the compose database

Handy if you want the bot running on your host but do not want to install
PostgreSQL:

```bash
docker compose up -d db
export DATABASE_URL=postgresql+asyncpg://acm_hub:acm_hub@localhost:55432/acm_hub
alembic upgrade head
python -m acm_hub.main
```

## Discord Developer Portal setup

1. Go to <https://discord.com/developers/applications> and click **New
   Application**. Name it `ACM Hub`.
2. Open the **Bot** tab, then **Reset Token** and copy the token into
   `DISCORD_TOKEN` in your `.env`. Treat it like a password: it is never
   committed, and `.env` is gitignored.
3. Still on the **Bot** tab, leave **all three Privileged Gateway Intents
   off**. ACM Hub does not use Message Content, Server Members or Presence.
4. Open **OAuth2 → URL Generator**:
   - Scopes: `bot` and `applications.commands`
   - Bot Permissions: the four listed below
5. Copy the generated URL, open it, and add the bot to the ACM server. You need
   **Manage Server** on that server to complete this step.

### Required bot permissions

ACM Hub needs exactly these, and only in the two channels it uses:

| Permission | Why |
| --- | --- |
| View Channel | Resolve the configured channels |
| Send Messages | Post opportunities and officer review cards |
| Embed Links | Opportunities are rendered as embeds |
| Read Message History | Edit its own review cards after a decision |

It does **not** need, and should not be granted: Administrator, Manage Server,
Manage Roles, Manage Channels, Kick Members, Ban Members, Manage Messages, or
Mention Everyone. Sapphire owns everything in that category.

The cleanest setup is to grant no server-wide permissions at all and instead add
the bot's role to the two channels with the four permissions above.

## How to add ACM Hub to an existing Discord server

For an officer adding this to the live ACM at ASU server:

1. **Create the review channel.** Make a private text channel — for example
   `#acm-hub-review` — visible only to the officer role. This is where new
   opportunities land for approval.
2. **Pick the public channel.** Use the existing `#opportunities` channel. ACM
   Hub will not create, rename or reorganize anything.
3. **Turn on Developer Mode** so you can copy IDs: User Settings → Advanced →
   Developer Mode.
4. **Copy the IDs.** Right-click the server name → *Copy Server ID*
   (`DISCORD_GUILD_ID`). Right-click `#opportunities` → *Copy Channel ID*
   (`OPPORTUNITIES_CHANNEL_ID`). Right-click your review channel → *Copy Channel
   ID* (`OFFICER_REVIEW_CHANNEL_ID`).
5. **Optionally set `OFFICER_ROLE_ID`.** Server Settings → Roles → right-click
   your officer role → *Copy Role ID*. Without it, ACM Hub falls back to
   requiring the Discord **Manage Server** permission, which is usually broader
   than you want.
6. **Invite the bot** using the OAuth2 URL above, then give its role access to
   both channels.
7. **Deploy** with the `.env` filled in. On startup the bot syncs its slash
   commands to your guild only, so they appear within seconds.
8. **Verify** by running `/acm-admin health`, then `/acm-admin sync` to trigger a
   collection immediately rather than waiting for the schedule.

No channel per SIG is required. SIGs are data on the opportunity, shown as tags
in the embed, which keeps the door open for forum-channel routing later without
a migration.

## Environment variables

Required:

| Variable | Description |
| --- | --- |
| `DISCORD_TOKEN` | Bot token. Environment only, never committed |
| `DISCORD_GUILD_ID` | Server ID; commands are synced to this guild |
| `OPPORTUNITIES_CHANNEL_ID` | Public channel for approved opportunities |
| `OFFICER_REVIEW_CHANNEL_ID` | Private channel for the approval queue |
| `DATABASE_URL` | Must use an async driver (`postgresql+asyncpg://…`) |

Optional:

| Variable | Default | Description |
| --- | --- | --- |
| `OFFICER_ROLE_ID` | unset | Role allowed to use `/acm-admin`. Falls back to Manage Server |
| `RESEARCH_CHANNEL_ID` | unset | If set, approved research posts here instead of the opportunities channel |
| `GITHUB_TOKEN` | unset | A no-scope token raises GitHub rate limits for the internship feed |
| `ACM_CONFERENCES_ENABLED` | `true` | Set `false` when acm.org blocks automated fetches (HTTP 403) |
| `ACM_CONFERENCES_URL` | unset | Override the official ACM conferences page URL |
| `LOG_LEVEL` | `INFO` | Standard Python level name |
| `LOG_FORMAT` | `console` | `console` for humans, `text` for JSON lines |
| `SCHEDULER_ENABLED` | `true` | Set `false` to disable background collection |
| `INTERNSHIP_COLLECTION_INTERVAL_MINUTES` | `180` | Minimum 5 |
| `REVIEW_QUEUE_BATCH_LIMIT` | `10` | Caps review cards posted per cycle |
| `PUBLISH_BATCH_LIMIT` | `10` | Caps auto-approved posts per cycle |
| `CLOSURE_DETECTION_ENABLED` | `true` | Infer closures from absence in a complete snapshot |
| `CLOSURE_MIN_VOLUME_RATIO` | `0.5` | Skip closure if this run fetched less than this fraction of the last good run |
| `CLOSURE_MAX_CLOSE_RATIO` | `0.3` | Skip closure if it would close more than this fraction of open rows |
| `SIG_SCORE_THRESHOLD` | `0.25` | Minimum classifier score to tag a SIG |
| `SIG_MAX_MATCHES` | `3` | Maximum SIGs shown on one opportunity |
| `HTTP_TIMEOUT_SECONDS` | `30.0` | Per-request timeout for collectors |
| `HTTP_MAX_RETRIES` | `3` | Retries for transient upstream failures |

`REVIEW_QUEUE_BATCH_LIMIT` matters on first run: the upstream feed carries
thousands of open listings, and without a cap the officer channel would be
flooded.

## Database

PostgreSQL, accessed exclusively through async SQLAlchemy so no query can block
the Discord gateway heartbeat.

| Table | Contents |
| --- | --- |
| `opportunities` | The canonical record. `canonical_key` is **unique**. `is_baseline` / `closed_at` track import and closure. |
| `opportunity_sigs` | Queryable projection of SIG matches. Unique on `(opportunity_id, sig)` |
| `sources` | Collectors, `publication_policy`, and `initialized_at` (no hardcoded cutover date) |
| `publication_records` | One row per publish. **Unique** on `(opportunity_id, channel_id)` |
| `review_actions` | Officer decisions: reviewer ID, decision, reason, timestamp |
| `subscriptions` | Member preferences by Discord user ID |
| `collector_runs` | Per-run counts, duration, errors, and whether the run was a complete snapshot |

Both duplicate-prevention guarantees are enforced by database constraints, not
just by application code, so a race between two officers or two scheduler runs
cannot produce a duplicate post.

### Migrations

```bash
alembic upgrade head                              # apply
alembic revision --autogenerate -m "description"  # create after a model change
alembic downgrade -1                              # roll back one revision
```

`alembic/env.py` reads `DATABASE_URL` from the environment, so migrations and
the bot can never target different databases, and no connection string is
committed. A test (`tests/integration/test_migrations.py`) diffs the migrated
schema against the ORM metadata and fails on drift, so a model change without a
migration is caught in CI.

## Commands

Member commands:

| Command | Description |
| --- | --- |
| `/acm status` | What ACM Hub is tracking and when each source last ran |
| `/acm opportunities` | Browse published opportunities. Filters and paging are SQL. Saved interests apply when you pass no filters; `all:true` ignores them |
| `/acm research` | Browse published Ready-to-mentor faculty only. Saved SIGs apply; saved types do not |
| `/acm conferences` | Browse published official ACM conferences only |
| `/acm digest` | Download an HTML digest of published opportunities. Same filters and saved-interest behaviour as `/acm opportunities`; only published rows; capped at 200 |
| `/acm subscribe` | Save type and SIG interests. ACM Hub never sends DMs |

Officer commands (hidden from members, and re-checked at runtime):

| Command | Description |
| --- | --- |
| `/acm-admin queue` | Review queue, filterable by type / SIG / organization / newest-first |
| `/acm-admin approve <id>` | Approve and publish by ID |
| `/acm-admin bulk-approve` | Preview a batch, confirm, then publish; partial failures are itemised |
| `/acm-admin reject <id> [reason]` | Reject by ID |
| `/acm-admin rejected` | Recently rejected opportunities |
| `/acm-admin reconsider <id>` | Put a rejected opportunity back in the queue without touching the database |
| `/acm-admin sync` | Run every collector now |
| `/acm-admin health` | Database, scheduler, sources, counts. No tokens, URLs or traces |
| `/acm-admin requeue` | Re-post review cards after a channel outage |
| `/acm-admin source list` | Every source, its policy, and how much baseline it is holding |
| `/acm-admin source policy` | `manual_review` / `auto_publish` / `disabled` — data, not a name check |
| `/acm-admin source release-baseline` | Release a bounded slice of backlog into the queue, with confirmation |
| `/acm-admin source reset-initialization` | Next run is an initial import again |

All commands are Discord application (slash) commands. There are deliberately no
prefix commands such as `!opportunities`, and a test asserts none exist.

## Officer approval workflow

1. A collection cycle discovers an opportunity and stores it as `discovered`.
2. The scheduler posts a review card to the officer channel and the opportunity
   becomes `pending_review`. The card shows the same facts members would see,
   plus the classifier's SIG scores and matched terms, and how much ACM Hub
   actually verified.
3. An officer presses a button:
   - **Approve** → recorded, published to `#opportunities`, marked `published`.
   - **Reject** → a modal asks for an optional reason; the opportunity is marked
     `rejected` and is **never re-queued**, even if the upstream posting changes.
   - **Open Source** → a plain link to the upstream source.
4. The card is stamped with the decision and its buttons are removed.
5. `/acm-admin reconsider <id>` is the supported way to undo a rejection. The
   original `review_actions` row stays; the opportunity goes back to
   `discovered` so a new card can be posted.

**Bulk approve.** `/acm-admin bulk-approve` always previews and requires a
second press. The batch size is capped. Each approval is an individual audit
row; a rate-limit on the fifth post does not hide the four that succeeded.

**Source policy.** New sources are `manual_review`. Auto-publish is never the
default and is never inferred from the source's name. An initial import is
never auto-published even if the policy is already `auto_publish`.

**Idempotency.** Two officers pressing Approve at the same instant produce one
decision and one post. The decision is a conditional `UPDATE` restricted to
undecided rows, so exactly one caller wins; the publish is then guarded again by
a unique constraint. The second officer gets an ephemeral "already reviewed"
message. The buttons also survive a restart: their handlers are rebuilt from the
opportunity ID embedded in each button's `custom_id`.

If publishing fails (rate limit, channel deleted), the opportunity stays
`approved` with no publication record, and `/acm-admin approve <id>` retries it.

## Testing

```bash
pytest                       # SQLite suite (default; no secrets, no network)
pytest tests/unit            # fast, no database
pytest --cov=acm_hub         # with coverage
pytest -m postgres           # PostgreSQL concurrency + migration tests
ruff check . && ruff format --check . && mypy
```

PostgreSQL tests are collected only when `ACMHUB_POSTGRES_URL` is set to a
`postgresql+asyncpg://` URL pointing at an empty-able database. CI runs them
against a Postgres 16 service container. Locally:

```bash
export ACMHUB_POSTGRES_URL=postgresql+asyncpg://acm_hub:acm_hub@localhost:5432/acm_hub_test
pytest -m postgres
```

The SQLite suite never touches the network and never connects to Discord: HTTP
is mocked with `respx` against a fixture captured from the real upstream feed.
CI therefore needs no secrets for that job.

Live Discord behaviour is **not** claimed by CI. Work through
[`docs/LIVE_DISCORD_CHECKLIST.md`](docs/LIVE_DISCORD_CHECKLIST.md) on a real
guild; do not tick an item you did not actually run.

Coverage focuses on the things that would actually hurt in production: URL
normalization and canonical keys, content-change detection, SIG classification
including ambiguous and word-boundary cases, malformed and wholly-changed
upstream data, baseline import, closure guards, source trust policy, approval
and publication idempotency under concurrency (PostgreSQL), member search
paging in SQL, subscription independence, configuration validation, and
migration/model parity.

## Adding a collector

1. Create a class in `src/acm_hub/collectors/` that subclasses `Collector` and
   implements `async def collect(self) -> list[RawOpportunity]`.
2. Set `name` (stable and permanent — it is persisted on every record it
   produces), `display_name` and `source_url`. Set
   `produces_complete_snapshot = True` only if a successful `collect()` returns
   every currently-open listing the source knows about. Leave it `False`
   (the default) for paginated or incremental sources — otherwise ACM Hub
   would treat "not on this page" as "closed".
3. Keep every source-specific quirk inside the class. Parse defensively: skip
   and count individual bad records, and raise `CollectorError` only when the
   source as a whole is unusable.
4. Populate only what the source actually states. Leave unknown fields at their
   defaults; do not infer eligibility, remote status or dates.
5. Save a small, unmodified fixture under `tests/fixtures/` and test parsing,
   malformed input and transport failure against it.
6. Register it in `AcmHubBot.collectors`.

A collector must never import from `acm_hub.publishing` or `acm_hub.bot`.

## Adding a SIG rule

Edit `src/acm_hub/config/sigs.py`. Add terms to an existing `SigRule`, or append
a new one:

```python
SigRule(
    sig="SIGMOBILE",
    display_name="Mobility of Systems, Users, Data and Computing",
    primary_terms=("mobile computing", "wireless networks"),
    secondary_terms=("android", "ios", "5g"),
)
```

`primary_terms` are strong signals; `secondary_terms` support a match but rarely
carry it alone. Matching is case-insensitive on word boundaries, so short tokens
like `ai` will not match inside `chain`. Weights and the score threshold live in
`ClassifierWeights` and `SIG_SCORE_THRESHOLD`; a title match always outranks a
description match, which outranks a tag.

Add a case to `tests/unit/test_classify.py` — an existing test asserts every
configured SIG is actually reachable from its own first primary term.

## Deployment considerations

- **Single instance.** Two instances would both run the scheduler and both react
  to button presses. The database constraints prevent duplicate *posts*, but
  duplicate work and confusing ephemeral replies are still avoidable — run one.
- **Graceful shutdown.** On `SIGTERM` the bot stops the scheduler (and waits for
  APScheduler's deferred teardown to actually complete), closes the gateway,
  closes collector HTTP clients, then disposes the database engine, in that
  order. Give it at least 30 seconds; compose already does.
- **Restart policy.** Configuration errors exit `2` deliberately, so use a
  policy that surfaces repeated failures rather than looping silently.
- **Backups.** `review_actions` is the audit trail of officer decisions and
  `opportunities` holds the rejection history that prevents re-queueing. Losing
  the database means every previously rejected opportunity returns.
- **Rate limits.** discord.py handles Discord's limits. The internship collector
  reads `listings.json` via the GitHub REST API first, then falls back to the
  raw host. Set `GITHUB_TOKEN` (no scopes needed) if either endpoint returns
  HTTP 403 or 429 from a shared IP.
- **Logs.** Set `LOG_FORMAT=text` for JSON lines. Tokens are `SecretStr` and
  render masked even if a settings object is logged by mistake.

## Troubleshooting

**Slash commands do not appear.** Check `DISCORD_GUILD_ID` matches the server,
and that the bot was invited with the `applications.commands` scope. Commands
are synced per-guild at startup, so they should appear within seconds — look for
`bot.commands_synced` in the logs.

**`ACM Hub configuration is invalid` on startup.** The message lists each bad
variable by name. Compare against `.env.example`. A common cause is a
`DATABASE_URL` using `postgresql://` instead of `postgresql+asyncpg://`, which is
rejected because a synchronous driver would block the gateway.

**Nothing appears in the review channel.** Run `/acm-admin health` to confirm the
scheduler is running and the last collector run succeeded. Then `/acm-admin sync`
to force a cycle. If the log shows `scheduler.review_channel_unavailable`, the
bot cannot see `OFFICER_REVIEW_CHANNEL_ID` — check the ID and the channel
permissions, then `/acm-admin requeue`.

**Approve says it worked but nothing was posted.** The opportunity is `approved`
but publishing failed. Check the bot's permissions in `#opportunities`, then
re-run `/acm-admin approve <id>`.

**Everything is tagged with no SIGs.** Expected for listings with a generic title
and no description — the Simplify feed carries no prose, so classification leans
on the title and upstream category. Lower `SIG_SCORE_THRESHOLD` or add terms in
`config/sigs.py`.

**Duplicate posts.** Should be impossible; both guarantees are database
constraints. If you see one, check whether two instances are running against two
different databases.

**`/acm-admin` says "limited to ACM officers".** Set `OFFICER_ROLE_ID` to your
officer role, or grant Manage Server.

**Collector logs HTTP 403 for internships.** Add `GITHUB_TOKEN` to `.env` and
restart. The bot tries the GitHub API, then `raw.githubusercontent.com`.

**Collector logs HTTP 403 for ACM conferences.** acm.org often blocks
datacenter and Docker egress with Cloudflare. ACM Hub does not bypass that.
Browsers can still open the public page. Set `ACM_CONFERENCES_ENABLED=false` to
stop the failing source, or run `/acm-admin sync` from a network where
`https://www.acm.org/conferences` loads without a challenge.

## Roadmap

| Milestone | Scope | Status |
| --- | --- | --- |
| 1 | Foundation, config, database, Discord connection, status | Done |
| 2 | Canonical model, collector interface, Simplify collector | Done |
| 3 | Deduplication, change detection, SIG classifier, persistence | Done |
| 4 | Officer review, approve/reject, publishing, idempotency | Done |
| 4.5 | Baseline import, closure detection, source trust policy, review-queue ops | Done |
| 5 | `/acm opportunities`, subscriptions, filtering and search | Done |
| 6 | ASU Ready-to-Mentor research radar, `/acm research` | Done |
| 7 | Official ACM conferences page, `/acm conferences` | Done |
| 7.5 | `/acm digest` HTML export of published opportunities | Done |
| 8 | Remaining production ops (multi-instance, alerting) | Partial |

The research radar only labels a professor as recruiting when the official
Fulton Forge **Ready to mentor** section says so. Publication records, grants,
lab pages and the **Mentors from the current expo** list are not evidence of
mentorship availability.

ACM conference ingestion uses only `https://www.acm.org/conferences` (allowed
by robots.txt). It does not scrape `/calendar`. If Cloudflare blocks the fetch,
the source fails in `/acm-admin sync` and `/acm status` — ACM Hub will not
ship a hardcoded conference catalogue. Conference dates are event dates, not
invented CFP deadlines.

## License

MIT.
