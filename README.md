# Jira Ticket Monitoring Dashboard

Tracks tickets in the CSSD, PIE, ACP, and IAP Jira projects and flags ones
that have gone stale — see the project brief for the full plan. Connection,
staleness logic, the web dashboard, and the Slack digest are all done.

## What's here so far

- `jira_client.py` — connects to Jira and fetches tickets and their change
  history via the REST API.
- `test_connection.py` — a script you run to prove the connection works.
- `staleness.py` — the business logic that decides if a ticket is stale.
  See "How staleness is calculated" below.
- `check_staleness.py` — a script that prints URGENT and Needs Estimate
  sections, then every ticket classified red/yellow/green (plus "Ready to
  Close" and "Status Needs Updating"), each with who's responsible for the
  next action.
- `list_projects.py` — a script that lists every Jira project your account
  can see, with its real project key. Useful if you ever need to add or
  double check a project key.
- `app.py` + `templates/` — the web dashboard, including the Team Workload
  page. See "Running the dashboard" and "Team Workload" below.
- `slack_digest.py` — posts a daily summary to Slack. See "Slack digest"
  below.
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
   This pulls every *open* ticket (skips ones already Done, and skips
   Awaiting Implementation / Scheduling Acceptance by default — see below)
   for the team, fetches each one's change history and comments, and
   prints tables grouped by RED / YELLOW / READY TO CLOSE / GREEN plus a
   summary count. It's slower than `test_connection.py` — expect roughly
   1-2 seconds per open ticket, since it fetches each ticket's history and
   comments individually.

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

## Running the dashboard

```
python app.py
```

Then open **http://localhost:5050** in your browser (or whatever port you
set `JIRA_DASHBOARD_PORT` to in `.env` — see "If the page won't load"
below if 5050 doesn't work either). You'll see:

- **Team Overview** by default, plus a tab for each of the 7 team members,
  a `Project:` row (CSSD/PIE/ACP/IAP), a `Quick filter:` row (currently
  just **Waiting for Approval**, for following up with clients on tickets
  sitting on their approval), and a `Client:` row (currently just
  **Standish Management**) — click any of these to filter, using the
  already-cached data so it's instant, no new Jira pull. More quick
  filters or clients can be added easily — just say which one.
- A **search box** in the header — type a project key (`CSSD`), a full
  ticket key (`CSSD-6534`), or even just a ticket number (`6534`) and hit
  Search. It matches anything typed as a substring of the ticket key
  (case-insensitive), so it works for "show me this whole project" and
  "find this one specific ticket" alike.
- **Stat tiles** for Total Tickets, Urgent, Red, Yellow, Green, Ready to
  Close, Status Needs Updating, and Needs Estimate. Total Tickets always
  reflects whichever filter is active (Team Overview, a person, or a
  project). Every tile except Total Tickets is clickable and jumps down to
  that section on the page.
- An **Urgent**, **Needs Estimate**, and **Status Needs Updating** section
  up top, then Red / Yellow / Ready to Close / Green tables below — the
  same sections and rules as `check_staleness.py`, just as a web page
  instead of terminal text.
- Every ticket **key is a link** straight to that ticket in Jira (opens in
  a new tab).
- A **Team Workload** page (tab next to Team Overview) showing how much
  genuinely actionable work each person is carrying, separated from the
  tickets they own but cannot move. See "Team Workload" below.

**Why it loads instantly instead of taking minutes:** a full pull (change
history + comments for every ticket) is slow, the same as
`check_staleness.py`. So `app.py` doesn't do that pull on every page
load — instead it refreshes in the background every `JIRA_REFRESH_MINUTES`
(default 30, set in `.env`) and always serves the last completed pull
instantly. The header shows "Last updated <time>" so you know how fresh
the data is. There's also a **Refresh now** button for an on-demand pull —
it runs in the background too, so the page won't hang; give it a minute or
two, then reload to see the update.

The last successful pull is saved to `dashboard_cache.json` (not committed
to git), so if you restart `python app.py`, the dashboard shows the
last-known data immediately instead of an empty page while the first
background refresh runs.

**This is a local, single-user tool.** Run it with `python app.py` on your
own computer and leave that terminal window open while you use it (Ctrl+C
to stop). It is *not* set up to be reachable by anyone else or hosted on a
server — putting this somewhere the rest of the team could open in their
own browser is a reasonable next step, but it deserves its own
conversation first, since it means deciding where the Jira token lives and
who can reach the page.

### If the page won't load ("connection refused")

This means the terminal running `python app.py` isn't actually listening
on that port — usually because something else on your computer already
has it. The dashboard defaults to port **5050** (not the more common 5000)
specifically because Windows often reserves port 5000 for Hyper-V/WSL,
unrelated to this app.

1. Check the terminal where you ran `python app.py`. If it printed
   `Couldn't start the dashboard on port ...`, that confirms a port
   conflict — pick a different number and add it to `.env`:
   ```
   JIRA_DASHBOARD_PORT=5051
   ```
   then run `python app.py` again and open `http://localhost:5051`.
2. Make sure that terminal window is still open — closing it stops the
   server.
3. If the terminal shows a different error (not a port message), paste it
   back here and I'll take a look.

## Team Workload

A separate page (tab next to Team Overview) for answering "who actually has
room for more work" — open `/workload` or click **Team Workload** in the
nav.

The question a raw ticket count can't answer is whether those tickets are
*actionable*. Someone holding twelve tickets where nine are parked waiting
on a client signature has far more room than someone holding five they're
all actively building. So every ticket is classified into one of ten work
states, and only the active ones count toward capacity:

| Group | States | Counts toward capacity |
| --- | --- | --- |
| Active | Ready, In progress, In review | Yes |
| Waiting on client | Estimate approval, Acceptance, Input | No |
| Waiting on others | Internal, External | No |
| Needs review | Review required | As *provisional* units |
| Excluded | Excluded | No |

**Needs review** is the deliberate escape hatch: when the evidence doesn't
support a confident answer, the ticket lands here for a human to look at
rather than being guessed into Active or silently dropped. Its units are
counted separately as "provisional" so an uncertain pile can never read as
"this person is available."

### How a ticket gets classified

`workload.py` is a pure module — no Jira calls, no Flask — that replays the
ticket's evidence in chronological order:

1. **Scope.** Exclusion rules in `workload_policy.json` match on project
   plus issue type (IAP's `Change Request` / `[System] Change` records are
   excluded from workload by default — they still appear on Team Overview).
2. **Baseline.** The ticket's Jira status maps to a starting state via
   `status_defaults_by_name`.
3. **Events.** Comments (parsed out of Atlassian Document Format) and
   status transitions are walked oldest-first. Decisive events move the
   state; text inside a blockquote is treated as context, not a new
   assertion, so a quoted old message can't reopen a settled question.
4. **Author role.** What a comment means depends on who wrote it, resolved
   by Jira accountId rather than display name — a client asking for
   approval and a teammate asking for approval are different facts.
5. **Override.** A manual entry in `workload_overrides.json` wins, but
   expires and is invalidated by newer ticket events.

Classification is deterministic: the same evidence always yields the same
state, and every row shows the rule that fired, its confidence
(`explicit` / `inferred` / `review_required`), and the evidence excerpt it
fired on. `llm_classifier_enabled` is `false` in the shipped policy.

### Effort and capacity

Active tickets are scored in **planning units** from their remaining
estimate, banded (not taken literally) and capped at 4.0 so one wildly
large estimate can't swamp a person's row. A ticket with no usable estimate
is worth 1.0 unit and is counted in `unknown_effort_count` so you can see
how much of the total is a guess.

Capacity is only ever stated against **configured** numbers. Until you fill
in `member_settings_by_account_id`, every person reads **"Capacity not
calibrated"** — the page will not invent an overload or availability claim
from peer comparison alone. Peer median is a secondary signal and only
appears when there are at least 4 comparable people and a positive median.

### Attention clocks

Separate from staleness (which is calendar days since any activity), the
workload page measures time in **business hours** — Mon–Fri 09:00–17:00
America/New_York by default, with holidays, configured under
`business_calendar`. Waiting time doesn't burn these clocks, so a ticket
parked on a client for three weeks isn't reported as neglected. What's
measured is time the *assignee* could have acted: days awaiting first
engagement, and days since real progress.

### Configuration

`workload_policy.json` is versioned and validated atomically: a bad edit
is rejected and the last valid policy stays in effect with a warning on the
page, rather than taking the dashboard down. Changing the policy
reclassifies the cached rows in place — no new Jira pull needed.

The shipped file deliberately ships **no** invented identities or
capacities. To get beyond "not calibrated" you need to fill in, with real
values from your tenant:

- `member_settings_by_account_id` — per-person `planning_capacity_units`,
  `max_started_tickets`, `role_group`, `assignment_eligible`, and
  `availability`, keyed by Jira accountId.
- `client_approver_ids_by_client` — which accountIds are genuinely the
  client's approvers, so "waiting on client" means it.
- `internal_account_ids` — internal staff (account managers included) who
  must *not* be read as clients.
- `automation_account_ids` — bots, whose comments aren't human engagement.
- `business_calendar.holidays` — your actual holiday dates.

The page tells you which accountIds it couldn't resolve, so you can fill
these in from what it reports rather than guessing up front.

### Tests

`python -m unittest discover -s tests` — synthetic fixtures only, no live
Jira calls. Covers classification, effort banding, the business calendar
(including DST), aggregation and capacity invariants, policy reload safety,
and that Team Overview's own fields come out byte-identical with
classification running.

## Slack digest

Posts a daily summary to a Slack channel, so the team doesn't need to open
the dashboard to stay aware.

**One-time setup:** create a Slack Incoming Webhook (see below) and add
its URL to `.env` as `SLACK_WEBHOOK_URL`.

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**.
   Name it something like "Ticket Digest" and pick your workspace.
2. In the left sidebar, click **Incoming Webhooks** → toggle it **On**.
3. Click **Add New Webhook to Workspace**, choose the channel the digest
   should post to, click **Allow**.
4. Copy the webhook URL (starts with `https://hooks.slack.com/services/...`)
   into `.env`:
   ```
   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
   ```

**To send it:**
```
python slack_digest.py
```
This pulls fresh data from Jira (a few minutes, same as
`check_staleness.py`) and posts a message with the same counts as the
dashboard's stat tiles, plus the full **Urgent**, **Needs Estimate**, and
**Status Needs Updating** lists (each ticket key links straight to Jira).
Each list is capped at 15 rows to keep the message a reasonable size — if
there are more, it says how many more and points to the dashboard for the
rest.

**To run it automatically every day**, use Windows Task Scheduler rather
than leaving a terminal window open all day:

1. Open **Task Scheduler** (Start menu → search "Task Scheduler").
2. Click **Create Basic Task** in the right-hand panel.
3. Name it "Jira Ticket Digest", click Next.
4. Trigger: **Daily**, click Next, set the time you want it to send (e.g.
   8:00 AM), click Next.
5. Action: **Start a program**, click Next.
6. Program/script: the full path to your Python, e.g.
   `C:\Users\<you>\AppData\Local\Programs\Python\Python313\python.exe`
   (run `where python` in Git Bash if you're not sure of the exact path).
7. Add arguments: `slack_digest.py`
8. Start in: the full path to this project folder, e.g.
   `C:\Users\<you>\Documents\BI_reporting`
9. Click Next, then **Finish**.

Task Scheduler will now run the digest every day at that time, whether or
not you're logged in (as long as your computer is on). You can test it
immediately by right-clicking the task in Task Scheduler and choosing
**Run**.

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

The rule is deliberately simple, and not based on keywords: a ticket is
only legitimately "Open" if **none of the 7 team members** (from
`JIRA_TEAM_MEMBERS` in `.env`) has commented on it yet. The moment anyone
on the team posts a comment — whatever it says — that's real work
happening, so the status field is now wrong regardless of the comment's
wording. (An earlier version tried to match specific phrases like "sent
the estimate," but that missed real cases where someone described progress
in different words — checking *who* commented instead of *what they said*
is both simpler and more reliable.)

For any ticket currently marked **Open**, `staleness.py` checks its
comments for one authored by a team member. If it finds one, the ticket
gets flagged `[STATUS NEEDS UPDATING]` in `check_staleness.py`'s output,
showing who commented, when, and what they said, so you can confirm it.
Comments from the client/requester alone don't trigger it — only a comment
from someone on the actual team does.

This flag is independent of red/yellow/green/Ready to Close — a ticket can
be both, e.g. red *and* needing its status moved off Open.

### Responsible party and the URGENT section

Every ticket now gets a **responsible_party**: `Client`, `Team member`, or
`Internal` — whoever needs to take the next action. This is based on the
ticket's status, since your team already names statuses meaningfully:

- `Pending Client`, `Client Review`, `Waiting for Approval`, `Pending` →
  **Client** (they owe a response)
- `Scheduling Acceptance` → **Internal**
- Unassigned tickets → **Internal** (needs to be triaged/assigned)
- Everything else (Scoping, In Progress, Awaiting Implementation, Open,
  etc.) → **Team member** (the assignee needs to act)

This mapping lives in `STATUS_RESPONSIBLE_PARTY` at the top of
`staleness.py` and is a best guess based on the status names seen so far —
if you spot one classified wrong (or a status not in the list at all,
which defaults to Team member), tell me and I'll fix the mapping.

A ticket is **URGENT** when it's red (3+ days, no real activity) *and* the
responsible party is Team member or Internal — i.e. it's genuinely on us,
and it's been sitting too long. A red ticket where the client owes the
reply doesn't count as urgent, since that's not the team dropping the
ball. `check_staleness.py` prints an `### URGENT ###` section at the top,
before the regular red/yellow/green breakdown.

### The "Needs Estimate" section

Estimate requests commonly come in as a comment from **Henry Glubb**
asking a team member (usually Isaac) to assign the ticket for an estimate
— mostly on CSSD tickets, but it can happen on IAP or ACP too, so this
isn't restricted to one project.

The rule: `staleness.py` scans **every** comment (not just the latest,
since the request can be several comments back with quieter discussion
after it) for one from someone in `ESTIMATE_REQUESTERS` (just Henry Glubb
for now) that also matches `ESTIMATE_REQUEST_PHRASES` — "estimat...",
"assign", or "best way forward". It needs **both** signals, not just
authorship: Henry also does ongoing hands-on work on a ticket once it's
past the estimate stage — testing his own changes, configuring things,
talking directly to the client — and none of that is a new request just
because it's his most recent comment. (An earlier version tried
authorship alone and it correctly caught real requests, but also
flagged tickets like CSSD-6209, where the actual estimate had long since
been sent and approved, and Henry's later comment was just "I made this
change, can you confirm it works" — not a request at all.)

If the most recent matching comment has no comment from a **team member**
after it, the ticket is flagged `[NEEDS ESTIMATE]`. It clears
automatically the moment a team member comments anything after the
request — we don't try to verify the estimate actually went out, just that
someone responded, same simplification used for Status Needs Updating.
This is still a plain keyword/authorship heuristic, not real
understanding, so it can miss a genuine request phrased in a new way, or
occasionally catch one that isn't — tell me about either kind of miss and
I'll adjust `ESTIMATE_REQUEST_PHRASES`.

`check_staleness.py` shows these in a dedicated `### NEEDS ESTIMATE ###`
section, with Henry's actual comment shown underneath each ticket so you
can confirm it's a real request.

### Statuses excluded by default

`Awaiting Implementation` and `Scheduling Acceptance` are left out of every
view (URGENT, red/yellow/green, Ready to Close, Needs Estimate, all of it)
by default — those mean the work already left our hands and is just
sitting in a separate implementation/scheduling process, so they're noise
for a staleness check. This list is `DEFAULT_EXCLUDED_STATUSES` at the top
of `staleness.py`; add more status names there if others should be
excluded the same way.

If you ever want to see one of these excluded statuses specifically,
`--status` overrides the exclusion:
```
python check_staleness.py --status "Awaiting Implementation"
```

### Client filters (dashboard only)

Jira doesn't have a dedicated "client" field in this workflow — the
client's name just shows up as text in the ticket title (e.g.
"Standish DB - RP - ...", "StandishDB - IP - ..."). So the dashboard's
`Client:` nav filter works by matching a keyword against the ticket
summary instead: **Standish Management** matches any summary containing
"standish" (case-insensitive), which covers "Standish DB", "StandishDB",
"Standish -", etc.

This lives in `CLIENT_FILTERS` at the top of `app.py`:
```python
CLIENT_FILTERS = {
    "Standish Management": ["standish"],
}
```
To add another client, add a line with their display name and one or more
keywords to match on — or just tell me the client name and I'll add it.

## What's next

All the features from the original brief are built. If wanted, next steps
could include hosting the dashboard somewhere the whole team can reach it
(a separate conversation — see "Running the dashboard" above), or having
the Slack digest post to Task Scheduler automatically once it's set up
(see "Slack digest" above).
