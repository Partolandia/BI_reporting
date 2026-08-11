# Jira Ticket Monitoring Dashboard

Tracks tickets in the CSSD, PIE, ACP, and IAP Jira projects and flags ones
that have gone stale — see the project brief for the full plan. Connection
and staleness logic are done; no dashboard UI or Slack integration yet.

## What's here so far

- `jira_client.py` — connects to Jira and fetches tickets and their change
  history via the REST API.
- `test_connection.py` — a script you run to prove the connection works.
- `staleness.py` — the business logic that decides if a ticket is stale.
  See "How staleness is calculated" below.
- `check_staleness.py` — a script that prints every open ticket classified
  red/yellow/green, plus separate "Ready to Close" and "Status Needs
  Updating" lists.
- `list_projects.py` — a script that lists every Jira project your account
  can see, with its real project key. Useful if you ever need to add or
  double check a project key.
- `.env.example` — template for your credentials. Copy it to `.env` and fill
  in your real values. `.env` is listed in `.gitignore`, so it never gets
  committed to git or pushed to GitHub — your token stays local to your
  machine.

## Setup (one-time)

1. **Generate a Jira API token** (do this even if you already had one — if
   you ever pasted a token into a chat, treat it as compromised and make a
   fresh one):
   - Go to https://id.atlassian.com/manage-profile/security/api-tokens
   - Click "Create API token", give it a label like "ticket-dashboard"
   - Copy the token immediately — Atlassian only shows it once

2. **Install Python dependencies** (from this folder):
   ```
   pip install -r requirements.txt
   ```

3. **Create your `.env` file**:
   ```
   cp .env.example .env
   ```
   Then open `.env` in any text editor and paste in your real API token.
   Leave everything else as-is unless your site URL, project keys, or team
   member list differ. `JIRA_TEAM_MEMBERS` controls whose tickets get
   pulled — it must match each person's exact Jira display name. Leave it
   blank to pull tickets for every assignee instead of a specific team.

4. **Run the connection test**:
   ```
   python test_connection.py
   ```
   If it works, you'll see a per-project ticket count and a table of sample
   tickets (key, type, status, assignee, last updated). If it fails, the
   error message will usually tell you whether it's a credentials problem
   (401/403) or something else.

5. **Run the staleness check**:
   ```
   python check_staleness.py
   ```
   This pulls every *open* ticket (skips ones already Done) for the team,
   fetches each one's change history and comments, and prints tables grouped
   by RED / YELLOW / READY TO CLOSE / GREEN plus a summary count. It's
   slower than `test_connection.py` — expect roughly 1-2 seconds per open
   ticket, since it fetches each ticket's history and comments individually.

   To check just one or a few projects instead of all of them, pass their
   keys (comma-separated, no spaces):
   ```
   python check_staleness.py CSSD
   python check_staleness.py CSSD,IAP
   ```

   To check only specific statuses, use `--status` (comma-separated, must
   match Jira's exact status names):
   ```
   python check_staleness.py --status Open
   python check_staleness.py CSSD --status Open,Scoping
   ```

## Why the search endpoint looks the way it does

Jira retired its old ticket-search API in May 2025. `jira_client.py` uses the
current one (`/rest/api/3/search/jql`), which pages through results using a
token instead of a simple offset — that's handled for you inside
`get_tickets()`, nothing to configure.

## How staleness is calculated

This is the important part, since it's the whole point of the dashboard.

A ticket's generic **status** field (Open, In Progress, etc.) isn't trusted
on its own — people often leave it on "Open" even after real progress has
happened. So instead, for each open ticket we look at three dates and take
whichever is **most recent**:

1. When the ticket was **created**
2. The last time its **status actually changed** (pulled from Jira's change
   history, not the status label itself)
3. The last time someone **commented** on it

That "most recent of the three" date is the ticket's *real last activity*.
Staleness is how many days have passed since then:

- **Green** — less than 2 days since real last activity
- **Yellow** — 2 to 3 days
- **Red** — 3+ days (this is the "stale" / needs-attention flag)

So a ticket sitting on "Open" that got a client-approval comment yesterday
shows green, while a ticket sitting on "In Design" that nobody has touched
in 4 days shows red — which is exactly the distinction the team needs.

### The "Ready to Close" category

Some tickets aren't neglected — they're just unclosed. If the client replies
"looks good, thanks!" and nobody formally closes the Jira ticket, the old
logic would flag it as increasingly red the longer it sits, which isn't
fair to whoever's assigned. So before a ticket gets labeled red or yellow,
`staleness.py` checks whether its *latest comment* sounds like a sign-off
(phrases like "looks good," "all set," "resolved," etc. — see
`CLOSURE_SIGNAL_PHRASES` at the top of `staleness.py`). If it matches, the
ticket goes into a separate **Ready to Close** list instead — a
housekeeping nudge, not an "at risk" alarm.

This is a plain keyword match, not real language understanding, so it will
miss phrasings that aren't in the list and could occasionally misfire on a
comment that happens to contain one of those phrases in a different sense.
Treat it as a starting point — tell me what it gets wrong and I'll tune the
phrase list.

### The "Status Needs Updating" flag

This is the flip side of Ready to Close, and it's the exact problem
described in the original brief: a ticket sits on the generic **Open**
status even though real progress happened — an estimate went out, a scope
got delivered, development started. The status field itself is stale, even
if the ticket isn't.

For any ticket currently marked **Open**, `staleness.py` scans every
comment (not just the latest, since the moment progress happened might be
a few comments back) for phrases like "sent the estimate," "scope
delivered," "started development," etc. — see `PROGRESS_SIGNAL_PHRASES` in
`staleness.py`. If any comment matches, the ticket gets flagged
`[STATUS NEEDS UPDATING]` in `check_staleness.py`'s output, with the
matching comment shown so you can confirm it's right.

This flag is independent of red/yellow/green/Ready to Close — a ticket can
be both, e.g. red *and* needing its status moved off Open. Same caveat as
above: it's a keyword list, so tell me what it misses or misfires on and
I'll adjust it.

## What's next

1. Build the dashboard (green/yellow/red flagging, by-person and team views,
   an "At Risk" section) — using `check_staleness.py`'s logic as the engine.
2. Wire up a scheduled pull and a daily Slack digest.
