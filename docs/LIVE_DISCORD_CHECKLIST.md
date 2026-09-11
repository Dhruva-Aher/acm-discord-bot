# Live Discord test checklist

ACM Hub's automated tests never connect to Discord. This checklist is the
manual pass that proves the bot behaves on a real guild. It is 18 items on
purpose: each one catches a failure the SQLite suite cannot see (permissions,
ephemeral visibility, button persistence after restart, channel edits).

**Do not tick an item unless you actually performed it.** A passing CI job is
not a substitute.

Use a throwaway guild or a private officer channel. Never run bulk-approve
against the production members' channel until item 12 has passed on a
throwaway.

## Before you start

- Bot invited with `bot` + `applications.commands`, no privileged intents.
- Bot role has View Channel, Send Messages, Embed Links, Read Message History
  on the two configured channels, and **not** Administrator / Manage Server /
  Kick / Ban / Manage Roles.
- `.env` filled from `.env.example`. `DISCORD_TOKEN` is not in the repo.
- Database migrated (`alembic upgrade head`).
- Bot process running; logs show `bot.commands_synced`.

## The 18 checks

1. **Commands appear.** In the guild, `/acm` offers `status`, `opportunities`,
   `research`, `conferences`, `subscribe`. `/acm-admin` is hidden from a member
   account and visible to an officer. There is no `!opportunities` prefix
   command.
2. **Non-officers are blocked.** A member running any `/acm-admin` command gets
   an ephemeral "limited to ACM officers" reply. Nothing is posted publicly.
3. **Health is safe to screenshot.** `/acm-admin health` shows database ok,
   scheduler running, latency, counts, source policies. It does **not** show a
   token, a `DATABASE_URL`, a traceback, or an exception string. A failed
   collector is "see the bot logs", not the error text.
4. **First sync does not flood.** On a source that has never been initialized,
   `/acm-admin sync` reports an initial import / baseline. The review channel
   does **not** receive thousands of cards. `/acm-admin source list` shows the
   source as initialized and a non-zero baseline held back.
5. **News is queued, backlog is not.** After that first run, add or wait for
   one genuinely new listing (or temporarily point a test source at a larger
   fixture). The next `/acm-admin sync` posts review cards only for the new
   ones, still capped by `REVIEW_QUEUE_BATCH_LIMIT`.
6. **Approve publishes once.** An officer presses Approve on a review card.
   Exactly one embed appears in the opportunities channel. The card stamps the
   decision and loses its buttons. Pressing a leftover Approve (or
   `/acm-admin approve` with the same id) says it was already reviewed and does
   not post a second message.
7. **Reject stays rejected.** Reject with a reason. `/acm-admin rejected` lists
   it. A later `/acm-admin sync` does not put a new review card up for it.
8. **Reconsider does not need SQL.** `/acm-admin reconsider <id>` returns it to
   the queue. `/acm-admin requeue` posts a fresh card. The original rejection
   remains in `/acm-admin rejected` history conceptually (audit row stays; the
   opportunity itself is no longer rejected).
9. **Queue filters work.** `/acm-admin queue sig:SIGAI` and
   `type:Internship` and `organization:…` and `newest_first:true` each change
   which rows appear. A filter matching nothing says so plainly.
10. **Bulk approve requires a second press.** `/acm-admin bulk-approve` with a
    small limit previews the batch and does not publish yet. Cancel leaves the
    queue unchanged. Confirm publishes. If one send fails (temporarily deny
    Send Messages, confirm, then restore), the reply itemises the failure and
    the others still publish.
11. **Source policy is data, not a name check.** `/acm-admin source policy`
    set to Disabled, then `/acm-admin sync`: that source is skipped, not
    contacted. Set it back to Manual review. Auto-publish is reachable only
    through this command, never by deploying.
12. **Release baseline is bounded.** `/acm-admin source release-baseline`
    asks for confirmation and releases at most the requested count, not the
    entire backlog.
13. **Members can browse.** `/acm opportunities all:true` is ephemeral, lists
    published postings with source-stated facts only, and does not show
    pending, rejected or closed rows as open. `/acm research` lists only
    research; `/acm conferences` lists only conferences. An internship must
    not appear in either.
14. **Only the requesting member can page.** Run `/acm opportunities` so a
    Next button appears (seed enough published rows). A second account that
    can see the ephemeral message — or you, from another user — cannot page
    it; they are told the controls belong to whoever ran the command. The
    owner can page, and page 2 is a different set of rows.
15. **Subscribe menus are independent.** `/acm subscribe` says ACM Hub never
    DMs anyone. Saving SIGs does not clear types, and deselecting every SIG
    means "no SIG preference" (more results), not "match nothing".
16. **Saved interests are disclosed.** After saving a SIG, `/acm opportunities`
    with no options shows `Using your ACM interests: …` and only matching
    rows. A miss tells the member to try `all:true`.
17. **`all:true` is the escape.** The same member with saved interests runs
    `/acm opportunities all:true` and sees everything published, with no
    "Using your ACM interests" line.
18. **Closures edit in place.** After a posting that was published is no
    longer in the source (and the run is a complete snapshot, not a failure),
    the existing members' message is edited to `[CLOSED] …` and no new message
    is posted. Restart the bot: an old review card's Approve/Reject buttons
    still work.

## After a failure

Log the item number, the command used, and a screenshot of the ephemeral
reply. Do not "fix" a live-server failure by weakening a test that was
already passing against mocks — the mock and the guild disagreed, and the
guild is the one members see.
