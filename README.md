# Jira Ticket Monitoring Dashboard

Tracks tickets in the CSSD, PIE, ACP, and IAP Jira projects and (eventually) flags
ones that have gone stale — see the project brief for the full plan. This is
step 1: a working connection to Jira that pulls raw ticket data. No dashboard
or Slack integration yet.

## What's here so far

- `jira_client.py` — connects to Jira and fetches tickets via the REST API.
- `test_connection.py` — a script you run to prove the connection works.
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

## Why the search endpoint looks the way it does

Jira retired its old ticket-search API in May 2025. `jira_client.py` uses the
current one (`/rest/api/3/search/jql`), which pages through results using a
token instead of a simple offset — that's handled for you inside
`get_tickets()`, nothing to configure.

## What's next

Once the connection is confirmed working, next steps per the project brief:

1. Pull ticket **change history** (not just the current status) so we can
   tell the difference between "status field still says Open" and "nothing
   has actually happened in 3+ days" — this is the core staleness logic.
2. Build the dashboard (green/yellow/red flagging, by-person and team views,
   an "At Risk" section).
3. Wire up a scheduled pull and a daily Slack digest.
